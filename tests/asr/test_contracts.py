"""ASR 后端领域契约测试。"""

from dataclasses import FrozenInstanceError

import pytest

from voice_realtime.asr.contracts import ASRCapabilities, ASREvent, ASRSessionContext
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


def test_snapshot_event_accepts_immutable_transcript_window() -> None:
    window = TranscriptWindow(source_epoch=3, partial="正在识别")

    event = ASREvent(kind="snapshot", window=window)

    assert event.window is window
    assert event.kind == "snapshot"


def test_ready_event_rejects_error_fields() -> None:
    with pytest.raises(ValueError, match="error fields"):
        ASREvent(kind="ready", error_code="unexpected", error_message="unexpected")


def test_session_context_rejects_negative_timeline() -> None:
    with pytest.raises(ValueError, match="offset_ms"):
        ASRSessionContext(source_epoch=1, offset_ms=-1, purpose="meeting")
