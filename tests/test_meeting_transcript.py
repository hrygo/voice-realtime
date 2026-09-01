"""会议转录快照规范化与去重测试。"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from voice_realtime.asr.adapters.speechrail_realtime import _segments
from voice_realtime.asr.contracts import ASRSessionContext
from voice_realtime.asr.models import ASRWindow
from voice_realtime.meeting import transcript as transcript_module
from voice_realtime.meeting.asr_mapping import to_transcript_window
from voice_realtime.meeting.models import TranscriptWindow
from voice_realtime.meeting.transcript import TranscriptAccumulator
from voice_realtime.speechrail.transcription_events import (
    SpeechRailSegment,
    TranscriptionCompleted,
    decode_transcription_event,
)


def _decode_segments(text: str = "你好") -> tuple[SpeechRailSegment, ...]:
    event = decode_transcription_event(
        {
            "type": "transcription.completed",
            "text": text,
            "segments": [{"text": text, "start_ms": 1_000, "end_ms": 2_500}],
        }
    )
    assert isinstance(event, TranscriptionCompleted)
    return event.segments


def test_speechrail_segments_use_epoch_and_sample_offset() -> None:
    segments = _segments(
        _decode_segments(),
        ASRSessionContext(source_epoch=2, offset_ms=30_000, purpose="meeting"),
    )

    assert segments[0].start_ms == 31_000
    assert segments[0].end_ms == 32_500
    assert segments[0].speaker_key == "epoch:2:speaker:0"
    mapped = to_transcript_window(
        ASRWindow(source_epoch=2, segments=segments)
    )
    assert isinstance(mapped.segments[0].id, UUID)


def test_speechrail_segment_ids_change_when_text_changes() -> None:
    context = ASRSessionContext(source_epoch=1, offset_ms=0, purpose="meeting")
    first = to_transcript_window(
        ASRWindow(source_epoch=1, segments=_segments(_decode_segments("第一版"), context))
    )
    revised = to_transcript_window(
        ASRWindow(source_epoch=1, segments=_segments(_decode_segments("修订版"), context))
    )

    assert first.segments[0].id != revised.segments[0].id


def test_meeting_transcript_has_no_vendor_adapter_dependency() -> None:
    source = Path(transcript_module.__file__).read_text(encoding="utf-8")
    assert "voice_realtime.asr.adapters" not in source


def test_transcript_accumulator_detects_partial_speaker_changes() -> None:
    accumulator = TranscriptAccumulator()
    first = TranscriptWindow(
        source_epoch=1,
        partial="正在说",
        partial_speaker_key="epoch:1:speaker:1",
    )
    second = first.model_copy(
        update={"partial_speaker_key": "epoch:1:speaker:2"}
    )

    assert accumulator.apply(first) is True
    assert accumulator.apply(second) is True
