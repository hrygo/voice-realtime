"""会议转录持久化与 finalization 测试。

Task 2 覆盖 TranscriptPersistence 的去重/回放/降级路径（不使用真实 PostgreSQL）；
Task 3 追加 MeetingFinalizer 的严格调用顺序与失败清理测试。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from sona.meeting.finalization import MeetingFinalizer
from sona.meeting.models import (
    MeetingRecord,
    MeetingStatus,
    MinutesRecord,
    MinutesStatus,
    NormalizedSegment,
    TranscriptReconcileResult,
    TranscriptWindow,
)
from sona.meeting.persistence import TranscriptPersistence
from sona.meeting.ports import (
    CaptureFinalizationTimeoutError,
    RecoveryReplayRepository,
)


def _window(source_epoch: int = 1, text: str = "你好") -> TranscriptWindow:
    return TranscriptWindow(
        source_epoch=source_epoch,
        segments=(
            NormalizedSegment(
                id=uuid4(),
                order=0,
                source_epoch=source_epoch,
                speaker_key=f"epoch:{source_epoch}:speaker:0",
                start_ms=0,
                end_ms=100,
                text=text,
            ),
        ),
    )


def _record() -> MeetingRecord:
    now = datetime.now(UTC)
    return MeetingRecord(
        id=uuid4(),
        title="测试会议",
        status=MeetingStatus.RECORDING,
        language="Chinese",
        audio_source="microphone",
        started_at=now,
        ended_at=None,
        transcript_revision=0,
        content_revision=0,
        interruption_reason=None,
        metadata={},
        created_at=now,
        updated_at=now,
    )


def _minutes(meeting_id: UUID) -> MinutesRecord:
    now = datetime.now(UTC)
    return MinutesRecord(
        id=uuid4(),
        meeting_id=meeting_id,
        version=1,
        status=MinutesStatus.QUEUED,
        source_content_revision=0,
        model="test",
        prompt_version="test",
        content_json=None,
        content_markdown=None,
        raw_output=None,
        error_code=None,
        error_message=None,
        lease_until=None,
        attempts=0,
        created_at=now,
        generated_at=None,
        updated_at=now,
    )


class FakeTranscripts:
    """只实现 TranscriptStore.reconcile_window 的 fake。"""

    def __init__(self) -> None:
        self.calls: list[tuple[UUID, TranscriptWindow]] = []
        self.error: Exception | None = None

    async def reconcile_window(
        self, meeting_id: UUID, window: TranscriptWindow
    ) -> TranscriptReconcileResult:
        if self.error is not None:
            raise self.error
        self.calls.append((meeting_id, window))
        return TranscriptReconcileResult(
            meeting_id=meeting_id,
            transcript_revision=len(self.calls),
            content_revision=len(self.calls),
            replace_from_ms=0,
            segments=window.segments,
        )


class FakeJournal:
    """只实现 RecoveryJournalPort 的 fake。"""

    def __init__(self) -> None:
        self.appended: list[tuple[UUID, TranscriptWindow]] = []
        self.replayed: list[UUID] = []
        self.replay_count = 0
        self.error: Exception | None = None

    async def append(self, meeting_id: UUID, window: TranscriptWindow) -> object:
        if self.error is not None:
            raise self.error
        self.appended.append((meeting_id, window))
        return object()

    async def replay_meeting(
        self, repository: RecoveryReplayRepository, meeting_id: UUID
    ) -> int:
        if self.error is not None:
            raise self.error
        self.replayed.append(meeting_id)
        return self.replay_count


class RecordingReplayRepository:
    """记录回放调用顺序的 RecoveryReplayRepository fake。"""

    def __init__(self, transcripts: FakeTranscripts) -> None:
        self.order: list[str] = []
        self._transcripts = transcripts

    async def get_meeting(self, meeting_id: UUID) -> MeetingRecord | None:
        self.order.append("get_meeting")
        return None

    async def reconcile_window(
        self, meeting_id: UUID, window: TranscriptWindow
    ) -> TranscriptReconcileResult:
        self.order.append("reconcile_window")
        return await self._transcripts.reconcile_window(meeting_id, window)

    async def set_status(
        self, meeting_id: UUID, status: MeetingStatus, *, reason: str | None = None
    ) -> MeetingRecord:
        raise AssertionError("persistence 不调用 set_status")

    async def finalize_transcript(
        self,
        meeting_id: UUID,
        *,
        final_status: MeetingStatus = MeetingStatus.COMPLETED,
        reason: str | None = None,
    ) -> MeetingRecord:
        raise AssertionError("persistence 不调用 finalize_transcript")

    async def create_minutes(
        self, meeting_id: UUID, *, idempotency_key: str | None
    ) -> MinutesRecord:
        raise AssertionError("persistence 不调用 create_minutes")


def _persistence(
    transcripts: FakeTranscripts | None = None,
    journal: FakeJournal | None = None,
) -> tuple[TranscriptPersistence, FakeTranscripts, FakeJournal]:
    transcripts = transcripts or FakeTranscripts()
    journal = journal or FakeJournal()
    persistence = TranscriptPersistence(
        transcripts,
        journal=journal,
        replay_repository=RecordingReplayRepository(transcripts),
    )
    return persistence, transcripts, journal


async def test_reconcile_dedups_identical_window_signature() -> None:
    persistence, transcripts, _ = _persistence()
    meeting_id = uuid4()
    window = _window()

    first = await persistence.reconcile(meeting_id, window)
    second = await persistence.reconcile(meeting_id, window)

    assert first is not None
    assert second is None
    assert len(transcripts.calls) == 1
    assert persistence.degraded is False


async def test_reconcile_replays_pending_before_reconcile() -> None:
    transcripts = FakeTranscripts()
    journal = FakeJournal()
    journal.replay_count = 2
    persistence = TranscriptPersistence(
        transcripts,
        journal=journal,
        replay_repository=RecordingReplayRepository(transcripts),
    )
    meeting_id = uuid4()

    await persistence.reconcile(meeting_id, _window())

    assert journal.replayed == [meeting_id]
    assert len(transcripts.calls) == 1


async def test_repository_failure_writes_journal_and_returns_none() -> None:
    transcripts = FakeTranscripts()
    transcripts.error = RuntimeError("db down")
    persistence, _, journal = _persistence(transcripts=transcripts)
    meeting_id = uuid4()
    window = _window()

    result = await persistence.reconcile(meeting_id, window)

    assert result is None
    assert journal.appended == [(meeting_id, window)]
    assert persistence.degraded is True


async def test_journal_failure_raises() -> None:
    transcripts = FakeTranscripts()
    transcripts.error = RuntimeError("db down")
    journal = FakeJournal()
    journal.error = OSError("journal unwritable")
    persistence = TranscriptPersistence(
        transcripts,
        journal=journal,
        replay_repository=RecordingReplayRepository(transcripts),
    )

    with pytest.raises(OSError, match="journal unwritable"):
        await persistence.reconcile(uuid4(), _window())
    assert persistence.degraded is True


async def test_reconcile_clears_degraded_after_recovery() -> None:
    transcripts = FakeTranscripts()
    transcripts.error = RuntimeError("db down")
    persistence, _, _ = _persistence(transcripts=transcripts)
    meeting_id = uuid4()

    await persistence.reconcile(meeting_id, _window())
    assert persistence.degraded is True

    transcripts.error = None
    result = await persistence.reconcile(meeting_id, _window())
    assert result is not None
    assert persistence.degraded is False


async def test_signature_is_isolated_per_meeting() -> None:
    persistence, transcripts, _ = _persistence()
    first_meeting = uuid4()
    second_meeting = uuid4()
    window = _window()

    await persistence.reconcile(first_meeting, window)
    await persistence.reconcile(second_meeting, window)

    assert len(transcripts.calls) == 2


async def test_replay_pending_returns_count_and_clears_degraded() -> None:
    transcripts = FakeTranscripts()
    transcripts.error = RuntimeError("db down")
    persistence, _, journal = _persistence(transcripts=transcripts)
    meeting_id = uuid4()
    await persistence.reconcile(meeting_id, _window())
    assert persistence.degraded is True

    transcripts.error = None
    journal.replay_count = 1
    count = await persistence.replay_pending(meeting_id)

    assert count == 1
    assert persistence.degraded is False


async def test_replay_pending_without_journal_is_noop() -> None:
    persistence, _, _ = _persistence(journal=None)
    assert await persistence.replay_pending(uuid4()) == 0
    assert persistence.degraded is False


# ---------------------------------------------------------------------------
# Task 3: MeetingFinalizer 严格调用顺序与失败清理
# ---------------------------------------------------------------------------


class CallLogStore:
    """记录调用顺序的 transcript/speaker/minutes fake。"""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.errors: dict[str, Exception] = {}
        self.record = _record()

    async def reconcile_window(
        self, meeting_id: UUID, window: TranscriptWindow
    ) -> TranscriptReconcileResult:
        self._maybe_raise("reconcile_window")
        self.order.append("reconcile_window")
        return TranscriptReconcileResult(
            meeting_id=meeting_id,
            transcript_revision=1,
            content_revision=1,
            replace_from_ms=0,
            segments=window.segments,
        )

    async def apply_speaker_remapping(
        self, meeting_id: UUID, remapping: dict[str, str]
    ) -> MeetingRecord:
        self._maybe_raise("apply_speaker_remapping")
        self.order.append("apply_speaker_remapping")
        return self.record

    async def finalize_transcript(
        self,
        meeting_id: UUID,
        *,
        final_status: MeetingStatus = MeetingStatus.COMPLETED,
        reason: str | None = None,
    ) -> MeetingRecord:
        self._maybe_raise("finalize_transcript")
        self.order.append("finalize_transcript")
        self.record = self.record.model_copy(
            update={"status": final_status, "interruption_reason": reason}
        )
        return self.record

    async def create_minutes(
        self, meeting_id: UUID, *, idempotency_key: str | None
    ) -> MinutesRecord:
        self._maybe_raise("create_minutes")
        self.order.append("create_minutes")
        return _minutes(meeting_id)

    def _maybe_raise(self, name: str) -> None:
        error = self.errors.get(name)
        if error is not None:
            raise error


class NoopJournal:
    async def append(self, meeting_id: UUID, window: TranscriptWindow) -> object:
        return object()

    async def replay_meeting(
        self, repository: RecoveryReplayRepository, meeting_id: UUID
    ) -> int:
        return 0


def _finalizer(
    store: CallLogStore | None = None,
    gateway: AsyncMock | None = None,
    journal: object | None = None,
) -> tuple[MeetingFinalizer, CallLogStore, AsyncMock]:
    store = store or CallLogStore()
    gateway = gateway or AsyncMock(
        finish_capture=AsyncMock(return_value=TranscriptWindow(source_epoch=1))
    )
    persistence = TranscriptPersistence(store, journal=journal, replay_repository=store)
    finalizer = MeetingFinalizer(
        gateway=gateway,
        persistence=persistence,
        speakers=store,
        transcripts=store,
        minutes_store=store,
        timeout_secs=8.0,
    )
    return finalizer, store, gateway


async def test_finalize_normal_order_each_step_once() -> None:
    finalizer, store, gateway = _finalizer()
    meeting_id = uuid4()

    result = await finalizer.finalize(meeting_id)

    assert list(store.order) == [
        "reconcile_window",
        "finalize_transcript",
        "create_minutes",
    ]
    assert store.order.count("reconcile_window") == 1
    assert store.order.count("finalize_transcript") == 1
    assert store.order.count("create_minutes") == 1
    assert result.timed_out is False
    assert result.final_window is not None
    assert result.record.status is MeetingStatus.COMPLETED
    assert result.minutes.status is MinutesStatus.QUEUED
    gateway.finish_capture.assert_awaited_once_with(timeout_secs=8.0)
    gateway.abort_capture.assert_not_awaited()


async def test_finalize_typed_timeout_uses_last_window_and_finalizes_interrupted() -> None:
    last_window = TranscriptWindow(
        source_epoch=1, speaker_remap=(("epoch:1:speaker:spk_02", "epoch:1:speaker:spk_01"),)
    )
    gateway = AsyncMock(
        finish_capture=AsyncMock(
            side_effect=CaptureFinalizationTimeoutError(last_window)
        )
    )
    finalizer, store, gateway = _finalizer(gateway=gateway)
    meeting_id = uuid4()

    result = await finalizer.finalize(meeting_id)

    assert result.timed_out is True
    assert result.final_window is last_window
    assert result.record.status is MeetingStatus.INTERRUPTED
    assert result.record.interruption_reason == "finalization_timeout"
    assert "apply_speaker_remapping" in store.order
    assert store.order.index("apply_speaker_remapping") < store.order.index("finalize_transcript")
    gateway.abort_capture.assert_not_awaited()


async def test_finalize_finish_failure_aborts_capture_once() -> None:
    gateway = AsyncMock(
        finish_capture=AsyncMock(side_effect=RuntimeError("capture exploded"))
    )
    finalizer, store, _ = _finalizer(gateway=gateway)

    with pytest.raises(RuntimeError, match="capture exploded"):
        await finalizer.finalize(uuid4())

    gateway.abort_capture.assert_awaited_once_with()
    assert "finalize_transcript" not in store.order


async def test_finalize_downstream_failure_does_not_repeat_abort() -> None:
    store = CallLogStore()
    store.errors["finalize_transcript"] = RuntimeError("finalize failed")
    finalizer, _, gateway = _finalizer(store=store)

    with pytest.raises(RuntimeError, match="finalize failed"):
        await finalizer.finalize(uuid4())

    gateway.abort_capture.assert_not_awaited()
    assert store.order == ["reconcile_window"]


async def test_finalize_without_final_window_skips_reconcile_and_remap() -> None:
    gateway = AsyncMock(finish_capture=AsyncMock(return_value=None))
    finalizer, store, _ = _finalizer(gateway=gateway)

    result = await finalizer.finalize(uuid4())

    assert result.final_window is None
    assert "reconcile_window" not in store.order
    assert "apply_speaker_remapping" not in store.order
    assert store.order == ["finalize_transcript", "create_minutes"]


async def test_finalize_reconcile_failure_does_not_abort_capture() -> None:
    store = CallLogStore()
    store.errors["reconcile_window"] = RuntimeError("db down")
    finalizer, _, gateway = _finalizer(store=store, journal=None)

    with pytest.raises(RuntimeError, match="db down"):
        await finalizer.finalize(uuid4())

    gateway.abort_capture.assert_not_awaited()


async def test_finalize_cancellation_aborts_capture_once() -> None:
    never_finish = asyncio.Event()

    async def hang(*_args: object, **_kwargs: object) -> TranscriptWindow:
        await never_finish.wait()
        raise AssertionError("unreachable")

    gateway = AsyncMock(finish_capture=hang)
    finalizer, store, gateway = _finalizer(gateway=gateway)
    task = asyncio.create_task(finalizer.finalize(uuid4()))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    gateway.abort_capture.assert_awaited_once_with()
    assert "finalize_transcript" not in store.order


async def test_finalize_does_not_swallow_plain_timeout() -> None:
    gateway = AsyncMock(
        finish_capture=AsyncMock(side_effect=TimeoutError("socket timeout"))
    )
    finalizer, store, gateway = _finalizer(gateway=gateway)

    with pytest.raises(TimeoutError, match="socket timeout"):
        await finalizer.finalize(uuid4())

    gateway.abort_capture.assert_awaited_once_with()
    assert "finalize_transcript" not in store.order

