"""ASR 后端领域契约测试。"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError
from uuid import NAMESPACE_URL, uuid5

import pytest

from voice_realtime.asr.contracts import ASRCapabilities, ASREvent, ASRSessionContext
from voice_realtime.asr.models import ASRSegment, ASRWindow
from voice_realtime.meeting.asr_mapping import to_transcript_window
from voice_realtime.meeting.models import TranscriptWindow


def _capabilities() -> ASRCapabilities:
    return ASRCapabilities(
        languages=frozenset({"zh"}),
        supports_partial=True,
        supports_segment_timestamps=True,
        supports_word_timestamps=True,
        supports_hotwords=False,
        supports_speaker_labels=True,
        supports_native_diarization=False,
        supports_eof_flush=True,
    )


def test_asr_capabilities_are_immutable() -> None:
    capabilities = _capabilities()

    with pytest.raises(FrozenInstanceError):
        capabilities.supports_partial = False  # type: ignore[misc]


def test_asr_capabilities_reject_empty_languages() -> None:
    with pytest.raises(ValueError, match="languages"):
        ASRCapabilities(
            languages=frozenset(),
            supports_partial=True,
            supports_segment_timestamps=True,
            supports_word_timestamps=True,
            supports_hotwords=False,
            supports_speaker_labels=True,
            supports_native_diarization=False,
            supports_eof_flush=True,
        )


@pytest.mark.parametrize("kind", ["snapshot", "final"])
def test_transcript_events_require_window(kind: str) -> None:
    with pytest.raises(ValueError, match="window"):
        ASREvent(kind=kind)  # type: ignore[arg-type]


def test_error_event_requires_code_and_message() -> None:
    with pytest.raises(ValueError, match="error_code"):
        ASREvent(kind="error")


def test_snapshot_event_accepts_immutable_asr_window() -> None:
    window = ASRWindow(source_epoch=3, partial="正在识别")

    event = ASREvent(kind="snapshot", window=window)

    assert event.window is window
    assert event.kind == "snapshot"


def test_ready_event_rejects_error_fields() -> None:
    with pytest.raises(ValueError, match="error fields"):
        ASREvent(kind="ready", error_code="unexpected", error_message="unexpected")


def test_session_context_rejects_negative_timeline() -> None:
    with pytest.raises(ValueError, match="offset_ms"):
        ASRSessionContext(source_epoch=1, offset_ms=-1, purpose="meeting")


def test_asr_package_imports_without_meeting_models() -> None:
    """ASR port modules must load without importing meeting entities."""
    script = (
        "import sys;"
        "import voice_realtime.asr.contracts;"
        "import voice_realtime.asr.models;"
        "import voice_realtime.asr.presenters;"
        "import voice_realtime.asr.adapters.speechrail_realtime;"
        "import voice_realtime.asr.adapters.speechrail_pipecat;"
        "assert 'voice_realtime.meeting.models' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)


def test_asr_segment_rejects_negative_order() -> None:
    with pytest.raises(ValueError, match="order"):
        ASRSegment(order=-1, source_epoch=0, speaker_key="s", start_ms=0, end_ms=0, text="t")


def test_asr_segment_rejects_negative_source_epoch() -> None:
    with pytest.raises(ValueError, match="source_epoch"):
        ASRSegment(order=0, source_epoch=-1, speaker_key="s", start_ms=0, end_ms=0, text="t")


def test_asr_segment_rejects_negative_start_ms() -> None:
    with pytest.raises(ValueError, match="start_ms"):
        ASRSegment(order=0, source_epoch=0, speaker_key="s", start_ms=-1, end_ms=0, text="t")


def test_asr_segment_rejects_end_before_start() -> None:
    with pytest.raises(ValueError, match="end_ms"):
        ASRSegment(order=0, source_epoch=0, speaker_key="s", start_ms=100, end_ms=50, text="t")


def test_asr_segment_rejects_empty_speaker_key() -> None:
    with pytest.raises(ValueError, match="speaker_key"):
        ASRSegment(order=0, source_epoch=0, speaker_key="   ", start_ms=0, end_ms=0, text="t")


def test_asr_segment_rejects_empty_text() -> None:
    with pytest.raises(ValueError, match="text"):
        ASRSegment(order=0, source_epoch=0, speaker_key="s", start_ms=0, end_ms=0, text="   ")


def test_asr_segment_is_frozen() -> None:
    segment = ASRSegment(order=0, source_epoch=0, speaker_key="s", start_ms=0, end_ms=0, text="t")

    with pytest.raises(FrozenInstanceError):
        segment.text = "改"  # type: ignore[misc]


def test_asr_window_rejects_negative_source_epoch() -> None:
    with pytest.raises(ValueError, match="source_epoch"):
        ASRWindow(source_epoch=-1)


def test_asr_window_is_frozen() -> None:
    window = ASRWindow(source_epoch=1, partial="你好")

    with pytest.raises(FrozenInstanceError):
        window.partial = "改"  # type: ignore[misc]


def test_asr_window_segments_are_immutable_tuple() -> None:
    window = ASRWindow(
        source_epoch=1,
        segments=(
            ASRSegment(order=0, source_epoch=1, speaker_key="s", start_ms=0, end_ms=10, text="t"),
        ),
    )

    assert window.segments == window.segments
    assert type(window.segments) is tuple


def test_to_transcript_window_maps_segments_with_stable_uuids() -> None:
    window = ASRWindow(
        source_epoch=2,
        partial="正在识别",
        segments=(
            ASRSegment(
                order=0,
                source_epoch=2,
                speaker_key="epoch:2:speaker:spk_01",
                start_ms=1_000,
                end_ms=1_100,
                text="你好世界",
                detected_language="zh",
            ),
        ),
        speaker_remap=(("epoch:2:speaker:spk_01", "group:abc:speaker:spk_02"),),
    )

    transcript = to_transcript_window(window)

    assert isinstance(transcript, TranscriptWindow)
    assert transcript.source_epoch == 2
    assert transcript.partial == "正在识别"
    assert transcript.speaker_remap == window.speaker_remap
    segment = transcript.segments[0]
    assert segment.id == uuid5(NAMESPACE_URL, "speechrail:2:0:你好世界")
    assert segment.order == 0
    assert segment.source_epoch == 2
    assert segment.speaker_key == "epoch:2:speaker:spk_01"
    assert segment.start_ms == 1_000
    assert segment.end_ms == 1_100
    assert segment.text == "你好世界"
    assert segment.detected_language == "zh"


def test_to_transcript_window_round_trips_empty_window() -> None:
    transcript = to_transcript_window(ASRWindow(source_epoch=3))

    assert transcript.source_epoch == 3
    assert transcript.partial == ""
    assert transcript.segments == ()
    assert transcript.speaker_remap == ()
