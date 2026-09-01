"""Shared client and subtitle/meeting adapter for SpeechRail Realtime v2."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from voice_realtime.asr.contracts import ASRCapabilities, ASREvent, ASRSessionContext
from voice_realtime.asr.models import ASRSegment, ASRWindow
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
from voice_realtime.speechrail.transport import (
    ConnectionFactory,
    SpeechRailProtocolError,
    SpeechRailRealtimeClient,
)

__all__ = ["ConnectionFactory", "SpeechRailRealtimeClient", "SpeechRailStreamingTranscriber"]


class SpeechRailStreamingTranscriber:
    backend_id = "speechrail-realtime-v2"
    capabilities = ASRCapabilities(
        languages=frozenset({"Chinese", "English", "zh", "en"}),
        supports_partial=True,
        supports_segment_timestamps=True,
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
        self._final_ready = asyncio.Event()
        self._finish_lock = asyncio.Lock()
        self._commit_sent = False
        self._events_active = False
        self._terminal_error: tuple[str, str] | None = None
        self._diarization_requested = context.purpose == "meeting"
        self._observed_speaker_labels: set[str] = set()

    @property
    def uri(self) -> str:
        return self._client.uri

    async def connect(self) -> None:
        await self._client.connect(
            language=self._language,
            diarization=self._diarization_requested,
            speaker_count_hint=self._context.speaker_count_hint,
            diarization_group_id=self._context.diarization_group_id,
        )
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
                try:
                    decoded = decode_transcription_event(event)
                except SpeechRailProtocolError:
                    code, message = _terminal_error_for(event)
                    yield self._set_terminal_error(code, message)
                    return
                if isinstance(decoded, InputAudioAck):
                    continue
                if isinstance(decoded, TranscriptionDelta):
                    self._last_window = ASRWindow(
                        source_epoch=self._context.source_epoch, partial=decoded.text
                    )
                    yield ASREvent(kind="snapshot", window=self._last_window)
                elif isinstance(decoded, TranscriptionCompleted):
                    try:
                        segments = _segments(
                            decoded.segments,
                            self._context,
                            require_speaker=self._diarization_requested,
                        )
                    except RuntimeError:
                        yield self._set_terminal_error(
                            "SPEECHRAIL_PROTOCOL_ERROR",
                            "SpeechRail returned invalid completed segments",
                        )
                        return
                    self._last_window = ASRWindow(
                        source_epoch=self._context.source_epoch, segments=segments
                    )
                    if self._diarization_requested:
                        self._observed_speaker_labels.update(
                            _speaker_label(segment.speaker_key) for segment in segments
                        )
                    else:
                        self._final_ready.set()
                        yield ASREvent(kind="final", window=self._last_window)
                        return
                elif isinstance(decoded, DiarizationCompleted):
                    if not self._diarization_requested:
                        yield self._set_terminal_error(
                            "SPEECHRAIL_PROTOCOL_ERROR",
                            "SpeechRail returned diarization for a non-diarized session",
                        )
                        return
                    try:
                        mapping = _speaker_remap(
                            decoded.mapping,
                            self._context,
                            observed_speakers=self._observed_speaker_labels,
                        )
                    except RuntimeError:
                        yield self._set_terminal_error(
                            "SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR",
                            "SpeechRail returned an invalid diarization mapping",
                        )
                        return
                    self._last_window = ASRWindow(
                        source_epoch=self._context.source_epoch,
                        partial=self._last_window.partial,
                        partial_speaker_key=self._last_window.partial_speaker_key,
                        segments=self._last_window.segments,
                        speaker_remap=mapping,
                    )
                    self._final_ready.set()
                    yield ASREvent(kind="final", window=self._last_window)
                    return
                elif isinstance(decoded, SessionCompleted):
                    if self._diarization_requested:
                        yield self._set_terminal_error(
                            "SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR",
                            "SpeechRail ended a diarized session without a final mapping",
                        )
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


def _segments(
    value: tuple[SpeechRailSegment, ...],
    context: ASRSessionContext,
    *,
    require_speaker: bool = False,
) -> tuple[ASRSegment, ...]:
    result: list[ASRSegment] = []
    for index, segment in enumerate(value):
        if segment.speaker is None:
            if require_speaker:
                raise RuntimeError("SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR")
            speaker_key = "0"
        else:
            speaker_key = segment.speaker
        result.append(
            ASRSegment(
                order=index,
                source_epoch=context.source_epoch,
                speaker_key=f"epoch:{context.source_epoch}:speaker:{speaker_key}",
                start_ms=segment.start_ms + context.offset_ms,
                end_ms=segment.end_ms + context.offset_ms,
                text=segment.text,
            )
        )
    return tuple(result)


def _speaker_remap(
    value: tuple[tuple[str, str], ...],
    context: ASRSessionContext,
    *,
    observed_speakers: set[str],
) -> tuple[tuple[str, str], ...]:
    mapping: dict[str, str] = dict(value)
    if not mapping and context.diarization_group_id is None:
        return ()
    if not set(mapping).issubset(observed_speakers):
        raise RuntimeError("SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR")
    source_prefix = f"epoch:{context.source_epoch}:speaker:"
    if context.diarization_group_id is None:
        return tuple(
            (f"{source_prefix}{source}", f"{source_prefix}{target}")
            for source, target in value
        )
    target_prefix = f"group:{context.diarization_group_id}:speaker:"
    return tuple(
        (f"{source_prefix}{source}", f"{target_prefix}{mapping.get(source, source)}")
        for source in sorted(observed_speakers)
    )


def _terminal_error_for(event: dict[str, object]) -> tuple[str, str]:
    """Map a decoder failure to the pre-existing user-visible error semantics."""
    event_type = event.get("type")
    if event_type == "transcription.delta":
        return "SPEECHRAIL_PROTOCOL_ERROR", "SpeechRail returned a transcription delta without text"
    if event_type == "transcription.completed":
        return "SPEECHRAIL_PROTOCOL_ERROR", "SpeechRail returned invalid completed segments"
    if event_type == "transcription.diarization.completed":
        return (
            "SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR",
            "SpeechRail returned an invalid diarization mapping",
        )
    return "SPEECHRAIL_PROTOCOL_ERROR", "SpeechRail returned an invalid transcription event"


def _speaker_label(speaker_key: str) -> str:
    return speaker_key.rsplit(":", maxsplit=1)[-1]
