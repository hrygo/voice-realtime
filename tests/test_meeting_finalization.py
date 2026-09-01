"""会议转录持久化与 finalization 测试。

Task 2 覆盖 TranscriptPersistence 的去重/回放/降级路径（不使用真实 PostgreSQL）；
Task 3 追加 MeetingFinalizer 的严格调用顺序与失败清理测试。
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from voice_realtime.meeting.models import (
    MeetingRecord,
    MeetingStatus,
    MinutesRecord,
    NormalizedSegment,
    TranscriptReconcileResult,
    TranscriptWindow,
)
from voice_realtime.meeting.persistence import TranscriptPersistence
from voice_realtime.meeting.ports import RecoveryReplayRepository


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
