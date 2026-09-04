"""Shared subtitle/meeting adapter for SpeechRail OpenAI Realtime ASR.

Consumes SpeechRail ``WS /v1/realtime`` transcription events and projects them
onto the neutral :class:`ASREvent` / :class:`ASRWindow` domain contract.  The
meeting path enables the session-scoped ``diarization`` profile (via
``session.update``) and rewrites the anonymous ``spk_*`` labels into the
application's stable ``group:{id}`` speaker namespace so downstream mapping/
rename semantics are preserved.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from sona.asr.contracts import ASRCapabilities, ASREvent, ASRSessionContext
from sona.asr.models import ASRSegment, ASRWindow
from sona.speechrail.transcription_events import (
    Noop,
    SpeechRailTranscriptionError,
    TranscriptionCompleted,
    TranscriptionDelta,
    TranscriptionSegment,
    decode_transcription_event,
)
from sona.speechrail.transport import (
    DEFAULT_SERVER_VAD,
    ConnectionFactory,
    SpeechRailProtocolError,
    SpeechRailRealtimeClient,
)

__all__ = ["ConnectionFactory", "SpeechRailRealtimeClient", "SpeechRailStreamingTranscriber"]

_BYTES_PER_MS = 32_000 / 1_000  # 16 kHz mono s16le bytes per millisecond


class SpeechRailStreamingTranscriber:
    backend_id = "speechrail-openai-realtime"
    capabilities = ASRCapabilities(
        languages=frozenset({"Chinese", "English", "zh", "en"}),
        supports_partial=True,
        supports_segment_timestamps=False,
        supports_word_timestamps=False,
        supports_hotwords=False,
        supports_speaker_labels=True,
        supports_native_diarization=True,
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
        self._last_window = ASRWindow(source_epoch=context.source_epoch)
        self._pending_segments: list[ASRSegment] = []
        self._audio_ms = 0
        self._final_ready = asyncio.Event()
        self._finish_lock = asyncio.Lock()
        self._commit_sent = False
        self._events_active = False
        self._terminal_error: tuple[str, str] | None = None
        self._diarization_requested = context.purpose == "meeting"

    @property
    def uri(self) -> str:
        return self._client.uri

    async def connect(self) -> None:
        await self._client.connect(
            language=self._language,
            diarization=self._diarization_requested,
            speaker_count_hint=self._context.speaker_count_hint,
            diarization_group_id=self._context.diarization_group_id,
            turn_detection=DEFAULT_SERVER_VAD,
        )
        self._ready = True

    async def send_audio(self, chunk: bytes) -> None:
        await self._client.append_pcm(chunk)
        self._audio_ms += int(len(chunk) / _BYTES_PER_MS)

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
                try:
                    decoded = decode_transcription_event(event)
                except SpeechRailProtocolError:
                    yield self._set_terminal_error(*_terminal_error_for(event))
                    return
                if isinstance(decoded, Noop):
                    continue
                if isinstance(decoded, TranscriptionDelta):
                    self._last_window = ASRWindow(
                        source_epoch=self._context.source_epoch, partial=decoded.text
                    )
                    yield ASREvent(kind="snapshot", window=self._last_window)
                elif isinstance(decoded, TranscriptionSegment):
                    try:
                        self._pending_segments.append(
                            _segment(
                                decoded,
                                self._context,
                                require_speaker=self._diarization_requested,
                            )
                        )
                    except RuntimeError:
                        yield self._set_terminal_error(
                            "SPEECHRAIL_PROTOCOL_ERROR",
                            "SpeechRail returned an invalid transcription segment",
                        )
                        return
                elif isinstance(decoded, TranscriptionCompleted):
                    # 分人会话也可能收到无 segment 事件的 completed（服务端对短促/
                    # 单人轮次只下发 completed）。此时用兜底单 segment 保留本轮转写，
                    # 仅当 transcript 为空才视为真正的协议违反。
                    try:
                        segments = tuple(self._pending_segments) or (_synthesized_segment(
                            decoded.transcript, self._context, self._audio_ms
                        ),)
                    except RuntimeError:
                        yield self._set_terminal_error(
                            "SPEECHRAIL_PROTOCOL_ERROR",
                            "SpeechRail returned an invalid completed transcript",
                        )
                        return
                    self._pending_segments.clear()
                    self._last_window = ASRWindow(
                        source_epoch=self._context.source_epoch,
                        partial="",
                        segments=segments,
                    )
                    self._final_ready.set()
                    yield ASREvent(kind="final", window=self._last_window)
                    if self._commit_sent:
                        return
                elif isinstance(decoded, SpeechRailTranscriptionError):
                    yield self._set_terminal_error(
                        "SPEECHRAIL_REQUEST_FAILED",
                        "SpeechRail rejected the transcription request",
                    )
                    return
        finally:
            self._events_active = False

    async def finish(self) -> ASRWindow:
        async with self._finish_lock:
            if not self._commit_sent:
                self._final_ready.clear()
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


def _segment(
    value: TranscriptionSegment,
    context: ASRSessionContext,
    *,
    require_speaker: bool,
) -> ASRSegment:
    # 无说话人标注的 segment 落到匿名说话人，不断连（短轮次常见）。
    speaker_key = "0" if value.speaker is None else value.speaker
    return ASRSegment(
        order=0,
        source_epoch=context.source_epoch,
        speaker_key=_speaker_key(context, speaker_key),
        start_ms=value.start_ms + context.offset_ms,
        end_ms=value.end_ms + context.offset_ms,
        text=value.text,
    )


def _synthesized_segment(
    transcript: str, context: ASRSessionContext, audio_ms: int
) -> ASRSegment:
    """Fallback single segment for the non-diarized path (no segment events)."""
    segment = transcript.strip()
    if not segment:
        raise RuntimeError("SPEECHRAIL_PROTOCOL_ERROR")
    return ASRSegment(
        order=0,
        source_epoch=context.source_epoch,
        speaker_key=_speaker_key(context, "0"),
        start_ms=max(0, audio_ms - _nominal_duration_ms(segment)),
        end_ms=audio_ms,
        text=segment,
    )


def _nominal_duration_ms(text: str) -> int:
    """Best-effort duration for a fallback subtitle segment."""
    return int(max(len(text) * 120, 400))


def _speaker_key(context: ASRSessionContext, speaker: str) -> str:
    if context.purpose == "meeting" and context.diarization_group_id is not None:
        return f"group:{context.diarization_group_id}:speaker:{speaker}"
    return f"epoch:{context.source_epoch}:speaker:{speaker}"


def _terminal_error_for(event: dict[str, object]) -> tuple[str, str]:
    event_type = event.get("type")
    if event_type == "conversation.item.input_audio_transcription.delta":
        return "SPEECHRAIL_PROTOCOL_ERROR", "SpeechRail returned a transcription delta without text"
    if event_type == "conversation.item.input_audio_transcription.completed":
        return "SPEECHRAIL_PROTOCOL_ERROR", "SpeechRail returned an invalid completed transcript"
    if event_type == "conversation.item.input_audio_transcription.segment":
        return (
            "SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR",
            "SpeechRail returned an invalid transcription segment",
        )
    return "SPEECHRAIL_PROTOCOL_ERROR", "SpeechRail returned an invalid transcription event"
