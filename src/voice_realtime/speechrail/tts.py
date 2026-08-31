"""SpeechRail Realtime v2 TTS client without Pipecat or UI dependencies."""

from __future__ import annotations

import base64
import binascii
import contextlib
from collections.abc import AsyncIterator

from voice_realtime.speechrail.transport import (
    ConnectionFactory,
    SpeechRailProtocolError,
    SpeechRailV2Transport,
)

_PCM16_24K: dict[str, int | str] = {
    "type": "audio/pcm",
    "rate": 24_000,
    "channels": 1,
    "sample_width": 2,
}


class SpeechRailRemoteError(RuntimeError):
    """A stable error envelope returned by SpeechRail."""


class SpeechRailTTSClient:
    """Stream ordered SpeechRail PCM chunks for one bounded speech request."""

    def __init__(
        self,
        *,
        url: str,
        model: str,
        voice: str,
        language: str = "auto",
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        if not model.strip() or not voice.strip() or not language.strip():
            raise ValueError("model, voice, and language must be non-empty")
        self._url = url
        self._model = model
        self._voice = voice
        self._language = language
        self._connection_factory = connection_factory
        self._transport: SpeechRailV2Transport | None = None
        self._active_response_id: str | None = None
        self._cancel_requested = False

    @property
    def active_response_id(self) -> str | None:
        return self._active_response_id

    async def synthesize(self, text: str, speed: float = 1.0) -> AsyncIterator[bytes]:
        if not text.strip():
            raise ValueError("text must be non-empty")
        if not 0.25 <= speed <= 4.0:
            raise ValueError("speed must be between 0.25 and 4.0")
        if self._transport is not None:
            raise RuntimeError("SPEECHRAIL_TTS_ALREADY_ACTIVE")
        transport = SpeechRailV2Transport(
            url=self._url, connection_factory=self._connection_factory
        )
        self._transport = transport
        self._active_response_id = None
        self._cancel_requested = False
        response_finished = False
        next_chunk_index = 0
        try:
            await transport.connect()
            await transport.send_event(
                {
                    "type": "session.update",
                    "session": {
                        "type": "speech",
                        "model": self._model,
                        "voice": self._voice,
                        "language": self._language,
                        "audio_format": dict(_PCM16_24K),
                    },
                }
            )
            created = await transport.receive()
            if created.get("type") != "session.created":
                self._raise_remote_or_protocol(created)
            await transport.send_event(
                {"type": "speech_input.append", "text": text, "speed": speed}
            )
            await transport.send_event({"type": "speech_input.commit"})
            while True:
                event = await transport.receive()
                event_type = event.get("type")
                if event_type == "response.created":
                    self._active_response_id = _response_id(event)
                    _validate_audio_format(event.get("audio_format"))
                    next_chunk_index = 0
                elif event_type == "response.audio.delta":
                    response_id = _response_id(event)
                    if response_id != self._active_response_id:
                        raise SpeechRailProtocolError("unknown response")
                    chunk_index = event.get("chunk_index")
                    if not isinstance(chunk_index, int) or chunk_index != next_chunk_index:
                        raise SpeechRailProtocolError("out-of-order audio chunk")
                    next_chunk_index += 1
                    yield _decode_pcm(event.get("audio"))
                elif event_type == "response.audio.completed":
                    if _response_id(event) != self._active_response_id:
                        raise SpeechRailProtocolError("unknown response")
                    response_finished = True
                    self._active_response_id = None
                elif event_type == "response.audio.cancelled":
                    if _response_id(event) != self._active_response_id:
                        raise SpeechRailProtocolError("unknown response")
                    response_finished = True
                    self._active_response_id = None
                    return
                elif event_type == "session.completed":
                    if self._active_response_id is not None or not response_finished:
                        raise SpeechRailProtocolError(
                            "session completed before response terminal event"
                        )
                    return
                elif event_type == "error":
                    self._raise_remote_or_protocol(event)
                else:
                    raise SpeechRailProtocolError("unexpected speech event")
        finally:
            if self._active_response_id is not None and not self._cancel_requested:
                with contextlib.suppress(Exception):
                    await transport.send_event(
                        {"type": "response.cancel", "response_id": self._active_response_id}
                    )
            await transport.close()
            self._transport = None
            self._active_response_id = None

    async def cancel(self, response_id: str) -> None:
        if self._transport is None or response_id != self._active_response_id:
            raise RuntimeError("SPEECHRAIL_UNKNOWN_RESPONSE")
        self._cancel_requested = True
        await self._transport.send_event({"type": "response.cancel", "response_id": response_id})

    @staticmethod
    def _raise_remote_or_protocol(event: dict[str, object]) -> None:
        if event.get("type") != "error":
            raise SpeechRailProtocolError("SPEECHRAIL_SESSION_CREATE_FAILED")
        error = event.get("error")
        if not isinstance(error, dict):
            raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR")
        code = error.get("code")
        message = error.get("message")
        if not isinstance(code, str) or not isinstance(message, str):
            raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR")
        raise SpeechRailRemoteError(f"{code}: {message}")


def _response_id(event: dict[str, object]) -> str:
    response_id = event.get("response_id")
    if not isinstance(response_id, str) or not response_id:
        raise SpeechRailProtocolError("invalid response id")
    return response_id


def _validate_audio_format(value: object) -> None:
    if not isinstance(value, dict) or value != _PCM16_24K:
        raise SpeechRailProtocolError("invalid audio format")


def _decode_pcm(value: object) -> bytes:
    if not isinstance(value, str) or not value:
        raise SpeechRailProtocolError("invalid PCM")
    try:
        audio = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SpeechRailProtocolError("invalid PCM") from exc
    if not audio or len(audio) % 2:
        raise SpeechRailProtocolError("invalid PCM")
    return audio
