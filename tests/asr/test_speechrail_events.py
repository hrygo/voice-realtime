"""SpeechRail transcription event decoder tests.

Transport-level envelope concerns (malformed JSON, sequence gaps,
session/request mismatch) are owned by ``speechrail.transport`` and are
deliberately NOT tested here.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from voice_realtime.speechrail.transcription_events import (
    DiarizationCompleted,
    InputAudioAck,
    SessionCompleted,
    SpeechRailSegment,
    SpeechRailTranscriptionError,
    TranscriptionCompleted,
    TranscriptionDelta,
    decode_transcription_event,
)
from voice_realtime.speechrail.transport import SpeechRailProtocolError


def _event(**fields: object) -> dict[str, object]:
    return dict(fields)


def test_decodes_input_audio_ack() -> None:
    event = decode_transcription_event(_event(type="input_audio_buffer.ack"))

    assert isinstance(event, InputAudioAck)


def test_decodes_transcription_delta() -> None:
    event = decode_transcription_event(
        _event(type="transcription.delta", text="你好", item_id="item-1", revision=1)
    )

    assert isinstance(event, TranscriptionDelta)
    assert event.text == "你好"


def test_decodes_transcription_completed_with_segments() -> None:
    event = decode_transcription_event(
        _event(
            type="transcription.completed",
            text="你好世界",
            segments=[
                {"start_ms": 0, "end_ms": 100, "text": "你好世界", "speaker": "spk_01"},
            ],
        )
    )

    assert isinstance(event, TranscriptionCompleted)
    assert event.text == "你好世界"
    assert event.segments == (
        SpeechRailSegment(text="你好世界", start_ms=0, end_ms=100, speaker="spk_01"),
    )


def test_decodes_completed_segment_without_speaker() -> None:
    event = decode_transcription_event(
        _event(
            type="transcription.completed",
            text="你好世界",
            segments=[{"start_ms": 0, "end_ms": 100, "text": "你好世界"}],
        )
    )

    assert event.segments[0].speaker is None


def test_decodes_empty_segments_list() -> None:
    event = decode_transcription_event(
        _event(type="transcription.completed", text="你好世界", segments=[])
    )

    assert event.segments == ()


def test_decodes_diarization_completed_mapping() -> None:
    event = decode_transcription_event(
        _event(type="transcription.diarization.completed", mapping={"spk_02": "spk_01"})
    )

    assert isinstance(event, DiarizationCompleted)
    assert event.mapping == (("spk_02", "spk_01"),)


def test_decodes_empty_diarization_mapping() -> None:
    event = decode_transcription_event(
        _event(type="transcription.diarization.completed", mapping={})
    )

    assert event.mapping == ()


def test_decodes_session_completed() -> None:
    event = decode_transcription_event(_event(type="session.completed"))

    assert isinstance(event, SessionCompleted)


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
        decode_transcription_event(_event(text="你好"))


def test_rejects_delta_without_text() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(_event(type="transcription.delta"))


def test_rejects_delta_with_non_string_text() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(_event(type="transcription.delta", text=123))


def test_rejects_completed_without_text() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(_event(type="transcription.completed", segments=[]))


def test_rejects_completed_with_non_string_text() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(_event(type="transcription.completed", text=True, segments=[]))


def test_rejects_segments_when_not_a_list() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(type="transcription.completed", text="你好世界", segments="not-a-list")
        )


def test_rejects_segment_when_not_a_mapping() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(type="transcription.completed", text="你好世界", segments=["seg-1"])
        )


def test_rejects_segment_with_empty_text() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(
                type="transcription.completed",
                text="",
                segments=[{"start_ms": 0, "end_ms": 100, "text": ""}],
            )
        )


def test_rejects_segment_with_whitespace_text() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(
                type="transcription.completed",
                text="",
                segments=[{"start_ms": 0, "end_ms": 100, "text": "   "}],
            )
        )


def test_rejects_segment_with_missing_text() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(
                type="transcription.completed",
                text="",
                segments=[{"start_ms": 0, "end_ms": 100}],
            )
        )


def test_rejects_segment_with_bool_start_ms() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(
                type="transcription.completed",
                text="",
                segments=[{"start_ms": True, "end_ms": 100, "text": "你好"}],
            )
        )


def test_rejects_segment_with_bool_end_ms() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(
                type="transcription.completed",
                text="",
                segments=[{"start_ms": 0, "end_ms": False, "text": "你好"}],
            )
        )


def test_rejects_segment_with_float_timestamp() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(
                type="transcription.completed",
                text="",
                segments=[{"start_ms": 0.0, "end_ms": 100, "text": "你好"}],
            )
        )


def test_rejects_segment_with_negative_start_ms() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(
                type="transcription.completed",
                text="",
                segments=[{"start_ms": -1, "end_ms": 100, "text": "你好"}],
            )
        )


def test_rejects_segment_when_end_before_start() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(
                type="transcription.completed",
                text="",
                segments=[{"start_ms": 200, "end_ms": 100, "text": "你好"}],
            )
        )


def test_rejects_segment_with_non_string_speaker() -> None:
    with pytest.raises(SpeechRailProtocolError) as excinfo:
        decode_transcription_event(
            _event(
                type="transcription.completed",
                text="",
                segments=[{"start_ms": 0, "end_ms": 100, "text": "你好", "speaker": 7}],
            )
        )

    assert excinfo.value.code == "SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR"


def test_rejects_segment_with_bad_speaker_prefix() -> None:
    with pytest.raises(SpeechRailProtocolError) as excinfo:
        decode_transcription_event(
            _event(
                type="transcription.completed",
                text="",
                segments=[{"start_ms": 0, "end_ms": 100, "text": "你好", "speaker": "speaker_1"}],
            )
        )

    assert excinfo.value.code == "SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR"


def test_rejects_segment_with_oversized_speaker() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(
                type="transcription.completed",
                text="",
                segments=[
                    {"start_ms": 0, "end_ms": 100, "text": "你好", "speaker": "spk_" + "x" * 65}
                ],
            )
        )


def test_rejects_remap_when_not_a_mapping() -> None:
    with pytest.raises(SpeechRailProtocolError) as excinfo:
        decode_transcription_event(
            _event(type="transcription.diarization.completed", mapping=[("spk_02", "spk_01")])
        )

    assert excinfo.value.code == "SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR"


def test_rejects_remap_with_non_string_source() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(type="transcription.diarization.completed", mapping={7: "spk_01"})
        )


def test_rejects_remap_with_non_string_target() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(type="transcription.diarization.completed", mapping={"spk_02": 7})
        )


def test_rejects_remap_with_bad_source_prefix() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(type="transcription.diarization.completed", mapping={"speaker_2": "spk_01"})
        )


def test_rejects_remap_with_bad_target_prefix() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(type="transcription.diarization.completed", mapping={"spk_02": "speaker_1"})
        )


def test_rejects_remap_identity_pair() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(
            _event(type="transcription.diarization.completed", mapping={"spk_02": "spk_02"})
        )


def test_rejects_error_without_error_field() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(_event(type="error"))


def test_rejects_error_with_non_mapping_error_field() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(_event(type="error", error="speechrail_error"))


def test_rejects_error_without_code() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(_event(type="error", error={"message": "boom"}))


def test_rejects_error_with_non_string_code() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(_event(type="error", error={"code": 7}))


def test_rejects_error_with_non_string_message() -> None:
    with pytest.raises(SpeechRailProtocolError):
        decode_transcription_event(_event(type="error", error={"code": "c", "message": 7}))
