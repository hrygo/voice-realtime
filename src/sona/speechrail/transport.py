"""SpeechRail OpenAI-compatible Realtime transport adapter.

This is the single wire-protocol client for SpeechRail's ``WS /v1/realtime``
(OpenAI Realtime ASR/TTS subset).  It owns only the OpenAI envelope concerns
(JSON, ``type``/``session_id``/``sequence`` identity, strict monotonic
sequence) and the outbound client events; ASR/TTS semantics stay in the
adapters and ``speechrail.tts``.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol, cast

import websockets


class SpeechRailConnection(Protocol):
    async def send(self, payload: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self) -> None: ...


ConnectionFactory = Callable[[str], Awaitable[SpeechRailConnection]]


def _connect(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> Awaitable[SpeechRailConnection]:
    return cast(
        Awaitable[SpeechRailConnection],
        websockets.connect(url, additional_headers=headers or None),
    )


class SpeechRailProtocolError(RuntimeError):
    """Stable local error for malformed or rejected SpeechRail events."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


class SpeechRailOpenAITransport:
    """Validate the OpenAI Realtime envelope while leaving semantics to adapters."""

    def __init__(
        self,
        *,
        url: str,
        api_key: str | None = None,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self._url = url
        normalized_key = api_key.strip() if isinstance(api_key, str) else None
        self._connection_factory = connection_factory or (
            lambda target: _connect(
                target,
                headers=(
                    {"Authorization": f"Bearer {normalized_key}"}
                    if normalized_key
                    else None
                ),
            )
        )
        self._connection: SpeechRailConnection | None = None
        self._sequence = 0
        self._session_id: str | None = None

    @property
    def uri(self) -> str:
        return self._url

    async def connect(self) -> None:
        """Open the socket without sending a protocol-specific session update."""
        if self._connection is not None:
            raise RuntimeError("SPEECHRAIL_ALREADY_CONNECTED")
        self._sequence = 0
        self._session_id = None
        self._connection = await self._connection_factory(self._url)

    async def send_event(self, payload: Mapping[str, object]) -> None:
        if self._connection is None:
            raise RuntimeError("SPEECHRAIL_NOT_CONNECTED")
        await self._connection.send(json.dumps(dict(payload), separators=(",", ":")))

    async def receive(self) -> dict[str, object]:
        if self._connection is None:
            raise RuntimeError("SPEECHRAIL_NOT_CONNECTED")
        try:
            payload = json.loads(await self._connection.recv())
        except (json.JSONDecodeError, TypeError) as exc:
            raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR") from exc
        if not isinstance(payload, dict):
            raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR")
        sequence = payload.get("sequence")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence <= self._sequence
        ):
            raise SpeechRailProtocolError("SPEECHRAIL_SEQUENCE_ERROR")
        event_type = payload.get("type")
        session_id = payload.get("session_id")
        if (
            not isinstance(event_type, str)
            or not event_type
            or not isinstance(session_id, str)
            or not session_id
        ):
            raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR")
        if self._session_id is None:
            if event_type != "session.created":
                raise SpeechRailProtocolError("SPEECHRAIL_SESSION_CREATE_FAILED")
            self._session_id = session_id
        elif session_id != self._session_id:
            raise SpeechRailProtocolError("SPEECHRAIL_SESSION_MISMATCH")
        self._sequence = sequence
        return {str(key): value for key, value in payload.items()}

    async def close(self) -> None:
        if self._connection is not None:
            try:
                await self._connection.close()
            finally:
                self._connection = None
                self._session_id = None
                self._sequence = 0


_ASR_ALIAS_DIARIZE = "gpt-4o-transcribe-diarize"
_ASR_ALIAS_PLAIN = "gpt-4o-transcribe"


class SpeechRailRealtimeClient:
    """One OpenAI-standard Realtime ASR session used by subtitle/meeting adapters.

    ``connect`` opens the socket and sends a ``session.update`` that configures
    the ``input_audio_transcription`` model/language and (optionally) enables
    the session-scoped ``diarization`` profile.  Audio is streamed in 16 kHz
    mono PCM16 base64 chunks and each turn is finalized with ``commit``.
    """

    def __init__(
        self,
        *,
        url: str,
        api_key: str | None = None,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self._transport = SpeechRailOpenAITransport(
            url=url,
            api_key=api_key,
            connection_factory=connection_factory,
        )

    @property
    def uri(self) -> str:
        return self._transport.uri

    async def connect(
        self,
        *,
        language: str,
        diarization: bool = False,
        speaker_count_hint: int | None = None,
        diarization_group_id: str | None = None,
    ) -> None:
        await self._transport.connect()
        transcription: dict[str, object] = {
            "model": _ASR_ALIAS_DIARIZE if diarization else _ASR_ALIAS_PLAIN,
            "language": language,
        }
        if diarization:
            diarization_config: dict[str, object] = {"enabled": True, "finalize": True}
            if speaker_count_hint is not None:
                diarization_config["speaker_count_hint"] = speaker_count_hint
            if diarization_group_id is not None:
                diarization_config["group_id"] = diarization_group_id
            transcription["diarization"] = diarization_config
        await self._transport.send_event(
            {
                "type": "session.update",
                "session": {
                    "turn_detection": {"type": "manual"},
                    "input_audio_transcription": transcription,
                },
            }
        )

    async def append_pcm(self, chunk: bytes) -> None:
        if not chunk or len(chunk) % 2:
            raise ValueError("PCM must be non-empty int16")
        await self._transport.send_event(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(chunk).decode("ascii"),
            }
        )

    async def commit(self) -> None:
        await self._transport.send_event({"type": "input_audio_buffer.commit"})

    async def clear(self) -> None:
        await self._transport.send_event({"type": "input_audio_buffer.clear"})

    async def receive(self) -> dict[str, object]:
        return await self._transport.receive()

    async def close(self) -> None:
        await self._transport.close()


def decode_pcm16(value: object) -> bytes:
    """Decode a base64 PCM16 audio field at an outbound boundary."""

    if not isinstance(value, str) or not value:
        raise SpeechRailProtocolError("SPEECHRAIL_AUDIO_ERROR")
    try:
        audio = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise SpeechRailProtocolError("SPEECHRAIL_AUDIO_ERROR") from exc
    if not audio or len(audio) % 2:
        raise SpeechRailProtocolError("SPEECHRAIL_AUDIO_ERROR")
    return audio


__all__ = [
    "ConnectionFactory",
    "SpeechRailConnection",
    "SpeechRailOpenAITransport",
    "SpeechRailProtocolError",
    "SpeechRailRealtimeClient",
    "decode_pcm16",
]
