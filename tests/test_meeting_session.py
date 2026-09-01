"""MeetingSession 生命周期测试。"""

from __future__ import annotations

import asyncio
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
from voice_realtime.meeting.ports import CaptureFinalizationTimeoutError
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
        self.last_window: TranscriptWindow | None = None
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
        f"meeting:{preparation.record.id}", timeout_secs=5.0, speaker_count_hint=None
    )
    gateway.commit_capture.assert_not_called()
    publish.assert_not_awaited()


async def test_prepare_passes_max_speakers_as_a_speechrail_hint(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    session = MeetingSession(repository, gateway, event_publisher=AsyncMock())
    preparation = await session.prepare_start("1v1", max_speakers=2)

    gateway.prepare_capture.assert_awaited_once_with(
        f"meeting:{preparation.record.id}",
        timeout_secs=5.0,
        speaker_count_hint=2,
    )


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


async def test_stop_applies_speechrail_final_speaker_remapping(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    gateway.finish_capture.return_value = TranscriptWindow(
        source_epoch=1, speaker_remap=(("epoch:1:speaker:spk_02", "epoch:1:speaker:spk_01"),)
    )
    repository.apply_speaker_remapping = AsyncMock(return_value=MeetingRecord(title="聚类后"))

    session = MeetingSession(repository, gateway)
    await _start_session(session)
    result = await session.stop()

    repository.apply_speaker_remapping.assert_awaited_once_with(
        result.id, {"epoch:1:speaker:spk_02": "epoch:1:speaker:spk_01"}
    )



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


async def test_reconciled_event_uses_renamed_speaker_display_name(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    """回归测试：重命名说话人后，后续 transcript_reconciled 事件中的 speaker_name
    应使用自定义名称而非默认的 '说话人 X'。"""
    events: list[tuple[str, UUID, object]] = []

    async def publish(event_type: str, meeting_id: UUID, payload: object) -> None:
        events.append((event_type, meeting_id, payload))

    # 为 FakeRepository 添加 get_speakers 方法，模拟已重命名的说话人
    renamed_speakers: list[SimpleNamespace] = []

    async def get_speakers(meeting_id: UUID) -> tuple[SimpleNamespace, ...]:
        return tuple(renamed_speakers)

    repository.get_speakers = get_speakers  # type: ignore[attr-defined]

    session = MeetingSession(repository, gateway, event_publisher=publish)
    await _start_session(session)

    # 第一次窗口：尚未重命名，应使用默认名
    seg1 = NormalizedSegment(
        order=0,
        source_epoch=1,
        speaker_key="epoch:1:speaker:1",
        start_ms=0,
        end_ms=1000,
        text="第一段",
    )
    await session._on_window(
        TranscriptWindow(source_epoch=1, segments=(seg1,))
    )
    reconciled = [e[2] for e in events if e[0] == "transcript_reconciled"][-1]
    assert reconciled["segments"][0]["speaker_name"] == "说话人 1"

    # 模拟用户重命名说话人（通过 API→repository.rename_speaker 路径）
    renamed_speakers.append(
        SimpleNamespace(
            speaker_key="epoch:1:speaker:1",
            display_name="张总",
            default_label="说话人 1",
        )
    )

    # 第二次窗口：重命名后，应使用自定义名称
    seg2 = NormalizedSegment(
        order=1,
        source_epoch=1,
        speaker_key="epoch:1:speaker:1",
        start_ms=2000,
        end_ms=3000,
        text="第二段",
    )
    events.clear()
    await session._on_window(
        TranscriptWindow(source_epoch=1, segments=(seg2,))
    )
    reconciled = [e[2] for e in events if e[0] == "transcript_reconciled"][-1]
    assert reconciled["segments"][0]["speaker_name"] == "张总"


async def test_partial_event_uses_renamed_speaker_from_cache(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    """回归测试：重命名说话人后，partial 事件应使用缓存的自定义名称。"""
    events: list[tuple[str, UUID, object]] = []

    async def publish(event_type: str, meeting_id: UUID, payload: object) -> None:
        events.append((event_type, meeting_id, payload))

    async def get_speakers(meeting_id: UUID) -> tuple[SimpleNamespace, ...]:
        return (
            SimpleNamespace(
                speaker_key="epoch:1:speaker:1",
                display_name="张总",
                default_label="说话人 1",
            ),
        )

    repository.get_speakers = get_speakers  # type: ignore[attr-defined]

    session = MeetingSession(repository, gateway, event_publisher=publish)
    await _start_session(session)

    # 先触发一次有 segments 的窗口以加载 speaker_names 缓存
    seg = NormalizedSegment(
        order=0, source_epoch=1, speaker_key="epoch:1:speaker:1",
        start_ms=0, end_ms=1000, text="加载缓存",
    )
    await session._on_window(
        TranscriptWindow(source_epoch=1, segments=(seg,))
    )

    # 再触发纯 partial 窗口（无 speaker_name），应使用缓存的 "张总"
    events.clear()
    await session._on_window(
        TranscriptWindow(
            source_epoch=2,
            partial="正在说话",
            partial_speaker_key="epoch:1:speaker:1",
        )
    )
    partial = next(e[2] for e in events if e[0] == "transcript_partial")
    assert partial["speaker_name"] == "张总"


async def test_partial_event_preserves_known_speaker_identity(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    events: list[tuple[str, UUID, object]] = []

    async def publish(event_type: str, meeting_id: UUID, payload: object) -> None:
        events.append((event_type, meeting_id, payload))

    session = MeetingSession(repository, gateway, event_publisher=publish)
    await _start_session(session)

    await session._on_window(
        TranscriptWindow(
            source_epoch=1,
            partial="正在确认",
            partial_speaker_key="epoch:1:speaker:1",
            partial_speaker_name="主持人",
        )
    )

    partial = next(event[2] for event in events if event[0] == "transcript_partial")
    assert partial == {
        "text": "正在确认",
        "speaker_key": "epoch:1:speaker:1",
        "speaker_name": "主持人",
    }


async def test_partial_event_keeps_unknown_speaker_null(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    events: list[tuple[str, UUID, object]] = []

    async def publish(event_type: str, meeting_id: UUID, payload: object) -> None:
        events.append((event_type, meeting_id, payload))

    session = MeetingSession(repository, gateway, event_publisher=publish)
    await _start_session(session)
    await session._on_window(TranscriptWindow(source_epoch=1, partial="未知说话人"))

    partial = next(event[2] for event in events if event[0] == "transcript_partial")
    assert partial == {
        "text": "未知说话人",
        "speaker_key": None,
        "speaker_name": None,
    }


async def test_partial_event_does_not_echo_opaque_speaker_key(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    events: list[tuple[str, UUID, object]] = []

    async def publish(event_type: str, meeting_id: UUID, payload: object) -> None:
        events.append((event_type, meeting_id, payload))

    session = MeetingSession(repository, gateway, event_publisher=publish)
    await _start_session(session)
    await session._on_window(
        TranscriptWindow(
            source_epoch=1,
            partial="尚未命名",
            partial_speaker_key="opaque-internal-key",
        )
    )

    partial = next(event[2] for event in events if event[0] == "transcript_partial")
    assert partial == {
        "text": "尚未命名",
        "speaker_key": "opaque-internal-key",
        "speaker_name": None,
    }


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


async def test_start_supports_sync_summary_requeue(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    summary = SimpleNamespace(requeue_for_recording=AsyncMock())
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
    timeout = CaptureFinalizationTimeoutError(last_window)
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


async def test_stop_failure_marks_interrupted_and_releases_listeners(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    session = MeetingSession(repository, gateway)
    await _start_session(session)
    gateway.finish_capture.side_effect = RuntimeError("flush failed")

    with pytest.raises(RuntimeError, match="failed"):
        await session.stop()

    assert session.active_meeting_id is None
    assert gateway.listeners == []
    assert gateway.gap_listeners == []
    assert repository.calls[-1] == "interrupted"


async def test_stop_minutes_failure_does_not_overwrite_completed_record(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    session = MeetingSession(repository, gateway)
    await _start_session(session)
    repository.minutes_error = RuntimeError("minutes failed")

    with pytest.raises(RuntimeError, match="minutes failed"):
        await session.stop()

    assert repository.record.status is MeetingStatus.COMPLETED
    assert session.record is repository.record
    assert "interrupted" not in repository.calls
    assert session.active_meeting_id is None
    assert gateway.listeners == []
    assert gateway.gap_listeners == []


async def test_stop_finalizing_failure_aborts_capture_and_persists_interrupted(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    resume = AsyncMock()
    summary = SimpleNamespace(resume_after_recording=resume)
    session = MeetingSession(repository, gateway, summary_service=summary)
    await _start_session(session)
    original_set_status = repository.set_status

    async def fail_finalizing_once(meeting_id, status, *, reason=None):
        if status is MeetingStatus.FINALIZING:
            raise RuntimeError("finalizing unavailable")
        return await original_set_status(meeting_id, status, reason=reason)

    repository.set_status = AsyncMock(side_effect=fail_finalizing_once)

    with pytest.raises(RuntimeError, match="finalizing"):
        await session.stop()

    gateway.finish_capture.assert_not_awaited()
    gateway.abort_capture.assert_awaited_once_with()
    assert gateway.listeners == []
    assert gateway.gap_listeners == []
    assert session.active_meeting_id is None
    assert session.record is repository.record
    assert session.record is not None
    assert session.record.status is MeetingStatus.INTERRUPTED
    assert repository.calls[-1] == "interrupted"
    resume.assert_awaited_once_with()


async def test_stop_finalizing_and_interrupted_write_fail_still_closes_capture(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    resume = AsyncMock()
    summary = SimpleNamespace(resume_after_recording=resume)
    session = MeetingSession(repository, gateway, summary_service=summary)
    await _start_session(session)
    repository.status_error = RuntimeError("storage unavailable")

    with pytest.raises(RuntimeError, match="storage"):
        await session.stop()

    gateway.finish_capture.assert_not_awaited()
    gateway.abort_capture.assert_awaited_once_with()
    assert gateway.listeners == []
    assert gateway.gap_listeners == []
    assert session.active_meeting_id is None
    assert session.record is not None
    assert session.record.status is MeetingStatus.INTERRUPTED
    assert session.storage_health is StorageHealth.DEGRADED
    resume.assert_awaited_once_with()


async def test_stop_cancellation_marks_interrupted_and_releases_capture(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    resume = AsyncMock()
    summary = SimpleNamespace(resume_after_recording=resume)
    session = MeetingSession(repository, gateway, summary_service=summary)
    await _start_session(session)
    finalizing_started = asyncio.Event()
    never_finish = asyncio.Event()
    original_set_status = repository.set_status

    async def block_finalizing(meeting_id, status, *, reason=None):
        if status is MeetingStatus.FINALIZING:
            finalizing_started.set()
            await never_finish.wait()
        return await original_set_status(meeting_id, status, reason=reason)

    repository.set_status = AsyncMock(side_effect=block_finalizing)
    stopping = asyncio.create_task(session.stop())
    await finalizing_started.wait()

    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping

    gateway.finish_capture.assert_not_awaited()
    gateway.abort_capture.assert_awaited_once_with()
    assert gateway.listeners == []
    assert gateway.gap_listeners == []
    assert session.active_meeting_id is None
    assert session.record is repository.record
    assert session.record is not None
    assert session.record.status is MeetingStatus.INTERRUPTED
    resume.assert_awaited_once_with()


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


async def test_interrupt_status_failure_releases_local_session_and_allows_restart(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    resume = AsyncMock()
    summary = SimpleNamespace(resume_after_recording=resume)
    session = MeetingSession(repository, gateway, summary_service=summary)
    await _start_session(session)
    interrupted_reason = "database unavailable"
    repository.status_error = RuntimeError(interrupted_reason)

    with pytest.raises(RuntimeError, match=interrupted_reason):
        await session.interrupt(interrupted_reason)

    gateway.abort_capture.assert_awaited_once_with()
    assert gateway.listeners == []
    assert gateway.gap_listeners == []
    assert session.active_meeting_id is None
    assert session._committed_preparation is None
    assert session.record is not None
    assert session.record.status is MeetingStatus.INTERRUPTED
    assert session.record.interruption_reason == interrupted_reason
    assert session.storage_health is StorageHealth.DEGRADED
    resume.assert_awaited_once_with()

    repository.status_error = None
    restarted = await _start_session(session, "恢复后的会议")

    assert session.active_meeting_id == restarted.id
    assert restarted.status is MeetingStatus.RECORDING


async def test_interrupt_cancellation_waits_for_cleanup_before_restart(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    resume_started = asyncio.Event()
    allow_resume = asyncio.Event()
    events: list[str] = []

    async def resume_after_recording() -> None:
        events.append("resume_started")
        resume_started.set()
        await allow_resume.wait()
        events.append("resume_finished")

    async def prepare_next(session: MeetingSession):
        preparation = await session.prepare_start("取消后的会议")
        events.append("prepare_finished")
        return preparation

    summary = SimpleNamespace(resume_after_recording=resume_after_recording)
    session = MeetingSession(repository, gateway, summary_service=summary)
    await _start_session(session)
    interrupting = asyncio.create_task(session.interrupt("调用方取消"))
    await resume_started.wait()

    interrupting.cancel("first cancellation")
    preparing = asyncio.create_task(prepare_next(session))
    interrupt_result: asyncio.CancelledError | None = None
    try:
        await asyncio.sleep(0)
        assert not interrupting.done()
        assert not preparing.done()
        interrupting.cancel("second cancellation")
        await asyncio.sleep(0)
        assert not interrupting.done()
        assert not preparing.done()
    finally:
        allow_resume.set()
        try:
            await interrupting
        except asyncio.CancelledError as exc:
            interrupt_result = exc
        finally:
            preparation = await preparing

    assert isinstance(interrupt_result, asyncio.CancelledError)
    assert interrupt_result.args == ("first cancellation",)
    assert events == ["resume_started", "resume_finished", "prepare_finished"]
    restarted = session.commit_start(preparation)
    await session.publish_started(preparation)
    assert session.active_meeting_id == restarted.id


async def test_interrupt_cleanup_failure_takes_priority_over_cancellation(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    cleanup_started = asyncio.Event()
    allow_cleanup_failure = asyncio.Event()
    cleanup_tasks: list[asyncio.Task[None]] = []

    async def fail_cleanup() -> None:
        cleanup_task = asyncio.current_task()
        assert cleanup_task is not None
        cleanup_tasks.append(cleanup_task)
        cleanup_started.set()
        await allow_cleanup_failure.wait()
        raise RuntimeError("cleanup failed")

    session = MeetingSession(repository, gateway)
    await _start_session(session)
    session._release_stopped_session = fail_cleanup  # type: ignore[method-assign]
    interrupting = asyncio.create_task(session.interrupt("调用方取消"))
    await cleanup_started.wait()

    interrupting.cancel("caller cancelled")
    allow_cleanup_failure.set()
    try:
        with pytest.raises(RuntimeError, match="cleanup failed") as exc_info:
            await interrupting
    finally:
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)

    assert isinstance(exc_info.value.__cause__, asyncio.CancelledError)
    assert exc_info.value.__cause__.args == ("caller cancelled",)


async def test_interrupt_preserves_body_cancellation_over_cleanup_cancellation(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    status_started = asyncio.Event()
    allow_status = asyncio.Event()
    resume_started = asyncio.Event()
    allow_resume = asyncio.Event()

    async def gated_set_status(
        meeting_id: UUID, status: MeetingStatus, *, reason: str | None = None
    ) -> MeetingRecord:
        status_started.set()
        await allow_status.wait()
        return repository.record

    async def resume_after_recording() -> None:
        resume_started.set()
        await allow_resume.wait()

    summary = SimpleNamespace(resume_after_recording=resume_after_recording)
    session = MeetingSession(repository, gateway, summary_service=summary)
    await _start_session(session)
    repository.set_status = gated_set_status  # type: ignore[method-assign]
    interrupting = asyncio.create_task(session.interrupt("调用方取消"))
    await status_started.wait()

    interrupting.cancel("first")
    await resume_started.wait()
    interrupting.cancel("second")
    try:
        await asyncio.sleep(0)
        assert not interrupting.done()
    finally:
        allow_status.set()
        allow_resume.set()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await interrupting

    assert exc_info.value.args == ("first",)


async def test_interrupt_cleanup_failure_chains_body_cancellation(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    status_started = asyncio.Event()
    allow_status = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup_failure = asyncio.Event()

    async def gated_set_status(
        meeting_id: UUID, status: MeetingStatus, *, reason: str | None = None
    ) -> MeetingRecord:
        status_started.set()
        await allow_status.wait()
        return repository.record

    async def fail_cleanup() -> None:
        cleanup_started.set()
        await allow_cleanup_failure.wait()
        raise RuntimeError("cleanup failed after body cancellation")

    session = MeetingSession(repository, gateway)
    await _start_session(session)
    repository.set_status = gated_set_status  # type: ignore[method-assign]
    session._release_stopped_session = fail_cleanup  # type: ignore[method-assign]
    interrupting = asyncio.create_task(session.interrupt("调用方取消"))
    await status_started.wait()

    interrupting.cancel("first")
    await cleanup_started.wait()
    allow_status.set()
    allow_cleanup_failure.set()

    with pytest.raises(
        RuntimeError, match="cleanup failed after body cancellation"
    ) as exc_info:
        await interrupting

    assert isinstance(exc_info.value.__cause__, asyncio.CancelledError)
    assert exc_info.value.__cause__.args == ("first",)


async def test_recover_stale_delegates_to_repository(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    session = MeetingSession(repository, gateway)
    repository.stale_count = 3
    assert await session.recover_stale() == 3


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

        async def append(self, meeting_id: UUID, window: TranscriptWindow) -> None:
            self.calls.append((meeting_id, window))

        async def replay_meeting(self, target_repository, meeting_id: UUID) -> int:
            return 0

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

    await session._on_window(window)
    assert session.storage_health is StorageHealth.DEGRADED
    assert journal.calls == [(session.active_meeting_id, window)]
    gateway.abort_capture.assert_not_awaited()


async def test_window_recovery_replays_journal_before_next_window_and_stop(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
    class ReplayJournal:
        def __init__(self) -> None:
            self.pending: list[tuple[UUID, TranscriptWindow]] = []
            self.replay_calls = 0

        async def append(self, meeting_id: UUID, window: TranscriptWindow) -> None:
            self.pending.append((meeting_id, window))

        async def replay_meeting(self, target_repository, meeting_id: UUID) -> int:
            self.replay_calls += 1
            pending = [window for target_id, window in self.pending if target_id == meeting_id]
            self.pending = [
                (target_id, window)
                for target_id, window in self.pending
                if target_id != meeting_id
            ]
            for window in pending:
                await target_repository.reconcile_window(meeting_id, window)
            return len(pending)

    journal = ReplayJournal()
    repository.reconcile_error = RuntimeError("database unavailable")
    session = MeetingSession(repository, gateway, recovery_journal=journal)
    await _start_session(session)
    first_window = TranscriptWindow(
        source_epoch=1,
        segments=(
            NormalizedSegment(
                order=0,
                source_epoch=1,
                speaker_key="epoch:1:speaker:1",
                start_ms=0,
                end_ms=10,
                text="先写入 journal",
            ),
        ),
    )
    second_window = first_window.model_copy(
        update={
            "source_epoch": 2,
            "segments": (
                first_window.segments[0].model_copy(
                    update={"source_epoch": 2, "text": "恢复后写入"}
                ),
            ),
        }
    )

    await session._on_window(first_window)
    repository.reconcile_error = None
    await session._on_window(second_window)

    assert journal.replay_calls == 2
    assert journal.pending == []
    assert repository.calls[-2:] == ["reconcile", "reconcile"]

    await session.stop()
    assert journal.pending == []
    assert journal.replay_calls == 4


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
        await session._on_window(window)

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
    summary = SimpleNamespace(resume_after_recording=AsyncMock())
    session = MeetingSession(repository, gateway, summary_service=summary)
    await _start_session(session)

    result = await session.interrupt("测试结束")

    assert result is not None


class FailingJournal:
    async def append(self, meeting_id: UUID, window: TranscriptWindow) -> None:
        raise RuntimeError("journal failed")

    async def replay_meeting(self, target_repository, meeting_id: UUID) -> int:
        return 0


async def test_window_persistence_failure_with_unusable_journal_aborts_capture(
    repository: FakeRepository, gateway: FakeGateway
) -> None:
        repository.reconcile_error = RuntimeError("database unavailable")
        session = MeetingSession(repository, gateway, recovery_journal=FailingJournal())
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

        with pytest.raises(RuntimeError, match="journal failed"):
            await session._on_window(window)

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
    await session._on_gap(SimpleNamespace(start_ms=10, end_ms=25))

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
    gateway.last_window = window
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
