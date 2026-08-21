"""会议助手的稳定领域与传输模型。

这些模型是后端各 workstream 之间的共享边界。模型使用不可变 Pydantic
对象，避免在异步转录广播和 PostgreSQL 写入之间共享可变状态。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。"""
    return datetime.now(UTC)


class _FrozenModel(BaseModel):
    """默认拒绝未知字段并禁止替换字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeMode(StrEnum):
    """Voice Studio 当前的互斥运行模式。"""

    ASSISTANT = "assistant"
    MEETING = "meeting"
    IDLE = "idle"


class MeetingStatus(StrEnum):
    """会议录制生命周期状态。"""

    RECORDING = "recording"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    STORAGE_ERROR = "storage_error"


class MinutesStatus(StrEnum):
    """AI 纪要任务状态。"""

    QUEUED = "queued"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"


class StorageHealth(StrEnum):
    """会议持久化健康状态。"""

    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class NormalizedSegment(_FrozenModel):
    """从 WhisperLiveKit 窗口规范化得到的一段已确认转录。"""

    id: UUID = Field(default_factory=uuid4)
    order: int = Field(ge=0)
    source_epoch: int = Field(ge=0)
    speaker_key: str = Field(min_length=1, max_length=200)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=100_000)
    translation: str | None = Field(default=None, max_length=100_000)
    detected_language: str | None = Field(default=None, max_length=32)

    @field_validator("speaker_key", "text", "translation", "detected_language")
    @classmethod
    def _strip_text(cls, value: str | None) -> str | None:
        """去除边界空白，并把空的可选字段归一化为 None。"""
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def _validate_segment(self) -> Self:
        """确保时间线有序且文本不是空白。"""
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms 必须大于等于 start_ms")
        if not self.text:
            raise ValueError("text 不能为空")
        return self


class TranscriptWindow(_FrozenModel):
    """WLK 当前 confirmed 窗口与易失 partial 文本。"""

    source_epoch: int = Field(ge=0)
    partial: str = Field(default="", max_length=100_000)
    segments: tuple[NormalizedSegment, ...] = ()

    @field_validator("partial")
    @classmethod
    def _strip_partial(cls, value: str) -> str:
        return value.strip()


class TranscriptReconcileResult(_FrozenModel):
    """一次窗口对账事务提交后的结果。"""

    meeting_id: UUID
    transcript_revision: int = Field(ge=0)
    content_revision: int = Field(ge=0)
    replace_from_ms: int = Field(ge=0)
    segments: tuple[NormalizedSegment, ...] = ()


class SpeakerRecord(_FrozenModel):
    """会议内匿名 speaker 与用户显示名的映射。"""

    meeting_id: UUID
    speaker_key: str = Field(min_length=1, max_length=200)
    source_epoch: int = Field(ge=0)
    raw_speaker: str = Field(min_length=1, max_length=200)
    default_label: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class MeetingRecord(_FrozenModel):
    """会议主记录。"""

    id: UUID = Field(default_factory=uuid4)
    title: str = Field(min_length=1, max_length=200)
    status: MeetingStatus = MeetingStatus.RECORDING
    language: str = Field(default="Chinese", min_length=1, max_length=32)
    audio_source: str = Field(default="microphone", min_length=1, max_length=32)
    started_at: datetime = Field(default_factory=_utc_now)
    ended_at: datetime | None = None
    transcript_revision: int = Field(default=0, ge=0)
    content_revision: int = Field(default=0, ge=0)
    interruption_reason: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    @field_validator("title", "language", "audio_source")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("字段不能为空")
        return stripped


class TranscriptDocument(_FrozenModel):
    """可供纪要和公开 API 读取的封存转录文档。"""

    meeting_id: UUID
    transcript_revision: int = Field(ge=0)
    content_revision: int = Field(ge=0)
    segments: tuple[NormalizedSegment, ...] = ()
    speakers: tuple[SpeakerRecord, ...] = ()


class MeetingPage(_FrozenModel):
    """会议历史游标分页结果。"""

    items: tuple[MeetingRecord, ...] = ()
    next_cursor: str | None = None


class Topic(_FrozenModel):
    """纪要中的主题。"""

    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=100_000)
    evidence_segment_ids: tuple[UUID, ...] = ()


class Decision(_FrozenModel):
    """纪要中的决策。"""

    content: str = Field(min_length=1, max_length=100_000)
    evidence_segment_ids: tuple[UUID, ...] = ()


class ActionItem(_FrozenModel):
    """纪要中的行动项；缺失信息保持为空。"""

    task: str = Field(min_length=1, max_length=100_000)
    owner: str | None = Field(default=None, max_length=200)
    due_date: str | None = Field(default=None, max_length=64)
    evidence_segment_ids: tuple[UUID, ...] = ()


class Risk(_FrozenModel):
    """纪要中的风险。"""

    content: str = Field(min_length=1, max_length=100_000)
    evidence_segment_ids: tuple[UUID, ...] = ()


class OpenQuestion(_FrozenModel):
    """纪要中的待确认问题。"""

    content: str = Field(min_length=1, max_length=100_000)
    evidence_segment_ids: tuple[UUID, ...] = ()


class Highlight(_FrozenModel):
    """纪要中的重点摘录。"""

    content: str = Field(min_length=1, max_length=100_000)
    evidence_segment_ids: tuple[UUID, ...] = ()


class MinutesResult(_FrozenModel):
    """已通过证据 ID 校验的结构化纪要。"""

    overview: str = Field(min_length=1, max_length=100_000)
    topics: tuple[Topic, ...] = ()
    decisions: tuple[Decision, ...] = ()
    action_items: tuple[ActionItem, ...] = ()
    risks: tuple[Risk, ...] = ()
    open_questions: tuple[OpenQuestion, ...] = ()
    highlights: tuple[Highlight, ...] = ()


class MinutesRecord(_FrozenModel):
    """PostgreSQL 中的纪要版本。"""

    id: UUID = Field(default_factory=uuid4)
    meeting_id: UUID
    version: int = Field(ge=1)
    status: MinutesStatus = MinutesStatus.QUEUED
    source_content_revision: int = Field(default=0, ge=0)
    model: str = Field(default="", max_length=256)
    prompt_version: str = Field(default="v1", max_length=64)
    content_json: MinutesResult | None = None
    content_markdown: str | None = None
    raw_output: str | None = None
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=2_000)
    created_at: datetime = Field(default_factory=_utc_now)
    generated_at: datetime | None = None
    updated_at: datetime = Field(default_factory=_utc_now)
    lease_until: datetime | None = None
    attempts: int = Field(default=0, ge=0)


class MinutesJob(_FrozenModel):
    """被 worker claim 的纪要任务及其租约信息。"""

    minutes: MinutesRecord
    meeting: MeetingRecord

    @property
    def id(self) -> UUID:
        """返回纪要版本 ID，方便 worker 使用共享接口。"""
        return self.minutes.id


class APIErrorDetail(_FrozenModel):
    """公开 API 的稳定错误载荷。"""

    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2_000)
    request_id: str = Field(min_length=1, max_length=128)
    details: dict[str, Any] = Field(default_factory=dict)
