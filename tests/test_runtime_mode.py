"""RuntimeModeCoordinator 两阶段工作负载仲裁测试。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID

import pytest

from voice_realtime.meeting.models import (
    MeetingRecord,
    MeetingStatus,
    PCMOwner,
    RuntimeMode,
    StorageHealth,
)
from voice_realtime.meeting.runtime_mode import (
    MeetingNotActiveError,
    MeetingUnavailableError,
    ModeConflictError,
    RuntimeModeCoordinator,
)
from voice_realtime.meeting.session import MeetingPreparation


@dataclass(frozen=True, slots=True)
class FakeSubtitlePreparation:
    generation: int


class FakeInteraction:
    def __init__(self, calls: list[str], *, active: bool) -> None:
        self.calls = calls
        self.active = active
        self.fail_start: BaseException | None = None
        self.fail_stop: BaseException | None = None
        self.stop_sets_inactive_before_error = False
        self.cancel_stop_sets_inactive = False
        self.block_stop_once = False
        self.stop_started = asyncio.Event()
        self.stop_gate = asyncio.Event()

    async def start(self) -> None:
        self.calls.append("interaction.start")
        if self.fail_start is not None:
            raise self.fail_start
        self.active = True

    async def stop(self, *, reason: str) -> None:
        del reason
        self.calls.append("interaction.stop")
        if self.block_stop_once:
            self.block_stop_once = False
            self.stop_started.set()
            try:
                await self.stop_gate.wait()
            except asyncio.CancelledError:
                if self.cancel_stop_sets_inactive:
                    self.active = False
                raise
        if self.stop_sets_inactive_before_error:
            self.active = False
        if self.fail_stop is not None:
            error = self.fail_stop
            self.fail_stop = None
            raise error
        self.active = False


class FakeSubtitles:
    def __init__(self, calls: list[str], *, active: bool) -> None:
        self.calls = calls
        self.browser_capture_active = active
        self.prepared: FakeSubtitlePreparation | None = None
        self.generation = 0
        self.fail_prepare: BaseException | None = None
        self.fail_deactivate: BaseException | None = None
        self.prepare_started = asyncio.Event()
        self.prepare_gate: asyncio.Event | None = None

    async def prepare_browser_capture(
        self, *, timeout_secs: float
    ) -> FakeSubtitlePreparation:
        assert timeout_secs > 0
        self.calls.append("subtitles.prepare")
        self.prepare_started.set()
        if self.fail_prepare is not None:
            raise self.fail_prepare
        if self.prepare_gate is not None:
            await self.prepare_gate.wait()
        self.generation += 1
        self.prepared = FakeSubtitlePreparation(self.generation)
        return self.prepared

    def commit_browser_capture(self, preparation: FakeSubtitlePreparation) -> None:
        assert self.prepared is preparation
        self.calls.append("subtitles.commit")
        self.prepared = None
        self.browser_capture_active = True

    async def abort_browser_capture(self, preparation: FakeSubtitlePreparation) -> None:
        assert self.prepared is preparation
        self.calls.append("subtitles.abort")
        self.prepared = None

    async def deactivate_browser_capture(self) -> None:
        self.calls.append("subtitles.deactivate")
        if self.fail_deactivate is not None:
            error = self.fail_deactivate
            self.fail_deactivate = None
            raise error
        self.browser_capture_active = False
        self.prepared = None


class FakeMeeting:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.storage_health = StorageHealth.OK
        self.active_meeting_id = None
        self.prepared: MeetingPreparation | None = None
        self.record = MeetingRecord(title="周会")
        self.fail_prepare: BaseException | None = None
        self.fail_stop: BaseException | None = None
        self.fail_publish: BaseException | None = None
        self.abort_failures = 0
        self.block_stop_once = False
        self.stop_started = asyncio.Event()
        self.stop_gate = asyncio.Event()

    async def prepare_start(self, title: str | None = None) -> MeetingPreparation:
        self.calls.append("meeting.prepare")
        if self.fail_prepare is not None:
            raise self.fail_prepare
        self.record = MeetingRecord(title=title or "周会")
        self.prepared = MeetingPreparation(record=self.record, capture=object())
        return self.prepared

    def commit_start(self, preparation: MeetingPreparation) -> MeetingRecord:
        assert self.prepared is preparation
        self.calls.append("meeting.commit")
        self.prepared = None
        self.active_meeting_id = preparation.record.id
        return preparation.record

    async def publish_started(self, preparation: MeetingPreparation) -> None:
        self.calls.append("meeting.publish")
        if self.fail_publish is not None:
            raise self.fail_publish
        assert self.active_meeting_id == preparation.record.id

    async def abort_start(self, preparation: MeetingPreparation) -> None:
        assert self.prepared is preparation
        self.calls.append("meeting.abort")
        if self.abort_failures:
            self.abort_failures -= 1
            raise RuntimeError("meeting abort unavailable")
        self.prepared = None

    async def stop(self) -> MeetingRecord:
        self.calls.append("meeting.stop")
        if self.block_stop_once:
            self.block_stop_once = False
            self.stop_started.set()
            try:
                await self.stop_gate.wait()
            except asyncio.CancelledError:
                self.active_meeting_id = None
                self.record = self.record.model_copy(
                    update={
                        "status": MeetingStatus.INTERRUPTED,
                        "interruption_reason": "meeting_stop_failed",
                    }
                )
                raise
        self.active_meeting_id = None
        if self.fail_stop is not None:
            error = self.fail_stop
            self.fail_stop = None
            self.record = self.record.model_copy(
                update={
                    "status": MeetingStatus.INTERRUPTED,
                    "interruption_reason": "meeting_stop_failed",
                }
            )
            raise error
        self.record = self.record.model_copy(update={"status": MeetingStatus.COMPLETED})
        return self.record

    async def interrupt(self, reason: str) -> None:
        self.calls.append("meeting.interrupt")
        self.active_meeting_id = None
        self.record = self.record.model_copy(
            update={
                "status": MeetingStatus.INTERRUPTED,
                "interruption_reason": reason,
            }
        )


@dataclass(slots=True)
class Harness:
    coordinator: RuntimeModeCoordinator
    interaction: FakeInteraction
    subtitles: FakeSubtitles
    meeting: FakeMeeting | None
    calls: list[str]
    snapshots: list[
        tuple[RuntimeMode, PCMOwner, int, UUID | None, MeetingStatus | None]
    ]


def make_harness(
    mode: RuntimeMode = RuntimeMode.ASSISTANT,
    *,
    with_meeting: bool = True,
    fail_owners: set[PCMOwner] | None = None,
) -> Harness:
    calls: list[str] = []
    snapshots: list[
        tuple[RuntimeMode, PCMOwner, int, UUID | None, MeetingStatus | None]
    ] = []
    interaction = FakeInteraction(calls, active=mode is RuntimeMode.ASSISTANT)
    subtitles = FakeSubtitles(calls, active=mode is RuntimeMode.SUBTITLES)
    meeting = FakeMeeting(calls) if with_meeting else None
    holder: dict[str, RuntimeModeCoordinator] = {}

    def on_owner_changed(owner: PCMOwner) -> None:
        calls.append(f"owner.{owner.value}")
        if fail_owners is not None and owner in fail_owners:
            raise RuntimeError("owner callback unavailable")

    def state_publisher() -> None:
        coordinator = holder["coordinator"]
        snapshots.append(
            (
                coordinator.mode,
                coordinator.pcm_owner,
                coordinator.runtime_revision,
                coordinator.active_meeting_id,
                coordinator.meeting_state,
            )
        )
        calls.append(
            "state.publish:"
            f"{coordinator.mode.value}:{coordinator.pcm_owner.value}:"
            f"{coordinator.runtime_revision}"
        )

    coordinator = RuntimeModeCoordinator(
        interaction,
        subtitles,
        meeting_session=meeting,
        initial_mode=mode,
        on_owner_changed=on_owner_changed,
        state_publisher=state_publisher,
    )
    holder["coordinator"] = coordinator
    return Harness(coordinator, interaction, subtitles, meeting, calls, snapshots)


async def test_assistant_to_subtitles_uses_two_phase_order() -> None:
    harness = make_harness()

    await harness.coordinator.start_subtitles()

    assert harness.calls == [
        "subtitles.prepare",
        "owner.none",
        "interaction.stop",
        "subtitles.commit",
        "owner.subtitles",
        "state.publish:subtitles:subtitles:1",
    ]
    assert harness.coordinator.mode is RuntimeMode.SUBTITLES
    assert harness.coordinator.pcm_owner is PCMOwner.SUBTITLES


async def test_subtitles_to_assistant_uses_two_phase_order() -> None:
    harness = make_harness(RuntimeMode.SUBTITLES)

    await harness.coordinator.start_assistant()

    assert harness.calls == [
        "interaction.start",
        "owner.none",
        "subtitles.deactivate",
        "owner.assistant",
        "state.publish:assistant:assistant:1",
    ]


async def test_inactive_assistant_mode_recovers_without_quiescing_prepared_target() -> None:
    harness = make_harness(RuntimeMode.ASSISTANT)
    harness.interaction.active = False

    await harness.coordinator.start_assistant()

    assert harness.interaction.active is True
    assert harness.calls == [
        "interaction.start",
        "owner.assistant",
        "state.publish:assistant:assistant:1",
    ]
    assert harness.coordinator.runtime_revision == 1
    assert harness.snapshots == [
        (RuntimeMode.ASSISTANT, PCMOwner.ASSISTANT, 1, None, None)
    ]


async def test_inactive_subtitles_mode_recovers_without_quiescing_prepared_target() -> None:
    harness = make_harness(RuntimeMode.SUBTITLES)
    harness.subtitles.browser_capture_active = False

    await harness.coordinator.start_subtitles()

    assert harness.subtitles.browser_capture_active is True
    assert harness.calls == [
        "subtitles.prepare",
        "subtitles.commit",
        "owner.subtitles",
        "state.publish:subtitles:subtitles:1",
    ]
    assert harness.coordinator.runtime_revision == 1
    assert harness.snapshots == [
        (RuntimeMode.SUBTITLES, PCMOwner.SUBTITLES, 1, None, None)
    ]


@pytest.mark.parametrize("source", [RuntimeMode.ASSISTANT, RuntimeMode.SUBTITLES])
async def test_active_source_to_meeting_publishes_state_before_recording(
    source: RuntimeMode,
) -> None:
    harness = make_harness(source)
    assert harness.meeting is not None

    record = await harness.coordinator.start_meeting("设计评审")

    source_stop = (
        "interaction.stop" if source is RuntimeMode.ASSISTANT else "subtitles.deactivate"
    )
    assert harness.calls == [
        "meeting.prepare",
        "owner.none",
        source_stop,
        "meeting.commit",
        "owner.meeting",
        "state.publish:meeting:meeting:1",
        "meeting.publish",
    ]
    assert record.title == "设计评审"
    assert harness.coordinator.active_meeting_id == record.id
    assert harness.snapshots == [
        (RuntimeMode.MEETING, PCMOwner.MEETING, 1, record.id, record.status)
    ]


@pytest.mark.parametrize("target", [RuntimeMode.ASSISTANT, RuntimeMode.SUBTITLES])
async def test_idle_starts_target_and_repeated_start_is_idempotent(
    target: RuntimeMode,
) -> None:
    harness = make_harness(RuntimeMode.IDLE)

    if target is RuntimeMode.ASSISTANT:
        await harness.coordinator.start_assistant()
    else:
        await harness.coordinator.start_subtitles()
    revision = harness.coordinator.runtime_revision
    harness.calls.clear()

    if target is RuntimeMode.ASSISTANT:
        await harness.coordinator.start_assistant()
    else:
        await harness.coordinator.start_subtitles()

    assert revision == 1
    assert harness.coordinator.runtime_revision == revision
    assert harness.calls == []
    assert harness.snapshots == [(target, PCMOwner(target.value), 1, None, None)]


async def test_meeting_to_idle_preserves_eof_failure_semantics() -> None:
    harness = make_harness()
    assert harness.meeting is not None
    record = await harness.coordinator.start_meeting("周会")
    harness.calls.clear()
    harness.snapshots.clear()
    harness.meeting.fail_stop = RuntimeError("finalization_timeout")

    with pytest.raises(RuntimeError, match="finalization_timeout"):
        await harness.coordinator.end_meeting(record.id)

    assert harness.calls == [
        "owner.none",
        "meeting.stop",
        "owner.none",
        "state.publish:idle:none:2",
    ]
    assert harness.coordinator.mode is RuntimeMode.IDLE
    assert harness.coordinator.active_meeting_id is None
    assert harness.snapshots == [
        (RuntimeMode.IDLE, PCMOwner.NONE, 2, None, MeetingStatus.INTERRUPTED)
    ]


async def test_end_meeting_validates_id_and_active_state() -> None:
    harness = make_harness()
    record = await harness.coordinator.start_meeting("周会")

    with pytest.raises(MeetingNotActiveError, match="不匹配"):
        await harness.coordinator.end_meeting("00000000-0000-0000-0000-000000000000")
    assert harness.coordinator.active_meeting_id == record.id

    await harness.coordinator.end_meeting(record.id)
    with pytest.raises(MeetingNotActiveError, match="没有正在录制"):
        await harness.coordinator.end_meeting()


async def test_meeting_rejects_all_repeated_start_commands() -> None:
    harness = make_harness()
    await harness.coordinator.start_meeting("周会")

    for command in (
        harness.coordinator.start_assistant,
        harness.coordinator.start_subtitles,
        harness.coordinator.start_meeting,
    ):
        with pytest.raises(ModeConflictError):
            await command()


async def test_target_prepare_failure_keeps_assistant_chain_and_revision() -> None:
    harness = make_harness()
    harness.subtitles.fail_prepare = RuntimeError("WLK secret response body")

    with pytest.raises(RuntimeError, match="WLK"):
        await harness.coordinator.start_subtitles()

    assert harness.interaction.active is True
    assert harness.coordinator.mode is RuntimeMode.ASSISTANT
    assert harness.coordinator.pcm_owner is PCMOwner.ASSISTANT
    assert harness.coordinator.runtime_revision == 0
    assert harness.snapshots == []
    assert "interaction.stop" not in harness.calls
    transition = harness.coordinator.last_transition
    assert transition is not None
    assert transition["error_type"] == "RuntimeError"
    assert transition["error_code"] == "service_unavailable"
    assert "secret" not in repr(transition)


async def test_source_quiesce_failure_aborts_target_and_restores_source() -> None:
    harness = make_harness()
    harness.interaction.fail_stop = RuntimeError("drain failed")

    with pytest.raises(RuntimeError, match="drain"):
        await harness.coordinator.start_subtitles()

    assert harness.calls == [
        "subtitles.prepare",
        "owner.none",
        "interaction.stop",
        "subtitles.abort",
        "owner.assistant",
        "state.publish:assistant:assistant:1",
    ]
    assert harness.coordinator.mode is RuntimeMode.ASSISTANT
    assert harness.coordinator.pcm_owner is PCMOwner.ASSISTANT
    assert harness.coordinator.last_transition is not None
    assert harness.coordinator.last_transition["rollback_result"] == "success"


async def test_failed_source_recovery_forces_all_workloads_idle() -> None:
    harness = make_harness()
    harness.interaction.stop_sets_inactive_before_error = True
    harness.interaction.fail_stop = RuntimeError("drain body")
    harness.interaction.fail_start = RuntimeError("restore body")

    with pytest.raises(MeetingUnavailableError) as raised:
        await harness.coordinator.start_subtitles()

    assert raised.value.code == "service_unavailable"
    assert harness.coordinator.mode is RuntimeMode.IDLE
    assert harness.coordinator.pcm_owner is PCMOwner.NONE
    assert harness.coordinator.runtime_revision == 1
    assert "subtitles.abort" in harness.calls
    assert "subtitles.deactivate" in harness.calls
    assert harness.calls[-2:] == ["owner.none", "state.publish:idle:none:1"]
    transition = harness.coordinator.last_transition
    assert transition is not None
    assert transition["rollback_result"] == "failed"
    assert "body" not in repr(transition)


async def test_user_commands_share_one_non_preemptive_lock() -> None:
    harness = make_harness()
    harness.subtitles.prepare_gate = asyncio.Event()
    first = asyncio.create_task(harness.coordinator.start_subtitles())
    await harness.subtitles.prepare_started.wait()
    second = asyncio.create_task(harness.coordinator.start_assistant())
    await asyncio.sleep(0)

    assert "interaction.start" not in harness.calls
    assert not first.done()
    assert not second.done()

    harness.subtitles.prepare_gate.set()
    await asyncio.gather(first, second)

    assert harness.coordinator.mode is RuntimeMode.ASSISTANT
    assert harness.coordinator.runtime_revision == 2
    first_publish = harness.calls.index("state.publish:subtitles:subtitles:1")
    second_prepare = harness.calls.index("interaction.start")
    assert first_publish < second_prepare


async def test_shutdown_cancels_transition_aborts_target_and_stops_everything() -> None:
    harness = make_harness()
    harness.interaction.block_stop_once = True
    transition = asyncio.create_task(harness.coordinator.start_subtitles())
    await harness.interaction.stop_started.wait()

    await harness.coordinator.stop()

    with pytest.raises(asyncio.CancelledError):
        await transition
    assert "subtitles.abort" in harness.calls
    assert harness.calls.count("interaction.stop") == 2
    assert "subtitles.deactivate" in harness.calls
    assert harness.coordinator.mode is RuntimeMode.IDLE
    assert harness.coordinator.pcm_owner is PCMOwner.NONE
    assert harness.coordinator.runtime_revision == 1
    with pytest.raises(MeetingUnavailableError):
        await harness.coordinator.start_assistant()


async def test_stop_active_mode_handles_assistant_subtitles_and_idle() -> None:
    for mode, expected_stop in (
        (RuntimeMode.ASSISTANT, "interaction.stop"),
        (RuntimeMode.SUBTITLES, "subtitles.deactivate"),
    ):
        harness = make_harness(mode)
        await harness.coordinator.stop_active_mode()
        assert expected_stop in harness.calls
        assert harness.coordinator.mode is RuntimeMode.IDLE
        assert harness.coordinator.pcm_owner is PCMOwner.NONE

    idle = make_harness(RuntimeMode.IDLE)
    await idle.coordinator.stop_active_mode()
    assert idle.coordinator.runtime_revision == 0
    assert idle.calls == []


async def test_meeting_publish_failure_does_not_rollback_committed_state() -> None:
    harness = make_harness()
    assert harness.meeting is not None
    harness.meeting.fail_publish = RuntimeError("event unavailable")

    record = await harness.coordinator.start_meeting("周会")

    assert harness.coordinator.mode is RuntimeMode.MEETING
    assert harness.coordinator.active_meeting_id == record.id
    assert harness.calls.index("state.publish:meeting:meeting:1") < harness.calls.index(
        "meeting.publish"
    )


async def test_meeting_can_be_configured_once_after_construction() -> None:
    harness = make_harness(with_meeting=False)
    meeting = FakeMeeting(harness.calls)

    with pytest.raises(MeetingUnavailableError):
        await harness.coordinator.start_meeting("周会")
    assert harness.coordinator.mode is RuntimeMode.ASSISTANT

    harness.coordinator.configure_meeting(meeting)
    await harness.coordinator.start_meeting("周会")
    with pytest.raises(RuntimeError, match="已配置"):
        harness.coordinator.configure_meeting(FakeMeeting(harness.calls))


def test_constructor_keeps_legacy_positional_meeting_call_compatible() -> None:
    calls: list[str] = []
    interaction = FakeInteraction(calls, active=False)
    meeting = FakeMeeting(calls)

    coordinator = RuntimeModeCoordinator(interaction, meeting)

    assert coordinator.meeting is meeting
    assert coordinator.mode is RuntimeMode.IDLE
    assert coordinator.pcm_owner is PCMOwner.NONE
    assert coordinator.storage is StorageHealth.OK


def test_constructor_detects_delegated_subtitle_callables_without_false_positive() -> None:
    calls: list[str] = []
    interaction = FakeInteraction(calls, active=False)

    async def prepare_browser_capture(*, timeout_secs: float):
        del timeout_secs
        return object()

    async def async_noop(*_args, **_kwargs) -> None:
        return None

    delegated = SimpleNamespace(
        browser_capture_active=False,
        prepare_browser_capture=prepare_browser_capture,
        commit_browser_capture=lambda _preparation: None,
        abort_browser_capture=async_noop,
        deactivate_browser_capture=async_noop,
    )
    non_callable = SimpleNamespace(
        prepare_browser_capture=True,
        commit_browser_capture=True,
        abort_browser_capture=True,
        deactivate_browser_capture=True,
    )

    coordinator = RuntimeModeCoordinator(interaction, delegated)
    legacy = RuntimeModeCoordinator(interaction, non_callable)

    assert coordinator.subtitles is delegated
    assert coordinator.meeting is None
    assert legacy.subtitles is None
    assert legacy.meeting is non_callable


async def test_direct_transition_cancellation_restores_assistant_source() -> None:
    harness = make_harness()
    harness.interaction.block_stop_once = True
    transition = asyncio.create_task(harness.coordinator.start_subtitles())
    await harness.interaction.stop_started.wait()

    transition.cancel()
    with pytest.raises(asyncio.CancelledError):
        await transition

    assert harness.subtitles.prepared is None
    assert harness.subtitles.browser_capture_active is False
    assert harness.interaction.active is True
    assert harness.coordinator.mode is RuntimeMode.ASSISTANT
    assert harness.coordinator.pcm_owner is PCMOwner.ASSISTANT
    assert harness.coordinator.runtime_revision == 1
    assert harness.snapshots == [
        (RuntimeMode.ASSISTANT, PCMOwner.ASSISTANT, 1, None, None)
    ]


async def test_direct_transition_cancellation_recovery_failure_forces_idle() -> None:
    harness = make_harness()
    harness.interaction.block_stop_once = True
    harness.interaction.cancel_stop_sets_inactive = True
    harness.interaction.fail_start = RuntimeError("assistant restore unavailable")
    transition = asyncio.create_task(harness.coordinator.start_subtitles())
    await harness.interaction.stop_started.wait()

    transition.cancel()
    with pytest.raises(asyncio.CancelledError):
        await transition

    assert harness.subtitles.prepared is None
    assert harness.subtitles.browser_capture_active is False
    assert harness.interaction.active is False
    assert harness.coordinator.mode is RuntimeMode.IDLE
    assert harness.coordinator.pcm_owner is PCMOwner.NONE
    assert harness.coordinator.runtime_revision == 1
    assert harness.snapshots == [
        (RuntimeMode.IDLE, PCMOwner.NONE, 1, None, None)
    ]


async def test_stop_active_mode_cancellation_restores_source_owner() -> None:
    harness = make_harness()
    harness.interaction.block_stop_once = True
    stopping = asyncio.create_task(harness.coordinator.stop_active_mode())
    await harness.interaction.stop_started.wait()

    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping

    assert harness.interaction.active is True
    assert harness.coordinator.mode is RuntimeMode.ASSISTANT
    assert harness.coordinator.pcm_owner is PCMOwner.ASSISTANT
    assert harness.coordinator.runtime_revision == 1
    assert harness.snapshots == [
        (RuntimeMode.ASSISTANT, PCMOwner.ASSISTANT, 1, None, None)
    ]


async def test_stop_is_idempotent_and_concurrent_callers_share_cleanup() -> None:
    harness = make_harness(RuntimeMode.IDLE)

    await asyncio.gather(harness.coordinator.stop(), harness.coordinator.stop())
    await harness.coordinator.stop()

    assert harness.calls == [
        "subtitles.deactivate",
        "owner.none",
        "state.publish:idle:none:1",
    ]
    assert harness.coordinator.runtime_revision == 1
    assert harness.snapshots == [
        (RuntimeMode.IDLE, PCMOwner.NONE, 1, None, None)
    ]


async def test_shutdown_cancelling_meeting_end_commits_idle_only_once() -> None:
    harness = make_harness()
    assert harness.meeting is not None
    await harness.coordinator.start_meeting("周会")
    harness.calls.clear()
    harness.snapshots.clear()
    harness.meeting.block_stop_once = True
    ending = asyncio.create_task(harness.coordinator.end_meeting())
    await harness.meeting.stop_started.wait()

    await harness.coordinator.stop()

    with pytest.raises(asyncio.CancelledError):
        await ending
    assert harness.coordinator.mode is RuntimeMode.IDLE
    assert harness.coordinator.pcm_owner is PCMOwner.NONE
    assert harness.coordinator.runtime_revision == 2
    assert harness.snapshots == [
        (RuntimeMode.IDLE, PCMOwner.NONE, 2, None, MeetingStatus.INTERRUPTED)
    ]


async def test_force_idle_retries_failed_meeting_preparation_abort() -> None:
    harness = make_harness()
    assert harness.meeting is not None
    harness.meeting.abort_failures = 1
    harness.interaction.fail_stop = RuntimeError("source quiesce unavailable")

    with pytest.raises(MeetingUnavailableError):
        await harness.coordinator.start_meeting("周会")

    assert harness.calls.count("meeting.abort") == 2
    assert harness.meeting.prepared is None
    assert harness.coordinator.mode is RuntimeMode.IDLE
    assert harness.coordinator.pcm_owner is PCMOwner.NONE


async def test_none_owner_callback_failure_aborts_target_and_restores_source() -> None:
    harness = make_harness(fail_owners={PCMOwner.NONE})

    with pytest.raises(RuntimeError, match="owner callback"):
        await harness.coordinator.start_subtitles()

    assert harness.subtitles.prepared is None
    assert harness.subtitles.browser_capture_active is False
    assert harness.interaction.active is True
    assert harness.coordinator.mode is RuntimeMode.ASSISTANT
    assert harness.coordinator.pcm_owner is PCMOwner.ASSISTANT
    assert harness.snapshots == [
        (RuntimeMode.ASSISTANT, PCMOwner.ASSISTANT, 1, None, None)
    ]


async def test_target_owner_callback_failure_deactivates_target_and_restores_source() -> None:
    harness = make_harness(fail_owners={PCMOwner.SUBTITLES})

    with pytest.raises(RuntimeError, match="owner callback"):
        await harness.coordinator.start_subtitles()

    assert harness.subtitles.prepared is None
    assert harness.subtitles.browser_capture_active is False
    assert harness.interaction.active is True
    assert harness.coordinator.mode is RuntimeMode.ASSISTANT
    assert harness.coordinator.pcm_owner is PCMOwner.ASSISTANT
    assert harness.snapshots == [
        (RuntimeMode.ASSISTANT, PCMOwner.ASSISTANT, 1, None, None)
    ]


async def test_meeting_owner_callback_failure_clears_committed_meeting_metadata() -> None:
    harness = make_harness(fail_owners={PCMOwner.MEETING})
    assert harness.meeting is not None

    with pytest.raises(RuntimeError, match="owner callback"):
        await harness.coordinator.start_meeting("周会")

    assert harness.meeting.active_meeting_id is None
    assert harness.coordinator.active_meeting_id is None
    assert harness.coordinator.meeting_state is MeetingStatus.INTERRUPTED
    assert harness.interaction.active is True
    assert harness.coordinator.mode is RuntimeMode.ASSISTANT
    assert harness.coordinator.pcm_owner is PCMOwner.ASSISTANT
    assert harness.snapshots == [
        (
            RuntimeMode.ASSISTANT,
            PCMOwner.ASSISTANT,
            1,
            None,
            MeetingStatus.INTERRUPTED,
        )
    ]


async def test_force_idle_stops_workloads_even_when_owner_callback_fails() -> None:
    harness = make_harness(fail_owners={PCMOwner.NONE, PCMOwner.ASSISTANT})

    with pytest.raises(MeetingUnavailableError):
        await harness.coordinator.start_subtitles()

    assert harness.interaction.active is False
    assert harness.subtitles.prepared is None
    assert harness.subtitles.browser_capture_active is False
    assert harness.coordinator.mode is RuntimeMode.IDLE
    assert harness.coordinator.pcm_owner is PCMOwner.NONE
    assert harness.coordinator.runtime_revision == 1
    assert harness.snapshots == [
        (RuntimeMode.IDLE, PCMOwner.NONE, 1, None, None)
    ]


async def test_inactive_source_error_restores_once_and_publishes_one_snapshot() -> None:
    harness = make_harness()
    harness.interaction.stop_sets_inactive_before_error = True
    harness.interaction.fail_stop = RuntimeError("source quiesce unavailable")

    with pytest.raises(RuntimeError, match="source quiesce"):
        await harness.coordinator.start_subtitles()

    assert harness.interaction.active is True
    assert harness.subtitles.prepared is None
    assert harness.coordinator.mode is RuntimeMode.ASSISTANT
    assert harness.coordinator.pcm_owner is PCMOwner.ASSISTANT
    assert harness.snapshots == [
        (RuntimeMode.ASSISTANT, PCMOwner.ASSISTANT, 1, None, None)
    ]
