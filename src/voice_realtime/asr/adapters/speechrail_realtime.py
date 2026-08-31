"""Shared client and subtitle/meeting adapter for SpeechRail Realtime v2."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from voice_realtime.asr.contracts import ASRCapabilities, ASREvent, ASRSessionContext
from voice_realtime.meeting.models import NormalizedSegment, TranscriptWindow


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
        self._session_id: str | None = None
        self._request_id: str | None = None

    @property
    def uri(self) -> str:
        return self._connection.uri if self._connection is not None else self._url

    async def connect(self, *, language: str) -> None:
        if self._connection is not None:
            raise RuntimeError("SPEECHRAIL_ALREADY_CONNECTED")
        self._sequence = 0
        self._session_id = None
        self._request_id = None
        self._connection = await self._connection_factory(self._url)
        try:
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
            await self.receive()
        except BaseException:
            await self.close()
            raise

    async def append_pcm(self, chunk: bytes) -> None:
        if not chunk or len(chunk) % 2:
            raise ValueError("PCM must be non-empty int16")
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(chunk).decode("ascii"),
            }
        )

    async def commit(self) -> None:
        await self._send({"type": "input_audio_buffer.commit"})

    async def cancel(self) -> None:
        await self._send({"type": "session.cancel"})

    async def receive(self) -> dict[str, object]:
        if self._connection is None:
            raise RuntimeError("SPEECHRAIL_NOT_CONNECTED")
        payload = json.loads(await self._connection.recv())
        if not isinstance(payload, dict):
            raise RuntimeError("SPEECHRAIL_PROTOCOL_ERROR")
        sequence = payload.get("sequence")
        if not isinstance(sequence, int) or sequence <= self._sequence:
            raise RuntimeError("SPEECHRAIL_SEQUENCE_ERROR")
        event_type = payload.get("type")
        session_id = payload.get("session_id")
        request_id = payload.get("request_id")
        if (
            not isinstance(event_type, str)
            or not isinstance(session_id, str)
            or not session_id
            or not isinstance(request_id, str)
            or not request_id
        ):
            raise RuntimeError("SPEECHRAIL_PROTOCOL_ERROR")
        if self._session_id is None:
            if event_type != "session.created":
                raise RuntimeError("SPEECHRAIL_SESSION_CREATE_FAILED")
            self._session_id = session_id
            self._request_id = request_id
        elif session_id != self._session_id:
            raise RuntimeError("SPEECHRAIL_SESSION_MISMATCH")
        elif request_id != self._request_id:
            raise RuntimeError("SPEECHRAIL_REQUEST_MISMATCH")
        self._sequence = sequence
        return {str(key): value for key, value in payload.items()}

    async def close(self) -> None:
        if self._connection is not None:
            try:
                await self._connection.close()
            finally:
                self._connection = None
                self._session_id = None
                self._request_id = None

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
        self,
        *,
        client: SpeechRailRealtimeClient,
        context: ASRSessionContext,
        language: str,
        finish_timeout_secs: float = 10.0,
    ) -> None:
        if finish_timeout_secs <= 0:
            raise ValueError("finish_timeout_secs must be positive")
        self._client = client
        self._context = context
        self._language = language
        self._finish_timeout_secs = finish_timeout_secs
        self._ready = False
        self._last_window = TranscriptWindow(source_epoch=context.source_epoch)
        self._final_ready = asyncio.Event()
        self._finish_lock = asyncio.Lock()
        self._commit_sent = False
        self._events_active = False
        self._terminal_error: tuple[str, str] | None = None

    @property
    def uri(self) -> str:
        return self._client.uri

    async def connect(self) -> None:
        await self._client.connect(language=self._language)
        self._ready = True

    async def send_audio(self, chunk: bytes) -> None:
        await self._client.append_pcm(chunk)

    async def events(self) -> AsyncIterator[ASREvent]:
        if self._events_active:
            raise RuntimeError("SPEECHRAIL_EVENTS_ALREADY_CONSUMED")
        self._events_active = True
        if self._ready:
            self._ready = False
            yield ASREvent(kind="ready")
        try:
            while True:
                event = await self._client.receive()
                if event.get("type") == "transcription.delta":
                    text = event.get("text")
                    if not isinstance(text, str):
                        yield self._set_terminal_error(
                            "SPEECHRAIL_PROTOCOL_ERROR",
                            "SpeechRail returned a transcription delta without text",
                        )
                        return
                    self._last_window = TranscriptWindow(
                        source_epoch=self._context.source_epoch, partial=text
                    )
                    yield ASREvent(kind="snapshot", window=self._last_window)
                elif event.get("type") == "transcription.completed":
                    try:
                        segments = _segments(event.get("segments"), self._context)
                    except RuntimeError:
                        yield self._set_terminal_error(
                            "SPEECHRAIL_PROTOCOL_ERROR",
                            "SpeechRail returned invalid completed segments",
                        )
                        return
                    self._last_window = TranscriptWindow(
                        source_epoch=self._context.source_epoch, segments=segments
                    )
                    self._final_ready.set()
                    yield ASREvent(kind="final", window=self._last_window)
                    return
                elif event.get("type") == "error":
                    yield self._set_terminal_error(
                        "SPEECHRAIL_REQUEST_FAILED",
                        "SpeechRail rejected the transcription request",
                    )
                    return
        finally:
            self._events_active = False

    async def finish(self) -> TranscriptWindow:
        async with self._finish_lock:
            if not self._commit_sent:
                await self._client.commit()
                self._commit_sent = True
        try:
            await asyncio.wait_for(self._final_ready.wait(), timeout=self._finish_timeout_secs)
        except TimeoutError:
            raise TimeoutError("SPEECHRAIL_FINAL_TIMEOUT: final result was not received") from None
        if self._terminal_error is not None:
            code, message = self._terminal_error
            raise RuntimeError(f"{code}: {message}")
        return self._last_window

    async def close(self) -> None:
        if self._terminal_error is None and not self._final_ready.is_set():
            self._terminal_error = ("SPEECHRAIL_CLOSED", "SpeechRail connection closed")
            self._final_ready.set()
        await self._client.close()

    def _set_terminal_error(self, code: str, message: str) -> ASREvent:
        self._terminal_error = (code, message)
        self._final_ready.set()
        return ASREvent(kind="error", error_code=code, error_message=message)


def _segments(value: object, context: ASRSessionContext) -> tuple[NormalizedSegment, ...]:
    if not isinstance(value, list):
        raise RuntimeError("SPEECHRAIL_PROTOCOL_ERROR")
    result: list[NormalizedSegment] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise RuntimeError("SPEECHRAIL_PROTOCOL_ERROR")
        text = raw.get("text")
        start_ms = raw.get("start_ms")
        end_ms = raw.get("end_ms")
        if (
            not isinstance(text, str)
            or not text.strip()
            or not isinstance(start_ms, int)
            or not isinstance(end_ms, int)
            or start_ms < 0
            or end_ms < start_ms
        ):
            raise RuntimeError("SPEECHRAIL_PROTOCOL_ERROR")
        absolute_start = start_ms + context.offset_ms
        absolute_end = end_ms + context.offset_ms
        result.append(
            NormalizedSegment(
                id=uuid5(NAMESPACE_URL, f"speechrail:{context.source_epoch}:{index}:{text}"),
                order=index,
                source_epoch=context.source_epoch,
                speaker_key=f"epoch:{context.source_epoch}:speaker:0",
                start_ms=absolute_start,
                end_ms=absolute_end,
                text=text,
            )
        )
    return tuple(result)
