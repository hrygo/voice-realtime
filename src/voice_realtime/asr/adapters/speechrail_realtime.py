"""Shared client and subtitle/meeting adapter for SpeechRail Realtime v2."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import NAMESPACE_URL, uuid5

from voice_realtime.asr.contracts import ASRCapabilities, ASREvent, ASRSessionContext
from voice_realtime.meeting.models import NormalizedSegment, TranscriptWindow
from voice_realtime.speechrail.transport import ConnectionFactory, SpeechRailRealtimeClient

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
        self._last_window = TranscriptWindow(source_epoch=context.source_epoch)
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
                        segments = _segments(
                            event.get("segments"),
                            self._context,
                            require_speaker=self._diarization_requested,
                        )
                    except RuntimeError:
                        yield self._set_terminal_error(
                            "SPEECHRAIL_PROTOCOL_ERROR",
                            "SpeechRail returned invalid completed segments",
                        )
                        return
                    self._last_window = TranscriptWindow(
                        source_epoch=self._context.source_epoch, segments=segments
                    )
                    if self._diarization_requested:
                        self._observed_speaker_labels.update(
                            _speaker_label(segment.speaker_key) for segment in segments
                        )
                    if not self._diarization_requested:
                        self._final_ready.set()
                        yield ASREvent(kind="final", window=self._last_window)
                        return
                elif event.get("type") == "transcription.diarization.completed":
                    if not self._diarization_requested:
                        yield self._set_terminal_error(
                            "SPEECHRAIL_PROTOCOL_ERROR",
                            "SpeechRail returned diarization for a non-diarized session",
                        )
                        return
                    try:
                        mapping = _speaker_remap(
                            event.get("mapping"),
                            self._context,
                            observed_speakers=self._observed_speaker_labels,
                        )
                    except RuntimeError:
                        yield self._set_terminal_error(
                            "SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR",
                            "SpeechRail returned an invalid diarization mapping",
                        )
                        return
                    self._last_window = self._last_window.model_copy(
                        update={"speaker_remap": mapping}
                    )
                    self._final_ready.set()
                    yield ASREvent(kind="final", window=self._last_window)
                    return
                elif event.get("type") == "session.completed" and self._diarization_requested:
                    yield self._set_terminal_error(
                        "SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR",
                        "SpeechRail ended a diarized session without a final mapping",
                    )
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


def _segments(
    value: object, context: ASRSessionContext, *, require_speaker: bool = False
) -> tuple[NormalizedSegment, ...]:
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
        speaker = raw.get("speaker")
        if speaker is None and not require_speaker:
            speaker_key = "0"
        elif (
            not isinstance(speaker, str)
            or not speaker.startswith("spk_")
            or len(speaker) > 64
        ):
            raise RuntimeError("SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR")
        else:
            speaker_key = speaker
        absolute_start = start_ms + context.offset_ms
        absolute_end = end_ms + context.offset_ms
        result.append(
            NormalizedSegment(
                id=uuid5(NAMESPACE_URL, f"speechrail:{context.source_epoch}:{index}:{text}"),
                order=index,
                source_epoch=context.source_epoch,
                speaker_key=f"epoch:{context.source_epoch}:speaker:{speaker_key}",
                start_ms=absolute_start,
                end_ms=absolute_end,
                text=text,
            )
        )
    return tuple(result)


def _speaker_remap(
    value: object,
    context: ASRSessionContext,
    *,
    observed_speakers: set[str],
) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        raise RuntimeError("SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR")
    mapping: dict[str, str] = {}
    for source, target in value.items():
        if (
            not isinstance(source, str)
            or not isinstance(target, str)
            or not source.startswith("spk_")
            or not target.startswith("spk_")
            or source == target
        ):
            raise RuntimeError("SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR")
        mapping[source] = target
    if not mapping and context.diarization_group_id is None:
        return ()
    if not set(mapping).issubset(observed_speakers):
        raise RuntimeError("SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR")
    source_prefix = f"epoch:{context.source_epoch}:speaker:"
    if context.diarization_group_id is None:
        return tuple(
            (f"{source_prefix}{source}", f"{source_prefix}{target}")
            for source, target in mapping.items()
        )
    target_prefix = f"group:{context.diarization_group_id}:speaker:"
    return tuple(
        (f"{source_prefix}{source}", f"{target_prefix}{mapping.get(source, source)}")
        for source in sorted(observed_speakers)
    )


def _speaker_label(speaker_key: str) -> str:
    return speaker_key.rsplit(":", maxsplit=1)[-1]
