"""会议领域窄 ports：capture gateway 与 repository 消费面。

本模块只声明 application 所需能力，不包含 SQL、状态或实现。
``PostgresMeetingRepository`` 可实现多个窄 protocol，但 SQL 与 transaction
仍留在具体方法中；``RecoveryJournal`` 是独立 transient component，不混入
PostgreSQL CRUD。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from .models import (
    MeetingPage,
    MeetingRecord,
    MeetingStatus,
    MinutesJob,
    MinutesRecord,
    MinutesResult,
    SpeakerRecord,
    TranscriptDocument,
    TranscriptReconcileResult,
    TranscriptWindow,
)

WindowListener = Callable[[TranscriptWindow], Awaitable[None]]
GapListener = Callable[["CaptureGap"], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class CaptureLease:
    """会议采集连接已 ready、尚未接收 PCM 的一次性凭证。"""

    owner: str
    generation: int


@dataclass(frozen=True)
class CaptureGap:
    """SpeechRail 重连期间无法转录的样本时钟区间。"""

    source_epoch: int
    start_ms: int
    end_ms: int


class CaptureFinalizationTimeoutError(TimeoutError):
    """会议 ASR 未在时限内排空 PCM 或完成 EOF，携带最后已知窗口。"""

    code = "finalization_timeout"

    def __init__(self, last_window: TranscriptWindow | None) -> None:
        self.last_window = last_window
        super().__init__("SpeechRail finalization timed out")


CaptureFinalizationTimeout = CaptureFinalizationTimeoutError


class MeetingCaptureGateway(Protocol):
    """MeetingSession 需要的 capture 窄端口。"""

    @property
    def last_window(self) -> TranscriptWindow | None: ...

    def add_event_listener(self, listener: WindowListener) -> None: ...

    def remove_event_listener(self, listener: WindowListener) -> None: ...

    def add_gap_listener(self, listener: GapListener) -> None: ...

    def remove_gap_listener(self, listener: GapListener) -> None: ...

    async def prepare_capture(
        self,
        owner: str,
        *,
        timeout_secs: float,
        speaker_count_hint: int | None,
    ) -> CaptureLease: ...

    def commit_capture(self, preparation: CaptureLease) -> None: ...

    async def abort_prepared_capture(self, preparation: CaptureLease) -> None: ...

    async def finish_capture(self, *, timeout_secs: float) -> TranscriptWindow: ...

    async def abort_capture(self) -> None: ...


class MeetingStore(Protocol):
    """会议元数据 CRUD 消费面。"""

    async def check_writable(self) -> bool: ...

    async def create_meeting(
        self, title: str, *, language: str, audio_source: str
    ) -> MeetingRecord: ...

    async def get_meeting(self, meeting_id: UUID) -> MeetingRecord | None: ...

    async def list_meetings(self, *, cursor: str | None, limit: int) -> MeetingPage: ...

    async def update_title(self, meeting_id: UUID, title: str) -> MeetingRecord: ...

    async def set_status(
        self, meeting_id: UUID, status: MeetingStatus, *, reason: str | None = None
    ) -> MeetingRecord: ...

    async def delete_meeting(self, meeting_id: UUID) -> None: ...


class TranscriptStore(Protocol):
    """转录对账与封存消费面。"""

    async def reconcile_window(
        self, meeting_id: UUID, window: TranscriptWindow
    ) -> TranscriptReconcileResult: ...

    async def finalize_transcript(
        self,
        meeting_id: UUID,
        *,
        final_status: MeetingStatus = MeetingStatus.COMPLETED,
        reason: str | None = None,
    ) -> MeetingRecord: ...

    async def get_transcript(self, meeting_id: UUID) -> TranscriptDocument: ...


class SpeakerStore(Protocol):
    """说话人映射消费面。"""

    async def get_speakers(self, meeting_id: UUID) -> tuple[SpeakerRecord, ...]: ...

    async def rename_speaker(
        self, meeting_id: UUID, speaker_key: str, display_name: str
    ) -> MeetingRecord: ...

    async def apply_speaker_remapping(
        self, meeting_id: UUID, remapping: dict[str, str]
    ) -> MeetingRecord: ...


class MinutesStore(Protocol):
    """纪要生命周期消费面。"""

    async def get_latest_minutes(self, meeting_id: UUID) -> MinutesRecord | None: ...

    async def get_minutes(self, meeting_id: UUID, version: int) -> MinutesRecord | None: ...

    async def create_minutes(
        self, meeting_id: UUID, *, idempotency_key: str | None
    ) -> MinutesRecord: ...

    async def claim_minutes(self) -> MinutesJob | None: ...

    async def complete_minutes(
        self, minutes_id: UUID, result: MinutesResult
    ) -> MinutesRecord: ...

    async def fail_minutes(
        self,
        minutes_id: UUID,
        *,
        code: str,
        message: str,
        raw_output: str | None = None,
    ) -> None: ...


class RepositoryMaintenance(Protocol):
    """启动期崩溃恢复消费面。"""

    async def recover_stale(self) -> int: ...


class ClosableStore(Protocol):
    """生命周期关闭消费面。"""

    async def close(self) -> None: ...


class MeetingRepository(
    MeetingStore,
    TranscriptStore,
    SpeakerStore,
    MinutesStore,
    RepositoryMaintenance,
    ClosableStore,
    Protocol,
):
    """兼容 aggregate：覆盖 concrete repository 的实际消费面。"""


class RecoveryReplayRepository(Protocol):
    """RecoveryJournal 回放实际需要的 repository 窄端口。"""

    async def get_meeting(self, meeting_id: UUID) -> MeetingRecord | None: ...

    async def reconcile_window(
        self, meeting_id: UUID, window: TranscriptWindow
    ) -> TranscriptReconcileResult: ...

    async def set_status(
        self, meeting_id: UUID, status: MeetingStatus, *, reason: str | None = None
    ) -> MeetingRecord: ...

    async def finalize_transcript(
        self,
        meeting_id: UUID,
        *,
        final_status: MeetingStatus = MeetingStatus.COMPLETED,
        reason: str | None = None,
    ) -> MeetingRecord: ...

    async def create_minutes(
        self, meeting_id: UUID, *, idempotency_key: str | None
    ) -> MinutesRecord: ...


class SummaryWorkloadControl(Protocol):
    """录制优先时释放纪要 worker 租约的异步控制面。"""

    async def requeue_for_recording(self) -> None: ...

    async def resume_after_recording(self) -> None: ...


__all__ = [
    "CaptureFinalizationTimeout",
    "CaptureFinalizationTimeoutError",
    "CaptureGap",
    "CaptureLease",
    "ClosableStore",
    "GapListener",
    "MeetingCaptureGateway",
    "MeetingRepository",
    "MeetingStore",
    "MinutesStore",
    "RecoveryReplayRepository",
    "RepositoryMaintenance",
    "SpeakerStore",
    "SummaryWorkloadControl",
    "TranscriptStore",
    "WindowListener",
]
