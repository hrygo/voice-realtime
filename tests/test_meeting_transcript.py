"""会议转录快照规范化与去重测试。"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from voice_realtime.asr.adapters.wlk import TranscriptNormalizer
from voice_realtime.meeting import transcript as transcript_module
from voice_realtime.meeting.models import TranscriptWindow
from voice_realtime.meeting.transcript import TranscriptAccumulator


def _snapshot(text: str = "你好", speaker: int = 1) -> dict[str, object]:
    return {
        "type": "full_update",
        "buffer_transcription": "正在说",
        "lines": [
            {
                "speaker": speaker,
                "text": text,
                "start": "0:00:01.00",
                "end": "0:00:02.50",
                "translation": None,
                "detected_language": "zh",
            },
            {"speaker": -2, "text": "", "start": "0:00:02.50", "end": "0:00:03"},
        ],
    }


def test_normalizer_uses_epoch_and_sample_offset() -> None:
    window = TranscriptNormalizer().normalize(_snapshot(), source_epoch=2, offset_ms=30_000)

    assert window.source_epoch == 2
    assert window.segments[0].start_ms == 31_000
    assert window.segments[0].end_ms == 32_500
    assert window.segments[0].speaker_key != "1"
    assert window.segments[0].speaker_key == "epoch:2:speaker:1"
    assert window.partial == "正在说"
    assert isinstance(window.segments[0].id, UUID)


def test_normalizer_skips_silence_and_invalid_lines() -> None:
    payload = {"lines": [{"speaker": -2, "text": "静音"}, {"speaker": 1, "text": "  "}]}

    window = TranscriptNormalizer().normalize(payload, source_epoch=1, offset_ms=0)

    assert window.segments == ()
    assert window.partial == ""


def test_normalizer_ids_change_when_wlk_revises_text() -> None:
    normalizer = TranscriptNormalizer()

    first = normalizer.normalize(_snapshot("第一版"), source_epoch=1, offset_ms=0)
    revised = normalizer.normalize(_snapshot("修订版"), source_epoch=1, offset_ms=0)

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
