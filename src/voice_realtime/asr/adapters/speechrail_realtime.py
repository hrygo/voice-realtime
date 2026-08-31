"""Shared client and subtitle/meeting adapter for SpeechRail Realtime v2."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

from voice_realtime.asr.contracts import ASRCapabilities, ASREvent, ASRSessionContext
from voice_realtime.meeting.models import TranscriptWindow


class SpeechRailConnection(Protocol):
    uri: str

    async def send(self, payload: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self) -> None: ...


ConnectionFactory = Callable[[str], Awaitable[SpeechRailConnection]]


class SpeechRailRealtimeClient:
    """One non-resumable SpeechRail v2 transcription connection."""

    def __init__(self, *, url: str, connection_factory: ConnectionFactory) -> None:
        self._url = url
        self._connection_factory = connection_factory
        self._connection: SpeechRailConnection | None = None
        self._sequence = 0

    @property
    def uri(self) -> str:
        return self._connection.uri if self._connection is not None else self._url

    async def connect(self, *, language: str) -> None:
        self._connection = await self._connection_factory(self._url)
        await self._send(
            {
                "type": "session.update",
                "session": {
                    "type": "transcription",
                    "language": language,
                    "audio_format": {
                        "type": "audio/pcm",
                        "rate": 16000,
                        "channels": 1,
                        "sample_width": 2,
                    },
                    "endpointing": {"mode": "manual"},
                },
            }
        )
        created = await self.receive()
        if created.get("type") != "session.created":
            raise RuntimeError("SPEECHRAIL_SESSION_CREATE_FAILED")

    async def append_pcm(self, chunk: bytes) -> None:
        if not chunk or len(chunk) % 2:
            raise ValueError("PCM must be non-empty int16")
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(chunk).decode("ascii"),
            }
        )

    async def receive(self) -> dict[str, object]:
        if self._connection is None:
            raise RuntimeError("SPEECHRAIL_NOT_CONNECTED")
        payload = json.loads(await self._connection.recv())
        if not isinstance(payload, dict):
            raise RuntimeError("SPEECHRAIL_PROTOCOL_ERROR")
        sequence = payload.get("sequence")
        if not isinstance(sequence, int) or sequence <= self._sequence:
            raise RuntimeError("SPEECHRAIL_SEQUENCE_ERROR")
        self._sequence = sequence
        return {str(key): value for key, value in payload.items()}

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def _send(self, payload: dict[str, object]) -> None:
        if self._connection is None:
            raise RuntimeError("SPEECHRAIL_NOT_CONNECTED")
        await self._connection.send(json.dumps(payload, separators=(",", ":")))


class SpeechRailStreamingTranscriber:
    backend_id = "speechrail-realtime-v2"
    capabilities = ASRCapabilities(
        languages=frozenset({"Chinese", "English", "zh", "en"}),
        supports_partial=True,
        supports_segment_timestamps=True,
        supports_word_timestamps=False,
        supports_hotwords=False,
        supports_speaker_labels=False,
        supports_native_diarization=False,
        supports_eof_flush=True,
    )

    def __init__(
        self, *, client: SpeechRailRealtimeClient, context: ASRSessionContext, language: str
    ) -> None:
        self._client = client
        self._context = context
        self._language = language
        self._ready = False
        self._last_window = TranscriptWindow(source_epoch=context.source_epoch)

    @property
    def uri(self) -> str:
        return self._client.uri

    async def connect(self) -> None:
        await self._client.connect(language=self._language)
        self._ready = True

    async def send_audio(self, chunk: bytes) -> None:
        await self._client.append_pcm(chunk)

    async def events(self) -> AsyncIterator[ASREvent]:
        if self._ready:
            self._ready = False
            yield ASREvent(kind="ready")
        while True:
            event = await self._client.receive()
            if event.get("type") == "transcription.delta":
                text = event.get("text")
                if not isinstance(text, str):
                    raise RuntimeError("SPEECHRAIL_PROTOCOL_ERROR")
                self._last_window = TranscriptWindow(
                    source_epoch=self._context.source_epoch, partial=text
                )
                yield ASREvent(kind="snapshot", window=self._last_window)

    async def finish(self) -> TranscriptWindow:
        await self._client._send({"type": "input_audio_buffer.commit"})
        return self._last_window

    async def close(self) -> None:
        await self._client.close()
