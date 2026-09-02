"""会议转录快照规范化与去重测试。"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from sona.asr.contracts import ASRSessionContext
from sona.asr.models import ASRWindow
from sona.meeting import transcript as transcript_module
from sona.meeting.asr_mapping import to_transcript_window
from sona.meeting.models import TranscriptWindow
from sona.meeting.transcript import TranscriptAccumulator
from sona.speechrail.transcriber import _segment
from sona.speechrail.transcription_events import (
    TranscriptionSegment,
    decode_transcription_event,
)


def _decode_segments(text: str = "你好") -> TranscriptionSegment:
    event = decode_transcription_event(
        {
            "type": "conversation.item.input_audio_transcription.segment",
            "text": text,
            "speaker": None,
            "start": 1_000 / 1_000,
            "end": 2_500 / 1_000,
        }
    )
    assert isinstance(event, TranscriptionSegment)
    return event


def test_speechrail_segments_use_epoch_and_sample_offset() -> None:
    segment = _segment(
        _decode_segments(),
        ASRSessionContext(source_epoch=2, offset_ms=30_000, purpose="meeting"),
        require_speaker=False,
    )

    assert segment.start_ms == 31_000
    assert segment.end_ms == 32_500
    assert segment.speaker_key == "epoch:2:speaker:0"
    mapped = to_transcript_window(
        ASRWindow(source_epoch=2, segments=(segment,))
    )
    assert isinstance(mapped.segments[0].id, UUID)


def test_speechrail_segment_ids_change_when_text_changes() -> None:
    context = ASRSessionContext(source_epoch=1, offset_ms=0, purpose="meeting")
    first = to_transcript_window(
        ASRWindow(
            source_epoch=1,
            segments=(_segment(_decode_segments("第一版"), context, require_speaker=False),),
        )
    )
    revised = to_transcript_window(
        ASRWindow(
            source_epoch=1,
            segments=(_segment(_decode_segments("修订版"), context, require_speaker=False),),
        )
    )

    assert first.segments[0].id != revised.segments[0].id


def test_meeting_transcript_has_no_vendor_adapter_dependency() -> None:
    source = Path(transcript_module.__file__).read_text(encoding="utf-8")
    assert "sona.asr.adapters" not in source


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
