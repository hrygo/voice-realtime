"""助手与会议模式互斥测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from voice_realtime.meeting.models import MeetingRecord, MeetingStatus, RuntimeMode, StorageHealth
from voice_realtime.meeting.runtime_mode import (
    MeetingNotActiveError,
    ModeConflictError,
    RuntimeModeCoordinator,
)


@pytest.fixture()
def coordinator() -> RuntimeModeCoordinator:
    interaction = MagicMock()
    interaction.active = True
    interaction.stop = AsyncMock()
    interaction.start = AsyncMock()
    meeting = MagicMock()
    meeting.start = AsyncMock(return_value=MeetingRecord(title="周会"))
    meeting.stop = AsyncMock(return_value=MeetingRecord(title="周会"))
    meeting.active_meeting_id = None
    return RuntimeModeCoordinator(interaction, meeting)


async def test_start_meeting_stops_interaction_before_capture(
    coordinator: RuntimeModeCoordinator,
) -> None:
    calls: list[str] = []
    coordinator.interaction.stop.side_effect = lambda **_: calls.append("interaction.stop")
    coordinator.meeting.start.side_effect = lambda *_: (
        calls.append("meeting.start") or MeetingRecord(title="周会")
    )

    await coordinator.start_meeting("周会")

    assert coordinator.mode is RuntimeMode.MEETING
    assert calls == ["interaction.stop", "meeting.start"]


async def test_end_meeting_returns_idle_and_does_not_restart_assistant(
    coordinator: RuntimeModeCoordinator,
) -> None:
    record = await coordinator.start_meeting("周会")
    coordinator.meeting.stop.return_value = record.model_copy(update={"status": "completed"})

    await coordinator.end_meeting(record.id)

    assert coordinator.mode is RuntimeMode.IDLE
    coordinator.interaction.start.assert_not_awaited()


async def test_start_assistant_refuses_while_meeting_active(
    coordinator: RuntimeModeCoordinator,
) -> None:
    await coordinator.start_meeting("周会")

    with pytest.raises(RuntimeError, match=r"meeting|会议"):
        await coordinator.start_assistant()


def test_constructor_accepts_alias_and_requires_meeting_session() -> None:
    interaction = MagicMock(active=False)
    meeting = MagicMock()
    coordinator = RuntimeModeCoordinator(interaction, meeting_session=meeting)

    assert coordinator.mode is RuntimeMode.IDLE
    assert coordinator.runtime_revision == 0
    with pytest.raises(ValueError, match="meeting session"):
        RuntimeModeCoordinator(interaction)


def test_properties_report_meeting_and_storage_state(coordinator: RuntimeModeCoordinator) -> None:
    assert coordinator.meeting_state is None
    assert coordinator.meeting_started_at is None
    assert coordinator.storage is StorageHealth.OK

    coordinator.meeting.storage_health = StorageHealth.DEGRADED
    assert coordinator.storage is StorageHealth.DEGRADED
    coordinator.meeting.storage_health = "not-a-health"
    assert coordinator.storage is StorageHealth.OK


async def test_start_meeting_rejects_duplicate_start(coordinator: RuntimeModeCoordinator) -> None:
    await coordinator.start_meeting("周会")

    with pytest.raises(ModeConflictError, match="已经在录制"):
        await coordinator.start_meeting("第二场")

    coordinator.meeting.start.assert_awaited_once_with("周会")


async def test_start_meeting_rejects_unknown_mode_defensively(
    coordinator: RuntimeModeCoordinator,
) -> None:
    coordinator._mode = object()

    with pytest.raises(ModeConflictError, match="当前模式"):
        await coordinator.start_meeting("周会")


async def test_start_meeting_failure_restores_assistant_mode(
    coordinator: RuntimeModeCoordinator,
) -> None:
    coordinator.meeting.start.side_effect = RuntimeError("storage unavailable")

    with pytest.raises(RuntimeError, match="storage"):
        await coordinator.start_meeting("周会")

    assert coordinator.mode is RuntimeMode.ASSISTANT
    assert coordinator.active_meeting_id is None
    coordinator.interaction.start.assert_awaited_once_with()


async def test_start_meeting_failure_from_idle_does_not_start_assistant() -> None:
    interaction = MagicMock(active=False)
    interaction.start = AsyncMock()
    meeting = MagicMock()
    meeting.start = AsyncMock(side_effect=RuntimeError("storage unavailable"))
    coordinator = RuntimeModeCoordinator(interaction, meeting)

    with pytest.raises(RuntimeError, match="storage"):
        await coordinator.start_meeting("周会")

    assert coordinator.mode is RuntimeMode.IDLE
    interaction.start.assert_not_awaited()


async def test_end_meeting_validates_id_and_leaves_meeting_active_on_mismatch(
    coordinator: RuntimeModeCoordinator,
) -> None:
    record = await coordinator.start_meeting("周会")

    with pytest.raises(MeetingNotActiveError, match="不匹配"):
        await coordinator.end_meeting("00000000-0000-0000-0000-000000000000")

    assert coordinator.mode is RuntimeMode.MEETING
    assert coordinator.active_meeting_id == record.id
    coordinator.meeting.stop.assert_not_awaited()


async def test_end_meeting_rejects_when_no_meeting_is_active(
    coordinator: RuntimeModeCoordinator,
) -> None:
    with pytest.raises(MeetingNotActiveError, match="没有正在录制"):
        await coordinator.end_meeting()


async def test_end_meeting_failure_still_returns_to_idle(
    coordinator: RuntimeModeCoordinator,
) -> None:
    await coordinator.start_meeting("周会")
    coordinator.meeting.stop.side_effect = RuntimeError("finalize failed")

    with pytest.raises(RuntimeError, match="finalize"):
        await coordinator.end_meeting()

    assert coordinator.mode is RuntimeMode.IDLE
    assert coordinator.active_meeting_id is None
    assert coordinator.runtime_revision == 2


async def test_start_assistant_is_idempotent_when_already_active(
    coordinator: RuntimeModeCoordinator,
) -> None:
    await coordinator.start_assistant()

    coordinator.interaction.start.assert_not_awaited()
    assert coordinator.mode is RuntimeMode.ASSISTANT
    assert coordinator.runtime_revision == 0


async def test_start_assistant_recovers_from_idle() -> None:
    interaction = MagicMock(active=False)
    interaction.start = AsyncMock()
    meeting = MagicMock()
    coordinator = RuntimeModeCoordinator(interaction, meeting)

    await coordinator.start_assistant()

    interaction.start.assert_awaited_once_with()
    assert coordinator.mode is RuntimeMode.ASSISTANT
    assert coordinator.runtime_revision == 1


async def test_stop_active_mode_stops_meeting_without_restarting_assistant(
    coordinator: RuntimeModeCoordinator,
) -> None:
    record = await coordinator.start_meeting("周会")
    stopped_record = coordinator.meeting.stop.return_value

    await coordinator.stop_active_mode()

    assert coordinator.mode is RuntimeMode.IDLE
    assert coordinator.active_meeting_id is None
    assert record != stopped_record
    assert coordinator.meeting_record == stopped_record
    coordinator.meeting.stop.assert_awaited_once_with()
    coordinator.interaction.start.assert_not_awaited()


async def test_stop_active_mode_meeting_failure_still_clears_mode(
    coordinator: RuntimeModeCoordinator,
) -> None:
    await coordinator.start_meeting("周会")
    coordinator.meeting.stop.side_effect = RuntimeError("stop failed")

    with pytest.raises(RuntimeError, match="stop"):
        await coordinator.stop_active_mode()

    assert coordinator.mode is RuntimeMode.IDLE
    assert coordinator.active_meeting_id is None


async def test_stop_active_mode_handles_inconsistent_meeting_without_id(
    coordinator: RuntimeModeCoordinator,
) -> None:
    coordinator._mode = RuntimeMode.MEETING
    coordinator.meeting.stop.return_value = MeetingRecord(title="孤立会议")

    await coordinator.stop_active_mode()

    assert coordinator.mode is RuntimeMode.IDLE
    assert coordinator.active_meeting_id is None
    assert coordinator.meeting_record is None


@pytest.mark.parametrize(
    ("initial_mode", "interaction_active", "should_stop"),
    [
        (RuntimeMode.ASSISTANT, True, True),
        (RuntimeMode.ASSISTANT, False, True),
        (RuntimeMode.IDLE, True, True),
        (RuntimeMode.IDLE, False, False),
    ],
)
async def test_stop_active_mode_stops_only_when_interaction_is_active(
    initial_mode: RuntimeMode,
    interaction_active: bool,
    should_stop: bool,
) -> None:
    interaction = MagicMock(active=interaction_active)
    interaction.stop = AsyncMock()
    meeting = MagicMock()
    coordinator = RuntimeModeCoordinator(interaction, meeting, initial_mode=initial_mode)

    await coordinator.stop_active_mode()

    assert coordinator.mode is RuntimeMode.IDLE
    if should_stop:
        interaction.stop.assert_awaited_once_with(reason="停止当前模式")
    else:
        interaction.stop.assert_not_awaited()


async def test_application_stop_interrupts_meeting_and_swallows_interrupt_failure(
    coordinator: RuntimeModeCoordinator,
) -> None:
    await coordinator.start_meeting("周会")
    coordinator.meeting.interrupt = AsyncMock(side_effect=RuntimeError("already closed"))

    await coordinator.stop()

    assert coordinator.mode is RuntimeMode.IDLE
    assert coordinator.active_meeting_id is None
    coordinator.meeting.interrupt.assert_awaited_once_with("应用停止")


async def test_application_stop_stops_assistant_or_is_noop_when_idle() -> None:
    interaction = MagicMock(active=True)
    interaction.stop = AsyncMock()
    coordinator = RuntimeModeCoordinator(interaction, MagicMock())

    await coordinator.stop()

    interaction.stop.assert_awaited_once_with(reason="应用停止")
    assert coordinator.runtime_revision == 1

    interaction.stop.reset_mock()
    interaction.active = False
    await coordinator.stop()
    interaction.stop.assert_not_awaited()
    assert coordinator.runtime_revision == 2


async def test_end_meeting_updates_record_and_state(coordinator: RuntimeModeCoordinator) -> None:
    await coordinator.start_meeting("周会")
    completed = MeetingRecord(title="周会", status=MeetingStatus.COMPLETED)
    coordinator.meeting.stop.return_value = completed

    result = await coordinator.end_meeting()

    assert result == completed
    assert coordinator.meeting_record == completed
    assert coordinator.meeting_state is MeetingStatus.COMPLETED
    assert coordinator.meeting_started_at == completed.started_at
