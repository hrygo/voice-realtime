"""进入会议转录实体的唯一 ASR 映射边界。"""

from __future__ import annotations

import json
from uuid import NAMESPACE_URL, uuid5

from sona.asr.models import ASRSegment, ASRWindow
from sona.meeting.models import NormalizedSegment, TranscriptWindow

__all__ = ["to_transcript_window"]


def to_transcript_window(window: ASRWindow) -> TranscriptWindow:
    """把 ASR 中立窗口投影为会议 TranscriptWindow。

    segment UUID 使用版本化种子，包含 source epoch、顺序、带会议 group
    的 speaker key、绝对时间区间和文本。同一窗口重播保持 ID 稳定，跨会议
    group 或不同时间的同文段不会复用 ID；历史已落库 ID 不做迁移。
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
        id=uuid5(NAMESPACE_URL, _segment_identity_seed(window, segment)),
        order=segment.order,
        source_epoch=segment.source_epoch,
        speaker_key=segment.speaker_key,
        start_ms=segment.start_ms,
        end_ms=segment.end_ms,
        text=segment.text,
        translation=segment.translation,
        detected_language=segment.detected_language,
    )


def _segment_identity_seed(window: ASRWindow, segment: ASRSegment) -> str:
    """Return the deterministic identity seed for one absolute ASR segment."""

    identity = json.dumps(
        [
            window.source_epoch,
            segment.source_epoch,
            segment.order,
            segment.speaker_key,
            segment.start_ms,
            segment.end_ms,
            segment.text,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"speechrail:v2:{identity}"
