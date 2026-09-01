"""进入会议转录实体的唯一 ASR 映射边界。"""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from voice_realtime.asr.models import ASRSegment, ASRWindow
from voice_realtime.meeting.models import NormalizedSegment, TranscriptWindow

__all__ = ["to_transcript_window"]


def to_transcript_window(window: ASRWindow) -> TranscriptWindow:
    """把 ASR 中立窗口投影为会议 TranscriptWindow。

    segment UUID 使用 ``speechrail:{source_epoch}:{order}:{text}`` 种子，
    保证与既有窗口对账 ID 稳定一致。
    """
    return TranscriptWindow(
        source_epoch=window.source_epoch,
        partial=window.partial,
        partial_speaker_key=window.partial_speaker_key,
        segments=tuple(_to_normalized_segment(window, segment) for segment in window.segments),
        speaker_remap=window.speaker_remap,
    )


def _to_normalized_segment(window: ASRWindow, segment: ASRSegment) -> NormalizedSegment:
    return NormalizedSegment(
        id=uuid5(
            NAMESPACE_URL, f"speechrail:{window.source_epoch}:{segment.order}:{segment.text}"
        ),
        order=segment.order,
        source_epoch=segment.source_epoch,
        speaker_key=segment.speaker_key,
        start_ms=segment.start_ms,
        end_ms=segment.end_ms,
        text=segment.text,
        translation=segment.translation,
        detected_language=segment.detected_language,
    )
