"""SpeechRail OpenAI Realtime transcription event decoder tests.

Transport-level envelope concerns (malformed JSON, sequence gaps, session
mismatch) are owned by ``speechrail.transport`` and are deliberately NOT
tested here.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from sona.speechrail.transcription_events import (
    Noop,
    SpeechRailTranscriptionError,
    TranscriptionCompleted,
    TranscriptionDelta,
    TranscriptionSegment,
    decode_transcription_event,
)
from sona.speechrail.transport import SpeechRailProtocolError


def _event(**fields: object) -> dict[str, object]:
    return dict(fields)


def test_decodes_session_noop() -> None:
    event = decode_transcription_event(_event(type="session.created"))

    assert isinstance(event, Noop)


def test_decodes_completed_commit_noop() -> None:
    event = decode_transcription_event(_event(type="input_audio_buffer.committed"))

    assert isinstance(event, Noop)


def test_decodes_transcription_delta() -> None:
    event = decode_transcription_event(
        _event(
            type="conversation.item.input_audio_transcription.delta",
            item_id="item-1",
            content_index=0,
            delta="你好",
        )
    )

    assert isinstance(event, TranscriptionDelta)
    assert event.text == "你好"


def test_decodes_transcription_completed() -> None:
    event = decode_transcription_event(
        _event(
            type="conversation.item.input_audio_transcription.completed",
            item_id="item-1",
            content_index=0,
            transcript="你好世界",
        )
    )

    assert isinstance(event, TranscriptionCompleted)
    assert event.transcript == "你好世界"


def test_decodes_transcription_segment_with_speaker() -> None:
    event = decode_transcription_event(
        _event(
            type="conversation.item.input_audio_transcription.segment",
            item_id="item-1",
            content_index=0,
            id="seg-1",
            text="你好世界",
            speaker="spk_01",
            start=0.0,
            end=1.234,
        )
    )

    assert isinstance(event, TranscriptionSegment)
    assert event.text == "你好世界"
    assert event.speaker == "spk_01"
    assert event.start_ms == 0
    assert event.end_ms == 1234
    assert event.start_ms < event.end_ms


def test_decodes_transcription_segment_without_speaker() -> None:
    event = decode_transcription_event(
        _event(
            type="conversation.item.input_audio_transcription.segment",
            text="你好世界",
            start=0.0,
            end=1.0,
        )
    )

    assert isinstance(event, TranscriptionSegment)
    assert event.speaker is None


def test_decodes_transcription_failed() -> None:
    event = decode_transcription_event(
        _event(
            type="conversation.item.input_audio_transcription.failed",
            error={"code": "backend_error", "message": "boom"},
        )
    )

    assert isinstance(event, SpeechRailTranscriptionError)
    assert event.code == "backend_error"
    assert event.message == "boom"


def test_decodes_error_with_code_and_message() -> None:
    event = decode_transcription_event(
        _event(type="error", error={"code": "speechrail_error", "message": "boom"})
    )

    assert isinstance(event, SpeechRailTranscriptionError)
    assert event.code == "speechrail_error"
    assert event.message == "boom"


def test_decodes_error_without_message() -> None:
    event = decode_transcription_event(_event(type="error", error={"code": "speechrail_error"}))

    assert event.message == ""


def test_events_are_frozen() -> None:
    delta = TranscriptionDelta(text="你好")

    with pytest.raises(FrozenInstanceError):
        delta.text = "改"  # type: ignore[misc]


def test_rejects_unknown_event_type() -> None:
    with pytest.raises(SpeechRailProtocolError) as excinfo:
        decode_transcription_event(_event(type="some.future.event"))

    assert excinfo.value.code == "SPEECHRAIL_PROTOCOL_ERROR"


def test_rejects_missing_type() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(_event(delta="你好"))


def test_rejects_delta_without_text() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(type="conversation.item.input_audio_transcription.delta")
        )


def test_rejects_delta_with_non_string_text() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(
                type="conversation.item.input_audio_transcription.delta",
                delta=123,
            )
        )


def test_rejects_completed_without_transcript() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(type="conversation.item.input_audio_transcription.completed")
        )


def test_rejects_completed_with_non_string_transcript() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(
                type="conversation.item.input_audio_transcription.completed",
                transcript=True,
            )
        )


def test_rejects_segment_with_empty_text() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(
                type="conversation.item.input_audio_transcription.segment",
                text="",
                start=0.0,
                end=1.0,
            )
        )


def test_rejects_segment_with_whitespace_text() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(
                type="conversation.item.input_audio_transcription.segment",
                text="   ",
                start=0.0,
                end=1.0,
            )
        )


def test_rejects_segment_with_negative_start() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(
                type="conversation.item.input_audio_transcription.segment",
                text="你好",
                start=-1,
                end=1.0,
            )
        )


def test_rejects_segment_when_end_before_start() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(
                type="conversation.item.input_audio_transcription.segment",
                text="你好",
                start=1.0,
                end=0.5,
            )
        )


def test_rejects_segment_with_non_string_speaker() -> None:
    with pytest.raises(SpeechRailProtocolError) as excinfo:
        decode_transcription_event(
            _event(
                type="conversation.item.input_audio_transcription.segment",
                text="你好",
                start=0.0,
                end=1.0,
                speaker=7,
            )
        )

    assert excinfo.value.code == "SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR"


def test_rejects_segment_with_bad_speaker_prefix() -> None:
    with pytest.raises(SpeechRailProtocolError) as excinfo:
        decode_transcription_event(
            _event(
                type="conversation.item.input_audio_transcription.segment",
                text="你好",
                start=0.0,
                end=1.0,
                speaker="speaker_1",
            )
        )

    assert excinfo.value.code == "SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR"


def test_rejects_error_without_error_field() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(_event(type="error"))


def test_rejects_error_with_non_mapping_error_field() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(_event(type="error", error="speechrail_error"))


def test_rejects_error_without_code() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(_event(type="error", error={"message": "boom"}))
