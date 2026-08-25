"""MeetingSession 生命周期测试。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

from voice_realtime.meeting.models import (
    MeetingRecord,
    MeetingStatus,
    NormalizedSegment,
    StorageHealth,
    TranscriptReconcileResult,
    TranscriptWindow,
)
from voice_realtime.meeting.session import (
    MeetingSession,
    MeetingStorageUnavailableError,
)


class FakeRepository:
    def __init__(self) -> None:
        self.record = MeetingRecord(title="周会")
        self.calls: list[str] = []
        self.writable = True
        self.create_error: Exception | None = None
        self.status_error: Exception | None = None
        self.reconcile_error: Exception | None = None
        self.finalize_error: Exception | None = None
        self.minutes_error: Exception | None = None
        self.stale_count = 0

    async def check_writable(self) -> bool:
        return self.writable

    async def create_meeting(
        self, title: str, *, language: str, audio_source: str
    ) -> MeetingRecord:
        if self.create_error is not None:
            raise self.create_error
        self.calls.append("create")
        self.record = MeetingRecord(title=title, language=language, audio_source=audio_source)
        return self.record

    async def get_meeting(self, meeting_id: UUID) -> MeetingRecord | None:
        return self.record if self.record.id == meeting_id else None

    async def set_status(
        self, meeting_id: UUID, status: MeetingStatus, *, reason: str | None = None
    ) -> MeetingRecord:
        if self.status_error is not None:
            raise self.status_error
        self.calls.append(status.value)
        self.record = self.record.model_copy(
            update={"status": status, "interruption_reason": reason}
        )
        return self.record

    async def reconcile_window(self, meeting_id: UUID, window: TranscriptWindow):
        if self.reconcile_error is not None:
            raise self.reconcile_error
        self.calls.append("reconcile")
        return TranscriptReconcileResult(
            meeting_id=meeting_id,
            transcript_revision=1,
            content_revision=1,
            replace_from_ms=min((item.start_ms for item in window.segments), default=0),
            segments=window.segments,
        )

    async def finalize_transcript(
        self,
        meeting_id: UUID,
        *,
        final_status: MeetingStatus = MeetingStatus.COMPLETED,
        reason: str | None = None,
    ) -> MeetingRecord:
        if self.finalize_error is not None:
            raise self.finalize_error
        self.calls.append("finalize")
        self.record = self.record.model_copy(
            update={"status": final_status, "interruption_reason": reason}
        )
        return self.record

    async def create_minutes(self, meeting_id: UUID, *, idempotency_key: str | None):
        if self.minutes_error is not None:
            raise self.minutes_error
        self.calls.append("minutes")
        return

    async def recover_stale(self) -> int:
        return self.stale_count


class FakeGateway:
    def __init__(self) -> None:
        self.listeners: list[Callable[[TranscriptWindow], Awaitable[None]]] = []
        self.gap_listeners: list[Callable[[object], Awaitable[None]]] = []
        self.capture = object()
        self.prepare_capture = AsyncMock(return_value=self.capture)
        self.commit_capture = Mock()
        self.abort_prepared_capture = AsyncMock()
        self.finish_capture = AsyncMock(return_value=TranscriptWindow(source_epoch=1))
        self.abort_capture = AsyncMock()

    def add_event_listener(self, listener):
        self.listeners.append(listener)

    def remove_event_listener(self, listener):
        if getattr(self, "remove_event_listener_error", None) is not None:
            raise self.remove_event_listener_error
        self.listeners.remove(listener)

    def add_gap_listener(self, listener):
        self.gap_listeners.append(listener)

    def remove_gap_listener(self, listener):
        if getattr(self, "remove_gap_listener_error", None) is not None:
            raise self.remove_gap_listener_error
        self.gap_listeners.remove(listener)


@pytest.fixture()
def repository() -> FakeRepository:
    return FakeRepository()


@pytest.fixture()
def gateway() -> FakeGateway:
    return FakeGateway()


async def _start_session(
    session: MeetingSession, title: str | None = "周会"
) -> MeetingRecord:
    preparation = await session.prepare_start(title)
    record = session.commit_start(preparation)
    await session.publish_started(preparation)
    return record


async def test_prepare_creates_record_and_listeners_without_activating_or_publishing(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    publish = AsyncMock()
    session = MeetingSession(repository, gateway, event_publisher=publish)

    preparation = await session.prepare_start("  周会  ")

    assert preparation.record.status is MeetingStatus.RECORDING
    assert preparation.record.title == "周会"
    assert session.active_meeting_id is None
    assert session.record == preparation.record
    assert len(gateway.listeners) == 1
    assert len(gateway.gap_listeners) == 1
    gateway.prepare_capture.assert_awaited_once_with(
        f"meeting:{preparation.record.id}", timeout_secs=5.0
    )
    gateway.commit_capture.assert_not_called()
    publish.assert_not_awaited()


async def test_commit_synchronously_activates_capture_without_publishing(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    publish = AsyncMock()
    session = MeetingSession(repository, gateway, event_publisher=publish)
    preparation = await session.prepare_start("周会")

    record = session.commit_start(preparation)

    assert record == preparation.record
    assert session.active_meeting_id == record.id
    gateway.commit_capture.assert_called_once_with(gateway.capture)
    publish.assert_not_awaited()


async def test_publish_started_emits_recording_only_after_commit(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    publish = AsyncMock()
    session = MeetingSession(repository, gateway, event_publisher=publish)
    preparation = await session.prepare_start("周会")

    with pytest.raises(RuntimeError, match="preparation"):
        await session.publish_started(preparation)
    publish.assert_not_awaited()

    session.commit_start(preparation)
    publish.assert_not_awaited()
    await session.publish_started(preparation)

    publish.assert_awaited_once()
    event_type, meeting_id, payload = publish.await_args.args
    assert event_type == "meeting_state_changed"
    assert meeting_id == preparation.record.id
    assert payload["status"] == "recording"


async def test_publish_failure_does_not_roll_back_committed_meeting(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    publish = AsyncMock(side_effect=RuntimeError("client disconnected"))
    session = MeetingSession(repository, gateway, event_publisher=publish)
    preparation = await session.prepare_start("周会")
    session.commit_start(preparation)

    await session.publish_started(preparation)

    assert session.active_meeting_id == preparation.record.id
    assert repository.record.status is MeetingStatus.RECORDING
    gateway.abort_prepared_capture.assert_not_awaited()


async def test_abort_marks_record_interrupted_and_releases_prepared_capture(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    session = MeetingSession(repository, gateway)
    preparation = await session.prepare_start("周会")

    await session.abort_start(preparation)

    assert repository.record.status is MeetingStatus.INTERRUPTED
    assert repository.record.interruption_reason == "mode_switch_aborted"
    assert session.record == repository.record
    assert session.active_meeting_id is None
    assert gateway.listeners == []
    assert gateway.gap_listeners == []
    gateway.abort_prepared_capture.assert_awaited_once_with(gateway.capture)


async def test_preparation_tokens_reject_stale_and_repeated_operations(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    session = MeetingSession(repository, gateway)
    first = await session.prepare_start("第一场")
    with pytest.raises(RuntimeError, match="已经"):
        await session.prepare_start("并发准备")
    await session.abort_start(first)

    with pytest.raises(RuntimeError, match="preparation"):
        await session.abort_start(first)

    second = await session.prepare_start("第二场")
    with pytest.raises(RuntimeError, match="preparation"):
        session.commit_start(first)
    session.commit_start(second)

    with pytest.raises(RuntimeError, match="preparation"):
        session.commit_start(second)
    with pytest.raises(RuntimeError, match="preparation"):
        await session.abort_start(second)
    with pytest.raises(RuntimeError, match="preparation"):
        await session.publish_started(first)

    await session.publish_started(second)
    with pytest.raises(RuntimeError, match="preparation"):
        await session.publish_started(second)


def test_meeting_session_has_no_implicit_start_api() -> None:
    assert not hasattr(MeetingSession, "start")


async def test_stop_flushes_transcript_and_returns_completed(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    session = MeetingSession(repository, gateway)
    await _start_session(session)

    result = await session.stop()

    assert result.status is MeetingStatus.COMPLETED
    assert session.active_meeting_id is None
    gateway.finish_capture.assert_awaited_once()
    assert repository.calls.index("finalizing") < repository.calls.index("finalize")
    assert "minutes" in repository.calls


async def test_partial_only_updates_do_not_reconcile_again(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    session = MeetingSession(repository, gateway)
    await _start_session(session)
    segment = NormalizedSegment(
        order=0,
        source_epoch=1,
        speaker_key="epoch:1:speaker:1",
        start_ms=0,
        end_ms=1000,
        text="已确认",
    )

    await session._on_window(
        TranscriptWindow(source_epoch=1, partial="第一版", segments=(segment,))
    )
    await session._on_window(
        TranscriptWindow(source_epoch=1, partial="第二版", segments=(segment,))
    )

    assert repository.calls.count("reconcile") == 1


async def test_session_publishes_partial_and_durable_transcript_events(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    events: list[tuple[str, UUID, object]] = []

    async def publish(event_type: str, meeting_id: UUID, payload: object) -> None:
        events.append((event_type, meeting_id, payload))

    session = MeetingSession(repository, gateway, event_publisher=publish)
    await _start_session(session)
    segment = NormalizedSegment(
        order=0,
        source_epoch=1,
        speaker_key="epoch:1:speaker:1",
        start_ms=0,
        end_ms=1000,
        text="已确认",
    )

    await session._on_window(
        TranscriptWindow(source_epoch=1, partial="正在转写", segments=(segment,))
    )

    assert any(event[0] == "meeting_state_changed" for event in events)
    partial = next(event[2] for event in events if event[0] == "transcript_partial")
    assert partial == {"text": "正在转写", "speaker_key": None, "speaker_name": None}
    reconciled = next(event[2] for event in events if event[0] == "transcript_reconciled")
    assert reconciled["replace_from_ms"] == 0
    assert reconciled["segments"][0]["text"] == "已确认"


async def test_start_rejects_unwritable_storage(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    repository.writable = False
    session = MeetingSession(repository, gateway)

    with pytest.raises(RuntimeError, match="storage"):
        await session.prepare_start("周会")

    gateway.prepare_capture.assert_not_awaited()


async def test_start_maps_create_failure_to_storage_unavailable(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    repository.create_meeting = AsyncMock(side_effect=OSError("database read only"))
    session = MeetingSession(repository, gateway)

    with pytest.raises(RuntimeError) as exc_info:
        await session.prepare_start("周会")

    assert getattr(exc_info.value, "code", None) == "storage_unavailable"
    gateway.prepare_capture.assert_not_awaited()


def test_init_requires_gateway_and_positive_timeout(repository: FakeRepository) -> None:
    with pytest.raises(ValueError, match="gateway"):
        MeetingSession(repository)
    with pytest.raises(ValueError, match="finalization_timeout_secs"):
        MeetingSession(repository, FakeGateway(), finalization_timeout_secs=0)


async def test_start_accepts_subtitle_proxy_and_generates_title(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    session = MeetingSession(repository, subtitle_proxy=gateway)

    record = await _start_session(session, "   ")

    assert record.title.startswith("会议-")
    assert session.storage_health is StorageHealth.OK
    assert session.last_window is None
    assert len(gateway.listeners) == 1
    assert len(gateway.gap_listeners) == 1


async def test_start_rejects_second_active_meeting(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    session = MeetingSession(repository, gateway)
    await _start_session(session)

    with pytest.raises(RuntimeError, match="已经在录制"):
        await session.prepare_start("第二场")

    assert repository.calls.count("create") == 1


async def test_start_requeues_summary_and_swallows_requeue_failure(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    requeue = AsyncMock(side_effect=RuntimeError("summary unavailable"))
    summary = SimpleNamespace(requeue_for_recording=requeue)
    session = MeetingSession(repository, gateway, summary_service=summary)

    preparation = await session.prepare_start("周会")
    record = session.commit_start(preparation)
    await session.publish_started(preparation)

    assert session.active_meeting_id == record.id
    requeue.assert_awaited_once_with()


async def test_start_supports_sync_summary_requeue_and_gateway_without_gap_hooks(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    gateway.add_gap_listener = None
    gateway.remove_gap_listener = None
    summary = SimpleNamespace(requeue_for_recording=lambda: None)
    session = MeetingSession(repository, gateway, summary_service=summary)

    preparation = await session.prepare_start("周会")
    session.commit_start(preparation)
    await session.publish_started(preparation)
    await session._release_listener()

    assert gateway.listeners == []
    assert gateway.gap_listeners == []


async def test_prepare_failure_cleans_state_and_marks_interrupted(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    gateway.prepare_capture.side_effect = RuntimeError("capture failed")
    session = MeetingSession(repository, gateway)

    with pytest.raises(RuntimeError, match="capture failed"):
        await session.prepare_start("周会")

    assert session.active_meeting_id is None
    assert session.record == repository.record
    assert repository.record.status is MeetingStatus.INTERRUPTED
    assert repository.record.interruption_reason == "mode_switch_aborted"
    assert gateway.listeners == []
    assert gateway.gap_listeners == []
    assert repository.calls[-1] == "interrupted"
    gateway.commit_capture.assert_not_called()
    gateway.abort_prepared_capture.assert_not_awaited()


async def test_prepare_failure_suppresses_cleanup_errors_and_releases_gap_listener(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    gateway.prepare_capture.side_effect = RuntimeError("capture failed")
    gateway.remove_event_listener_error = RuntimeError("listener cleanup failed")
    repository.status_error = RuntimeError("status update failed")
    session = MeetingSession(repository, gateway)

    with pytest.raises(RuntimeError, match="capture failed"):
        await session.prepare_start("周会")

    assert session.active_meeting_id is None
    assert session.record is not None
    assert gateway.gap_listeners == []


async def test_stop_requires_active_meeting(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    with pytest.raises(RuntimeError, match="not active"):
        await MeetingSession(repository, gateway).stop()


async def test_stop_timeout_persists_last_window_and_marks_interrupted(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    session = MeetingSession(repository, gateway, finalization_timeout_secs=0.25)
    await _start_session(session)
    segment = NormalizedSegment(
        order=0,
        source_epoch=1,
        speaker_key="epoch:1:speaker:2",
        start_ms=100,
        end_ms=200,
        text="超时前的最后一句",
    )
    last_window = TranscriptWindow(source_epoch=1, segments=(segment,))
    timeout = TimeoutError("capture did not finish")
    timeout.last_window = last_window
    gateway.finish_capture.side_effect = timeout

    result = await session.stop()

    assert result.status is MeetingStatus.INTERRUPTED
    assert result.interruption_reason == "finalization_timeout"
    gateway.finish_capture.assert_awaited_once_with(timeout_secs=0.25)
    assert repository.calls.count("reconcile") == 1
    assert repository.calls.count("finalize") == 1
    assert session.active_meeting_id is None
    assert gateway.listeners == []
    assert gateway.gap_listeners == []


@pytest.mark.parametrize("failure", [RuntimeError("flush failed"), RuntimeError("minutes failed")])
async def test_stop_failure_marks_interrupted_and_releases_listeners(
    repository: FakeRepository, gateway: FakeGateway, failure: Exception
) -> None:
    session = MeetingSession(repository, gateway)
    await _start_session(session)
    if "flush" in str(failure):
        gateway.finish_capture.side_effect = failure
    else:
        repository.minutes_error = failure

    with pytest.raises(RuntimeError, match="failed"):
        await session.stop()

    assert session.active_meeting_id is None
    assert gateway.listeners == []
    assert gateway.gap_listeners == []
    assert repository.calls[-1] == "interrupted"


async def test_interrupt_is_idempotent_when_inactive(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    session = MeetingSession(repository, gateway)
    assert await session.interrupt("没有录音") is None
    gateway.abort_capture.assert_not_awaited()


async def test_interrupt_suppresses_abort_failure_and_truncates_reason(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    session = MeetingSession(repository, gateway)
    await _start_session(session)
    gateway.abort_capture.side_effect = RuntimeError("abort failed")
    reason = "x" * 200

    result = await session.interrupt(reason)

    assert result is not None
    assert result.status is MeetingStatus.INTERRUPTED
    assert result.interruption_reason == "x" * 128
    assert session.active_meeting_id is None
    assert gateway.listeners == []
    assert gateway.gap_listeners == []


async def test_recover_stale_delegates_and_defaults_without_method(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    session = MeetingSession(repository, gateway)
    repository.stale_count = 3
    assert await session.recover_stale() == 3

    class RepositoryWithoutRecovery:
        pass

    no_recovery = MeetingSession(RepositoryWithoutRecovery(), gateway)
    assert await no_recovery.recover_stale() == 0


async def test_window_without_segments_only_emits_partial(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    events: list[tuple[str, UUID, object]] = []

    async def publish(event_type: str, meeting_id: UUID, payload: object) -> None:
        events.append((event_type, meeting_id, payload))

    session = MeetingSession(repository, gateway, event_publisher=publish)
    await _start_session(session)
    await session._on_window(TranscriptWindow(source_epoch=1, partial="只显示临时文本"))

    assert [event[0] for event in events].count("transcript_partial") == 1
    assert "reconcile" not in repository.calls


async def test_window_without_partial_still_reconciles_and_inactive_window_is_ignored(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    session = MeetingSession(repository, gateway)
    segment = NormalizedSegment(
        order=0,
        source_epoch=1,
        speaker_key="epoch:1:speaker:1",
        start_ms=0,
        end_ms=10,
        text="没有 partial",
    )

    await session._on_window(TranscriptWindow(source_epoch=1, segments=(segment,)))
    await _start_session(session)
    await session._on_window(TranscriptWindow(source_epoch=1, segments=(segment,)))
    await session.interrupt("测试结束")
    await session._on_window(TranscriptWindow(source_epoch=2, segments=(segment,)))

    assert repository.calls.count("reconcile") == 1


async def test_window_persistence_failure_uses_sync_journal_and_degrades_storage(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    class SyncJournal:
        def __init__(self) -> None:
            self.calls: list[tuple[UUID, TranscriptWindow]] = []

        def append(self, meeting_id: UUID, window: TranscriptWindow) -> None:
            self.calls.append((meeting_id, window))

    journal = SyncJournal()
    repository.reconcile_error = RuntimeError("database unavailable")
    session = MeetingSession(repository, gateway, recovery_journal=journal)
    await _start_session(session)
    window = TranscriptWindow(source_epoch=1, segments=(
        NormalizedSegment(
            order=0,
            source_epoch=1,
            speaker_key="epoch:1:speaker:1",
            start_ms=0,
            end_ms=10,
            text="暂存",
        ),
    ))

    assert await session._persist_window(session.active_meeting_id, window) is None
    assert session.storage_health is StorageHealth.DEGRADED
    assert journal.calls == [(session.active_meeting_id, window)]
    gateway.abort_capture.assert_not_awaited()


async def test_window_persistence_failure_without_journal_aborts_capture(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    repository.reconcile_error = RuntimeError("database unavailable")
    session = MeetingSession(repository, gateway)
    await _start_session(session)
    window = TranscriptWindow(source_epoch=1, segments=(
        NormalizedSegment(
            order=0,
            source_epoch=1,
            speaker_key="epoch:1:speaker:1",
            start_ms=0,
            end_ms=10,
            text="无法保存",
        ),
    ))

    with pytest.raises(RuntimeError, match="unavailable"):
        await session._persist_window(session.active_meeting_id, window)

    assert session.storage_health is StorageHealth.DEGRADED
    gateway.abort_capture.assert_awaited_once_with()


async def test_stop_without_final_window_completes_normally(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    gateway.finish_capture.return_value = None
    session = MeetingSession(repository, gateway)
    await _start_session(session)

    result = await session.stop()

    assert result.status is MeetingStatus.COMPLETED
    assert "reconcile" not in repository.calls


async def test_stop_resumes_summary_worker_and_swallows_resume_failure(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    resume = AsyncMock(side_effect=RuntimeError("worker unavailable"))
    summary = SimpleNamespace(resume_after_recording=resume)
    session = MeetingSession(repository, gateway, summary_service=summary)
    await _start_session(session)

    await session.stop()

    resume.assert_awaited_once_with()


async def test_interrupt_supports_sync_summary_resume_callback(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    summary = SimpleNamespace(resume_after_recording=lambda: None)
    session = MeetingSession(repository, gateway, summary_service=summary)
    await _start_session(session)

    result = await session.interrupt("测试结束")

    assert result is not None


@pytest.mark.parametrize("journal", [SimpleNamespace(), SimpleNamespace(append=AsyncMock(
    side_effect=RuntimeError("journal failed")
))])
async def test_window_persistence_failure_with_unusable_journal_aborts_capture(
    repository: FakeRepository, gateway: FakeGateway, journal: object
) -> None:
    repository.reconcile_error = RuntimeError("database unavailable")
    session = MeetingSession(repository, gateway, recovery_journal=journal)
    await _start_session(session)
    window = TranscriptWindow(source_epoch=1, segments=(
        NormalizedSegment(
            order=0,
            source_epoch=1,
            speaker_key="epoch:1:speaker:1",
            start_ms=0,
            end_ms=10,
            text="journal 错误",
        ),
    ))

    with pytest.raises(RuntimeError):
        await session._persist_window(session.active_meeting_id, window)

    gateway.abort_capture.assert_awaited_once_with()


async def test_gap_emits_reconnect_event_and_ignores_inactive_session(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    events: list[tuple[str, UUID, object]] = []

    async def publish(event_type: str, meeting_id: UUID, payload: object) -> None:
        events.append((event_type, meeting_id, payload))

    session = MeetingSession(repository, gateway, event_publisher=publish)
    await session._on_gap(SimpleNamespace(start_ms="10", end_ms="25"))
    assert events == []
    await _start_session(session)
    await session._on_gap(SimpleNamespace(start_ms="10", end_ms="25"))

    assert events[-1][0] == "transcription_gap"
    assert events[-1][2] == {"start_ms": 10, "end_ms": 25, "reason": "asr_reconnect"}


async def test_event_publisher_errors_are_logged_and_ignored(
    repository: FakeRepository, gateway: FakeGateway, caplog: pytest.LogCaptureFixture
) -> None:
    async def publish(*_args: object) -> None:
        raise RuntimeError("client disconnected")

    session = MeetingSession(repository, gateway, event_publisher=publish)
    await _start_session(session)

    assert session.active_meeting_id is not None
    assert "会议实时事件广播失败" in caplog.text


async def test_last_window_property_reads_gateway_snapshot(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    window = TranscriptWindow(source_epoch=1, partial="last")
    gateway._capture_last_window = window
    session = MeetingSession(repository, gateway)

    assert session.last_window == window


async def test_release_listener_is_safe_before_start(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    await MeetingSession(repository, gateway)._release_listener()

    assert gateway.listeners == []
    assert gateway.gap_listeners == []


async def test_start_storage_error_uses_stable_exception(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    repository.writable = False

    with pytest.raises(MeetingStorageUnavailableError) as error:
        await MeetingSession(repository, gateway).prepare_start("周会")

    assert error.value.code == "storage_unavailable"
