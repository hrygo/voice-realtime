"""SpeechRail OpenAI Realtime outbound adapter for text-to-speech.

Each ``synthesize`` call opens one OpenAI-standard ``/v1/realtime`` session and
produces its 24 kHz base64 PCM16 ``response.output_audio.delta`` stream.  The
caller owns playback and interruption (``cancel``); SpeechRail only supports a
single in-flight TTS response per session.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from sona.speechrail.transport import (
    ConnectionFactory,
    SpeechRailOpenAITransport,
    SpeechRailProtocolError,
    decode_pcm16,
)

_TTS_ALIAS = "gpt-4o-mini-tts"

# Server->client events that don't terminate or carry audio for the TTS flow.
_TTS_NOOPS = frozenset(
    {
        "session.created",
        "session.updated",
        "conversation.created",
        "conversation.item.created",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_audio.done",
        "response.output_audio_transcript.delta",
        "response.output_audio_transcript.done",
        "response.content_part.done",
        "response.output_item.done",
    }
)


class SpeechRailTTSClient:
    """One non-resumable OpenAI-standard speech session."""

    def __init__(
        self,
        *,
        url: str,
        model: str,
        voice: str,
        language: str = "auto",
        api_key: str | None = None,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be blank")
        if not voice.strip():
            raise ValueError("voice must not be blank")
        if not language.strip():
            raise ValueError("language must not be blank")
        self._url = url
        self._model = model.strip()
        self._voice = "default" if voice.strip() == "alloy" else voice.strip()
        self._language = language.strip() or "auto"
        self._api_key = api_key
        self._connection_factory = connection_factory
        self._transport: SpeechRailOpenAITransport | None = None
        self._active_response_id: str | None = None

    @property
    def active_response_id(self) -> str | None:
        """Return the response currently owned by this one-shot session."""
        return self._active_response_id

    async def synthesize(self, text: str, *, speed: float = 1.0) -> AsyncIterator[bytes]:
        if not text.strip():
            return
        if not 0.25 <= speed <= 4.0:
            raise ValueError("speed must be between 0.25 and 4.0")
        transport = SpeechRailOpenAITransport(
            url=self._url,
            api_key=self._api_key,
            connection_factory=self._connection_factory,
        )
        self._transport = transport
        try:
            await transport.connect()
            await transport.send_event(
                {
                    "type": "session.update",
                    "session": {
                        "model": self._model,
                        "voice": self._voice,
                        "language": self._language,
                        "output_audio_format": "pcm16",
                    },
                }
            )
            await transport.send_event(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    },
                }
            )
            await transport.send_event(
                {"type": "response.create", "response": {"voice": self._voice}}
            )
            while True:
                event = await transport.receive()
                event_type = event.get("type")
                if event_type == "response.created":
                    response_id = _response_id(event.get("response"))
                    if self._active_response_id is not None:
                        raise SpeechRailProtocolError("SPEECHRAIL_RESPONSE_ERROR")
                    self._active_response_id = response_id
                elif event_type == "response.output_audio.delta":
                    self._ensure_active(event.get("response_id"))
                    yield decode_pcm16(event.get("delta"))
                elif event_type == "response.done":
                    self._active_response_id = None
                    status = _response_status(event.get("response"))
                    if status not in ("completed", "cancelled"):
                        raise SpeechRailProtocolError("SPEECHRAIL_RESPONSE_ERROR")
                    return
                elif event_type == "error":
                    error = event.get("error")
                    code = error.get("code") if isinstance(error, dict) else None
                    raise SpeechRailProtocolError(
                        code if isinstance(code, str) and code else "SPEECHRAIL_REQUEST_FAILED"
                    )
                elif event_type in _TTS_NOOPS:
                    continue
                else:
                    raise SpeechRailProtocolError("SPEECHRAIL_EVENT_ERROR")
        except asyncio.CancelledError:
            active_response_id = self._active_response_id
            if active_response_id is not None:
                async with asyncio.timeout(0.5):
                    await self.cancel(active_response_id)
            raise
        finally:
            self._active_response_id = None
            await transport.close()
            self._transport = None

    async def cancel(self, response_id: str) -> None:
        """Cancel the active response; socket teardown stays with ``synthesize``."""
        if response_id != self._active_response_id:
            return
        transport = self._transport
        if transport is not None:
            with contextlib.suppress(Exception):
                await transport.send_event({"type": "response.cancel"})
        self._active_response_id = None

    def _ensure_active(self, response_id: object) -> None:
        if response_id != self._active_response_id:
            raise SpeechRailProtocolError("SPEECHRAIL_RESPONSE_ERROR")


def _response_id(value: object) -> str:
    if isinstance(value, dict):
        candidate = value.get("id")
        if isinstance(candidate, str) and candidate:
            return candidate
    raise SpeechRailProtocolError("SPEECHRAIL_RESPONSE_ERROR")


def _response_status(value: object) -> str:
    if isinstance(value, dict):
        candidate = value.get("status")
        if isinstance(candidate, str):
            return candidate
    return ""


__all__ = ["SpeechRailTTSClient"]
