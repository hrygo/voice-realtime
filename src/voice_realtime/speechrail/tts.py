"""SpeechRail Realtime v2 outbound adapter for text-to-speech."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from voice_realtime.speechrail.transport import (
    ConnectionFactory,
    SpeechRailProtocolError,
    SpeechRailV2Transport,
    decode_pcm16,
)


class SpeechRailTTSClient(SpeechRailV2Transport):
    """One non-resumable speech session; the caller owns playback and interruption."""

    def __init__(
        self,
        *,
        url: str,
        model: str,
        voice: str,
        language: str = "auto",
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        super().__init__(url=url, connection_factory=connection_factory)
        self._model = model
        self._voice = "default" if voice == "alloy" else voice
        self._language = language or "auto"
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
        await self.connect_session(
            {
                "type": "speech",
                "model": self._model,
                "voice": self._voice,
                "language": self._language,
                "audio_format": {
                    "type": "audio/pcm",
                    "rate": 24_000,
                    "channels": 1,
                    "sample_width": 2,
                },
            }
        )
        expected_chunk_index = 0
        completed_response = False
        try:
            await self.send_event({"type": "speech_input.append", "text": text})
            await self.send_event({"type": "speech_input.commit", "speed": speed})
            while True:
                event = await self.receive()
                event_type = event["type"]
                if event_type == "response.created":
                    response_id = event.get("response_id")
                    if not isinstance(response_id, str) or not response_id:
                        raise SpeechRailProtocolError("SPEECHRAIL_RESPONSE_ERROR")
                    if self._active_response_id is not None:
                        raise SpeechRailProtocolError("SPEECHRAIL_RESPONSE_ERROR")
                    self._active_response_id = response_id
                    expected_chunk_index = 0
                elif event_type == "response.audio.delta":
                    response_id = event.get("response_id")
                    chunk_index = event.get("chunk_index")
                    if (
                        response_id != self._active_response_id
                        or not isinstance(chunk_index, int)
                        or chunk_index != expected_chunk_index
                    ):
                        raise SpeechRailProtocolError("SPEECHRAIL_AUDIO_ORDER_ERROR")
                    audio = decode_pcm16(event.get("audio"))
                    expected_chunk_index += 1
                    yield audio
                elif event_type in {
                    "response.audio.completed",
                    "response.audio.cancelled",
                }:
                    if event.get("response_id") != self._active_response_id:
                        raise SpeechRailProtocolError("SPEECHRAIL_RESPONSE_ERROR")
                    self._active_response_id = None
                    completed_response = True
                elif event_type == "session.completed":
                    if not completed_response:
                        raise SpeechRailProtocolError("SPEECHRAIL_RESPONSE_ERROR")
                    return
                elif event_type == "error":
                    error = event.get("error")
                    code = error.get("code") if isinstance(error, dict) else None
                    raise SpeechRailProtocolError(
                        code if isinstance(code, str) and code else "SPEECHRAIL_REQUEST_FAILED"
                    )
                else:
                    raise SpeechRailProtocolError("SPEECHRAIL_EVENT_ERROR")
        except asyncio.CancelledError:
            response_id = self._active_response_id
            if response_id is not None:
                async with asyncio.timeout(0.5):
                    await self.cancel_response(response_id)
            raise
        finally:
            self._active_response_id = None
            await self.close()

    async def cancel_response(self, response_id: str) -> None:
        if not response_id.strip():
            raise ValueError("response_id must not be blank")
        await self.send_event({"type": "response.cancel", "response_id": response_id})

    async def cancel(self, response_id: str) -> None:
        """Cancel the active response, keeping socket teardown with ``synthesize``."""
        if response_id != self._active_response_id:
            return
        try:
            await self.cancel_response(response_id)
        finally:
            self._active_response_id = None


__all__ = ["SpeechRailTTSClient"]
