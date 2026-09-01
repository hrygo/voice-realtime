"""Event-specific semantic decoding for SpeechRail Realtime v2 transcription.

Transport-level validation (JSON, generic envelope, strict sequence,
session/request identity) stays unique in ``speechrail.transport``.
This module only validates event-specific fields and produces a narrow
typed union for ASR adapters to pattern-match.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from voice_realtime.speechrail.transport import SpeechRailProtocolError

__all__ = [
    "DiarizationCompleted",
    "InputAudioAck",
    "SessionCompleted",
    "SpeechRailSegment",
    "SpeechRailTranscriptionError",
    "SpeechRailTranscriptionEvent",
    "TranscriptionCompleted",
    "TranscriptionDelta",
    "decode_transcription_event",
]


@dataclass(frozen=True, slots=True)
class SpeechRailSegment:
    text: str
    start_ms: int
    end_ms: int
    speaker: str | None = None


@dataclass(frozen=True, slots=True)
class TranscriptionDelta:
    text: str


@dataclass(frozen=True, slots=True)
class TranscriptionCompleted:
    text: str
    segments: tuple[SpeechRailSegment, ...]


@dataclass(frozen=True, slots=True)
class DiarizationCompleted:
    mapping: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class InputAudioAck:
    pass


@dataclass(frozen=True, slots=True)
class SessionCompleted:
    pass


@dataclass(frozen=True, slots=True)
class SpeechRailTranscriptionError:
    code: str
    message: str


type SpeechRailTranscriptionEvent = (
    InputAudioAck
    | TranscriptionDelta
    | TranscriptionCompleted
    | DiarizationCompleted
    | SessionCompleted
    | SpeechRailTranscriptionError
)


def decode_transcription_event(raw: Mapping[str, object]) -> SpeechRailTranscriptionEvent:
    """Decode a transport-validated v2 event into its typed transcription form.

    Raises :class:`SpeechRailProtocolError` with ``SPEECHRAIL_PROTOCOL_ERROR``
    for generic shape violations and ``SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR``
    for speaker/remap violations.  Whether a speaker is *required* is decided
    by the consuming session's diarization configuration, never here.
    """
    event_type = raw.get("type")
    if event_type == "input_audio_buffer.ack":
        return InputAudioAck()
    if event_type == "transcription.delta":
        return _decode_delta(raw)
    if event_type == "transcription.completed":
        return _decode_completed(raw)
    if event_type == "transcription.diarization.completed":
        return DiarizationCompleted(mapping=_decode_remap(raw.get("mapping")))
    if event_type == "session.completed":
        return SessionCompleted()
    if event_type == "error":
        return _decode_error(raw.get("error"))
    raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR")


def _decode_delta(raw: Mapping[str, object]) -> TranscriptionDelta:
    text = raw.get("text")
    if not isinstance(text, str):
        raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR")
    return TranscriptionDelta(text=text)


def _decode_completed(raw: Mapping[str, object]) -> TranscriptionCompleted:
    text = raw.get("text")
    if not isinstance(text, str):
        raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR")
    return TranscriptionCompleted(text=text, segments=_decode_segments(raw.get("segments")))


def _decode_segments(value: object) -> tuple[SpeechRailSegment, ...]:
    if not isinstance(value, list):
        raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR")
    segments: list[SpeechRailSegment] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR")
        text = raw.get("text")
        start_ms = raw.get("start_ms")
        end_ms = raw.get("end_ms")
        if (
            not isinstance(text, str)
            or not text.strip()
            or not isinstance(start_ms, int)
            or isinstance(start_ms, bool)
            or not isinstance(end_ms, int)
            or isinstance(end_ms, bool)
            or start_ms < 0
            or end_ms < start_ms
        ):
            raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR")
        segments.append(
            SpeechRailSegment(
                text=text,
                start_ms=start_ms,
                end_ms=end_ms,
                speaker=_decode_speaker(raw.get("speaker")),
            )
        )
    return tuple(segments)


def _decode_speaker(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith("spk_") or len(value) > 64:
        raise SpeechRailProtocolError("SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR")
    return value


def _decode_remap(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        raise SpeechRailProtocolError("SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR")
    pairs: list[tuple[str, str]] = []
    for source, target in value.items():
        if (
            not isinstance(source, str)
            or not source.startswith("spk_")
            or not isinstance(target, str)
            or not target.startswith("spk_")
            or source == target
        ):
            raise SpeechRailProtocolError("SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR")
        pairs.append((source, target))
    return tuple(pairs)


def _decode_error(value: object) -> SpeechRailTranscriptionError:
    if not isinstance(value, dict):
        raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR")
    code = value.get("code")
    if not isinstance(code, str) or not code:
        raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR")
    message = value.get("message")
    if message is None:
        message = ""
    elif not isinstance(message, str):
        raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR")
    return SpeechRailTranscriptionError(code=code, message=message)
