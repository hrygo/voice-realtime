"""会议窄 ports 契约测试。

fakes 只实现各自窄 protocol；证明 finalizer 不需要 list/update/delete、
API 不需要 capture、RecoveryJournal 只需要 replay 操作涉及的方法。
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID, uuid4

from sona.meeting import ports
from sona.meeting.models import (
    MeetingPage,
    MeetingRecord,
    MeetingStatus,
    MinutesJob,
    MinutesRecord,
    MinutesResult,
    MinutesStatus,
    SpeakerRecord,
    TranscriptDocument,
    TranscriptReconcileResult,
    TranscriptWindow,
)
from sona.meeting.ports import (
    CaptureFinalizationTimeout,
    CaptureFinalizationTimeoutError,
    CaptureGap,
    CaptureLease,
    ClosableStore,
    MeetingCaptureGateway,
    MeetingRepository,
    MeetingStore,
    MinutesStore,
    RecoveryReplayRepository,
    RepositoryMaintenance,
    SpeakerStore,
    TranscriptStore,
)
from sona.subtitles.proxy import SubtitleProxy


def _window() -> TranscriptWindow:
    return TranscriptWindow(source_epoch=1, segments=())


def _record() -> MeetingRecord:
    return MeetingRecord(
        id=uuid4(),
        title="测试会议",
        status=MeetingStatus.RECORDING,
        language="Chinese",
        audio_source="microphone",
        started_at=None,
        ended_at=None,
        transcript_revision=0,
        content_revision=0,
        interruption_reason=None,
        metadata={},
        created_at=None,
        updated_at=None,
    )


def _protocol_members(protocol: type[object]) -> set[str]:
    members: set[str] = set()
    for klass in reversed(protocol.__mro__):
        if klass is object or klass is Protocol:
            continue
        members.update(name for name in vars(klass) if not name.startswith("_"))
    return members


def assert_satisfies(instance: object, protocol: type[object]) -> None:
    """结构性检查：instance 实现了 protocol 声明的全部非私有成员。"""
    available = {name for name in dir(instance) if not name.startswith("_")}
    missing = _protocol_members(protocol) - available
    assert not missing, (
        f"{type(instance).__name__} 不满足 {protocol.__name__}，缺少: {sorted(missing)}"
    )


class FakeMeetingStore:
    """只实现 MeetingStore 的 fake。"""

    async def check_writable(self) -> bool:
        return True

    async def create_meeting(
        self, title: str, *, language: str, audio_source: str
    ) -> MeetingRecord:
        return _record()

    async def get_meeting(self, meeting_id: UUID) -> MeetingRecord | None:
        return None

    async def list_meetings(self, *, cursor: str | None, limit: int) -> MeetingPage:
        return MeetingPage(items=(), next_cursor=None)

    async def update_title(self, meeting_id: UUID, title: str) -> MeetingRecord:
        return _record()

    async def set_status(
        self, meeting_id: UUID, status: MeetingStatus, *, reason: str | None = None
    ) -> MeetingRecord:
        return _record()

    async def delete_meeting(self, meeting_id: UUID) -> None:
        return None


class FakeTranscriptStore:
    """只实现 TranscriptStore 的 fake。"""

    async def reconcile_window(
        self, meeting_id: UUID, window: TranscriptWindow
    ) -> TranscriptReconcileResult:
        return TranscriptReconcileResult(
            meeting_id=meeting_id,
            transcript_revision=1,
            content_revision=1,
            replace_from_ms=0,
            segments=window.segments,
        )

    async def finalize_transcript(
        self,
        meeting_id: UUID,
        *,
        final_status: MeetingStatus = MeetingStatus.COMPLETED,
        reason: str | None = None,
    ) -> MeetingRecord:
        return _record()

    async def get_transcript(self, meeting_id: UUID) -> TranscriptDocument:
        return TranscriptDocument(
            meeting_id=meeting_id,
            transcript_revision=0,
            content_revision=0,
            segments=(),
            speakers=(),
        )


class FakeSpeakerStore:
    """只实现 SpeakerStore 的 fake。"""

    async def get_speakers(self, meeting_id: UUID) -> tuple[SpeakerRecord, ...]:
        return ()

    async def rename_speaker(
        self, meeting_id: UUID, speaker_key: str, display_name: str
    ) -> MeetingRecord:
        return _record()

    async def apply_speaker_remapping(
        self, meeting_id: UUID, remapping: dict[str, str]
    ) -> MeetingRecord:
        return _record()


class FakeMinutesStore:
    """只实现 MinutesStore 的 fake。"""

    async def get_latest_minutes(self, meeting_id: UUID) -> MinutesRecord | None:
        return None

    async def get_minutes(self, meeting_id: UUID, version: int) -> MinutesRecord | None:
        return None

    async def create_minutes(
        self, meeting_id: UUID, *, idempotency_key: str | None
    ) -> MinutesRecord:
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
            created_at=None,
            generated_at=None,
            updated_at=None,
        )

    async def claim_minutes(self) -> MinutesJob | None:
        return None

    async def complete_minutes(
        self, minutes_id: UUID, result: MinutesResult
    ) -> MinutesRecord:
        raise AssertionError("finalizer 不调用 complete_minutes")

    async def fail_minutes(
        self,
        minutes_id: UUID,
        *,
        code: str,
        message: str,
        raw_output: str | None = None,
    ) -> None:
        raise AssertionError("finalizer 不调用 fail_minutes")


class FakeReplayRepository:
    """只实现 RecoveryReplayRepository 的 fake。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_meeting(self, meeting_id: UUID) -> MeetingRecord | None:
        self.calls.append("get_meeting")
        return None

    async def reconcile_window(
        self, meeting_id: UUID, window: TranscriptWindow
    ) -> TranscriptReconcileResult:
        self.calls.append("reconcile_window")
        return TranscriptReconcileResult(
            meeting_id=meeting_id,
            transcript_revision=1,
            content_revision=1,
            replace_from_ms=0,
            segments=window.segments,
        )

    async def set_status(
        self, meeting_id: UUID, status: MeetingStatus, *, reason: str | None = None
    ) -> MeetingRecord:
        self.calls.append("set_status")
        return _record()

    async def finalize_transcript(
        self,
        meeting_id: UUID,
        *,
        final_status: MeetingStatus = MeetingStatus.COMPLETED,
        reason: str | None = None,
    ) -> MeetingRecord:
        self.calls.append("finalize_transcript")
        return _record()

    async def create_minutes(
        self, meeting_id: UUID, *, idempotency_key: str | None
    ) -> MinutesRecord:
        self.calls.append("create_minutes")
        return FakeMinutesStore().create_minutes(meeting_id, idempotency_key=idempotency_key)


def test_meeting_store_only_requires_meeting_crud() -> None:
    assert_satisfies(FakeMeetingStore(), MeetingStore)
    # list/update/delete 之外的窄 protocol 不应被 meeting fake 满足
    assert not _protocol_members(MeetingStore) & _protocol_members(TranscriptStore)


def test_transcript_store_only_requires_transcript_methods() -> None:
    assert_satisfies(FakeTranscriptStore(), TranscriptStore)


def test_speaker_store_only_requires_speaker_methods() -> None:
    assert_satisfies(FakeSpeakerStore(), SpeakerStore)


def test_minutes_store_only_requires_minutes_methods() -> None:
    assert_satisfies(FakeMinutesStore(), MinutesStore)


def test_finalizer_shaped_consumer_needs_no_crud() -> None:
    """finalizer 形状的消费者只用 transcript/speaker/minutes 三个窄端口。"""

    async def finalize(
        transcripts: TranscriptStore,
        speakers: SpeakerStore,
        minutes: MinutesStore,
        meeting_id: UUID,
    ) -> None:
        await transcripts.reconcile_window(meeting_id, _window())
        await speakers.apply_speaker_remapping(meeting_id, {})
        await transcripts.finalize_transcript(meeting_id)
        await minutes.create_minutes(meeting_id, idempotency_key=f"meeting:{meeting_id}:minutes:v1")

    assert_satisfies(FakeTranscriptStore(), TranscriptStore)
    assert_satisfies(FakeSpeakerStore(), SpeakerStore)
    assert_satisfies(FakeMinutesStore(), MinutesStore)


async def test_recovery_replay_repository_only_combines_replay_methods() -> None:
    assert_satisfies(FakeReplayRepository(), RecoveryReplayRepository)
    expected = {
        "get_meeting",
        "reconcile_window",
        "set_status",
        "finalize_transcript",
        "create_minutes",
    }
    assert _protocol_members(RecoveryReplayRepository) == expected
    # 窄 replay 端口不包含 list/update/delete/claim/complete
    assert not _protocol_members(RecoveryReplayRepository) & {
        "list_meetings",
        "update_title",
        "delete_meeting",
        "claim_minutes",
        "complete_minutes",
        "fail_minutes",
    }


def test_repository_aggregate_covers_narrow_ports() -> None:
    assert set(_protocol_members(MeetingRepository)) == (
        _protocol_members(MeetingStore)
        | _protocol_members(TranscriptStore)
        | _protocol_members(SpeakerStore)
        | _protocol_members(MinutesStore)
        | _protocol_members(RepositoryMaintenance)
        | _protocol_members(ClosableStore)
    )


def test_capture_gateway_protocol_shape() -> None:
    members = _protocol_members(MeetingCaptureGateway)
    assert {
        "last_window",
        "add_event_listener",
        "remove_event_listener",
        "add_gap_listener",
        "remove_gap_listener",
        "prepare_capture",
        "commit_capture",
        "abort_prepared_capture",
        "finish_capture",
        "abort_capture",
    } == members


def test_capture_value_types_are_aliased_by_proxy() -> None:
    assert ports.CaptureLease is CaptureLease
    assert ports.CaptureGap is CaptureGap
    assert ports.CaptureFinalizationTimeout is CaptureFinalizationTimeout
    assert CaptureFinalizationTimeout is CaptureFinalizationTimeoutError
    # SubtitleProxy 归位属于 sona.subtitles 领域模块
    assert SubtitleProxy.__module__ == "sona.subtitles.proxy"


def test_proxy_capture_types_alias_ports() -> None:
    from sona.subtitles import proxy as proxy_module
    from sona.subtitles import sessions as sessions_module

    assert proxy_module.CapturePreparation is ports.CaptureLease
    assert proxy_module.TranscriptionGap is ports.CaptureGap
    assert proxy_module.FinalizationTimeoutError is ports.CaptureFinalizationTimeout
    assert sessions_module.CapturePreparation is ports.CaptureLease
    assert sessions_module.TranscriptionGap is ports.CaptureGap
    assert sessions_module.FinalizationTimeoutError is ports.CaptureFinalizationTimeout
