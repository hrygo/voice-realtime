"""Event-specific semantic decoding for SpeechRail OpenAI Realtime transcription.

Transport-level envelope concerns (JSON, generic envelope, strict sequence,
session identity) stay unique in ``speechrail.transport``.  This module only
validates event-specific fields and produces a narrow typed union for ASR
adapters to pattern-match against the OpenAI Realtime ``/v1/realtime`` events.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sona.speechrail.transport import SpeechRailProtocolError

__all__ = [
    "Noop",
    "SpeechRailTranscriptionError",
    "SpeechRailTranscriptionEvent",
    "TranscriptionCompleted",
    "TranscriptionDelta",
    "TranscriptionSegment",
    "decode_transcription_event",
]

# Server->client events that carry no transcription payload and are safely
# ignored by the ASR adapters.
_SESSION_NOOPS = frozenset(
    {
        "session.created",
        "session.updated",
        "conversation.created",
        "conversation.item.created",
        "input_audio_buffer.committed",
        "input_audio_buffer.cleared",
        "response.created",
        "response.output_item.added",
        "response.content_part.added",
    }
)


@dataclass(frozen=True, slots=True)
class Noop:
    """A semantically-inert server event (session/ack/parent envelope)."""

    reason: str
    item_id: str | None = None
    audio_start_ms: int | None = None
    audio_end_ms: int | None = None


@dataclass(frozen=True, slots=True)
class TranscriptionDelta:
    text: str
    item_id: str | None = None


@dataclass(frozen=True, slots=True)
class TranscriptionSegment:
    """One immutable transcription segment; ``speaker`` is anonymous or null."""

    text: str
    speaker: str | None
    start_ms: int
    end_ms: int
    item_id: str | None = None


@dataclass(frozen=True, slots=True)
class TranscriptionCompleted:
    transcript: str
    item_id: str | None = None


@dataclass(frozen=True, slots=True)
class SpeechRailTranscriptionError:
    code: str
    message: str


type SpeechRailTranscriptionEvent = (
    Noop
    | TranscriptionDelta
    | TranscriptionSegment
    | TranscriptionCompleted
    | SpeechRailTranscriptionError
)


def decode_transcription_event(raw: Mapping[str, object]) -> SpeechRailTranscriptionEvent:
    """Decode a transport-validated OpenAI Realtime event into a typed form.

    Raises :class:`SpeechRailProtocolError` with ``SPEECHRAIL_PROTOCOL_ERROR``
    for shape violations and ``SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR`` for
    speaker/timestamp violations.
    """
    event_type = raw.get("type")
    if not isinstance(event_type, str):
        raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR")
    if event_type == "input_audio_buffer.speech_started":
        return _decode_speech_boundary(raw, "audio_start_ms")
    if event_type == "input_audio_buffer.speech_stopped":
        return _decode_speech_boundary(raw, "audio_end_ms")
    if event_type in _SESSION_NOOPS:
        return Noop(reason=event_type, item_id=_optional_item_id(raw.get("item_id")))
    if event_type == "conversation.item.input_audio_transcription.delta":
        return TranscriptionDelta(
            text=_require_text(raw.get("delta")),
            item_id=_optional_item_id(raw.get("item_id")),
        )
    if event_type == "conversation.item.input_audio_transcription.completed":
        return TranscriptionCompleted(
            transcript=_require_text(raw.get("transcript")),
            item_id=_optional_item_id(raw.get("item_id")),
        )
    if event_type == "conversation.item.input_audio_transcription.segment":
        return _decode_segment(raw)
    if event_type == "conversation.item.input_audio_transcription.failed":
        return _decode_error(raw.get("error"))
    if event_type == "error":
        return _decode_error(raw.get("error"))
    raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR")


def _require_text(value: object) -> str:
    if not isinstance(value, str):
        raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR")
    return value


def _optional_item_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR")
    return value


def _decode_speech_boundary(raw: Mapping[str, object], field: str) -> Noop:
    value = raw.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR")
    return Noop(
        reason=(
            "input_audio_buffer.speech_started"
            if field == "audio_start_ms"
            else "input_audio_buffer.speech_stopped"
        ),
        item_id=_optional_item_id(raw.get("item_id")),
        audio_start_ms=value if field == "audio_start_ms" else None,
        audio_end_ms=value if field == "audio_end_ms" else None,
    )


def _decode_segment(raw: Mapping[str, object]) -> TranscriptionSegment:
    text = raw.get("text")
    start = raw.get("start")
    end = raw.get("end")
    if (
        not isinstance(text, str)
        or not text.strip()
        or isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, (int, float))
        or not isinstance(end, (int, float))
        or start < 0
        or end < start
    ):
        raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR")
    return TranscriptionSegment(
        text=text,
        speaker=_decode_speaker(raw.get("speaker")),
        start_ms=round(start * 1000),
        end_ms=round(end * 1000),
        item_id=_optional_item_id(raw.get("item_id")),
    )


def _decode_speaker(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str):
        raise SpeechRailProtocolError("SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR")
    if not value.startswith("spk_") or len(value) > 64:
        raise SpeechRailProtocolError("SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR")
    return value


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
