"""ASR 后端与应用之间的稳定领域契约。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Protocol

from voice_realtime.asr.models import ASRWindow

ASREventKind = Literal["ready", "snapshot", "final", "error"]
ASRPurpose = Literal["subtitles", "meeting"]


@dataclass(frozen=True)
class ASRSessionContext:
    """由应用分配的会话时间轴；后端不得自行改写。"""

    source_epoch: int
    offset_ms: int
    purpose: ASRPurpose
    speaker_count_hint: int | None = None
    diarization_group_id: str | None = None

    def __post_init__(self) -> None:
        if self.source_epoch < 0:
            raise ValueError("source_epoch 必须非负")
        if self.offset_ms < 0:
            raise ValueError("offset_ms 必须非负")
        if self.speaker_count_hint is not None and not 1 <= self.speaker_count_hint <= 8:
            raise ValueError("speaker_count_hint 必须在 1 到 8 之间")
        if self.purpose != "meeting" and self.speaker_count_hint is not None:
            raise ValueError("speaker_count_hint 仅适用于会议会话")
        if self.diarization_group_id is not None and self.purpose != "meeting":
            raise ValueError("diarization_group_id 仅适用于会议会话")


@dataclass(frozen=True)
class ASRCapabilities:
    """一个 ASR 后端可被上层安全依赖的能力集合。"""

    languages: frozenset[str]
    supports_partial: bool
    supports_segment_timestamps: bool
    supports_word_timestamps: bool
    supports_hotwords: bool
    supports_speaker_labels: bool
    supports_native_diarization: bool
    supports_eof_flush: bool

    def __post_init__(self) -> None:
        normalized = frozenset(language.strip() for language in self.languages if language.strip())
        if not normalized:
            raise ValueError("languages 不能为空")
        object.__setattr__(self, "languages", normalized)


@dataclass(frozen=True)
class ASREvent:
    """从具体运行时规范化得到的原子 ASR 事件。"""

    kind: ASREventKind
    window: ASRWindow | None = None
    error_code: str | None = None
    error_message: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind in {"snapshot", "final"} and self.window is None:
            raise ValueError(f"{self.kind} event requires window")
        if self.kind in {"ready", "error"} and self.window is not None:
            raise ValueError(f"{self.kind} event cannot carry window")

        code = (self.error_code or "").strip()
        message = (self.error_message or "").strip()
        if self.kind == "error":
            if not code:
                raise ValueError("error event requires error_code")
            if not message:
                raise ValueError("error event requires error_message")
            object.__setattr__(self, "error_code", code)
            object.__setattr__(self, "error_message", message)
        elif self.error_code is not None or self.error_message is not None:
            raise ValueError("non-error event cannot carry error fields")

        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


class StreamingTranscriber(Protocol):
    """字幕/会议链路所需的流式识别端口。"""

    backend_id: str
    capabilities: ASRCapabilities

    @property
    def uri(self) -> str: ...

    async def connect(self) -> None: ...

    async def send_audio(self, chunk: bytes) -> None: ...

    def events(self) -> AsyncIterator[ASREvent]: ...

    async def finish(self) -> ASRWindow: ...

    async def close(self) -> None: ...


class ConversationSTTFactory(Protocol):
    """交互管道所需的 STT processor 构造端口。"""

    backend_id: str
    capabilities: ASRCapabilities

    def create_processor(self, *, sample_rate: int, language: str) -> object: ...
