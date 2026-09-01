"""ASR 端口输出给应用的中立结果模型。

这些 dataclass 与具体 SpeechRail wire event 和会议实体都无关；会议实体
只在 ``voice_realtime.meeting.asr_mapping`` 的唯一 mapper 中生成。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ASRSegment", "ASRWindow"]


@dataclass(frozen=True, slots=True)
class ASRSegment:
    """一段已确认的 ASR 转录，时间与说话人键已由适配器投影到会话时间轴。"""

    order: int
    source_epoch: int
    speaker_key: str
    start_ms: int
    end_ms: int
    text: str
    translation: str | None = None
    detected_language: str | None = None

    def __post_init__(self) -> None:
        if self.order < 0:
            raise ValueError("order 必须非负")
        if self.source_epoch < 0:
            raise ValueError("source_epoch 必须非负")
        if self.start_ms < 0:
            raise ValueError("start_ms 必须非负")
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms 必须大于等于 start_ms")
        if not self.speaker_key.strip():
            raise ValueError("speaker_key 不能为空")
        if not self.text.strip():
            raise ValueError("text 不能为空")


@dataclass(frozen=True, slots=True)
class ASRWindow:
    """ASR 端口当前 confirmed 窗口与易失 partial 文本。"""

    source_epoch: int
    partial: str = ""
    partial_speaker_key: str | None = None
    segments: tuple[ASRSegment, ...] = ()
    speaker_remap: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.source_epoch < 0:
            raise ValueError("source_epoch 必须非负")
