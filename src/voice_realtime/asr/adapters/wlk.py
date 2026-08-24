"""WhisperLiveKit WebSocket 到统一 ASR 领域事件的适配器。"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from voice_realtime.asr.contracts import (
    ASRCapabilities,
    ASREvent,
    ASRSessionContext,
)
from voice_realtime.meeting.models import NormalizedSegment, TranscriptWindow
from voice_realtime.subtitles.events import SubtitleEvent, SubtitleStream


def _timestamp_ms(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        number = float(value)
        return max(0, round(number * 1000)) if math.isfinite(number) else 0
    raw = str(value or "").strip()
    if not raw:
        return 0
    try:
        if ":" not in raw:
            return max(0, round(float(raw) * 1000))
    except ValueError:
        return 0
    parts = raw.split(":")
    if len(parts) > 3:
        return 0
    try:
        seconds = float(parts[-1])
        minutes = int(parts[-2]) if len(parts) >= 2 else 0
        hours = int(parts[-3]) if len(parts) == 3 else 0
    except ValueError:
        return 0
    value_ms = (hours * 3600 + minutes * 60 + seconds) * 1000
    return max(0, round(value_ms)) if math.isfinite(value_ms) else 0


def _speaker_key(source_epoch: int, raw_speaker: object) -> str:
    raw_value = str(raw_speaker if raw_speaker is not None else "0").strip()
    if raw_value in {"-1", "-2", ""}:
        raw_value = "0"
    return f"epoch:{source_epoch}:speaker:{raw_value}"


class TranscriptNormalizer:
    """将 WLK full snapshot 转换为后端无关领域窗口。"""

    def normalize(
        self,
        payload: Mapping[str, Any],
        source_epoch: int,
        offset_ms: int = 0,
    ) -> TranscriptWindow:
        if source_epoch < 0:
            raise ValueError("source_epoch 必须非负")
        if offset_ms < 0:
            raise ValueError("offset_ms 必须非负")
        lines = payload.get("lines")
        raw_lines = lines if isinstance(lines, list) else []
        segments: list[NormalizedSegment] = []
        for index, raw in enumerate(raw_lines):
            if not isinstance(raw, Mapping):
                continue
            text = str(raw.get("text") or "").strip()
            speaker = raw.get("speaker")
            if not text or speaker == -2:
                continue
            start_ms = _timestamp_ms(raw.get("start")) + offset_ms
            end_ms = max(start_ms, _timestamp_ms(raw.get("end")) + offset_ms)
            speaker_key = _speaker_key(source_epoch, speaker)
            identity = (
                f"{source_epoch}|{speaker_key}|{start_ms}|{end_ms}|{text}|"
                f"{raw.get('translation') or ''}|{raw.get('detected_language') or ''}"
            )
            segments.append(
                NormalizedSegment(
                    id=uuid5(NAMESPACE_URL, f"voice-realtime:segment:{identity}"),
                    order=index,
                    source_epoch=source_epoch,
                    speaker_key=speaker_key,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    text=text,
                    translation=(
                        str(raw["translation"]).strip()
                        if raw.get("translation") is not None
                        else None
                    ),
                    detected_language=(
                        str(raw["detected_language"]).strip()
                        if raw.get("detected_language") is not None
                        else None
                    ),
                )
            )
        segments.sort(key=lambda item: (item.start_ms, item.end_ms, item.order))
        ordered = tuple(
            segment.model_copy(update={"order": order})
            for order, segment in enumerate(segments)
        )
        partial = str(payload.get("buffer_transcription") or "").strip()
        return TranscriptWindow(source_epoch=source_epoch, partial=partial, segments=ordered)


class _WLKEventStream(Protocol):
    @property
    def uri(self) -> str: ...

    async def connect(self) -> None: ...

    async def send_audio(self, chunk: bytes) -> None: ...

    def events(self) -> AsyncIterator[SubtitleEvent]: ...

    async def close(self) -> None: ...


WLKStreamFactory = Callable[..., _WLKEventStream]


class WLKStreamingAdapter:
    """封装 WLK 传输、快照规范化和 EOF 完成语义。"""

    backend_id = "wlk"

    def __init__(
        self,
        *,
        url: str,
        language: str,
        context: ASRSessionContext,
        backend_id: str = "wlk",
        supports_speaker_labels: bool = True,
        token: str | None = None,
        stream_factory: WLKStreamFactory | None = None,
    ) -> None:
        self.backend_id = backend_id
        self._context = context
        resolved_stream_factory = stream_factory or SubtitleStream
        if token is None:
            self._stream = resolved_stream_factory(url=url, language=language)
        else:
            self._stream = resolved_stream_factory(url=url, language=language, token=token)
        self._normalizer = TranscriptNormalizer()
        self._last_window: TranscriptWindow | None = None
        self._final_ready = asyncio.Event()
        self._finish_lock = asyncio.Lock()
        self._eof_sent = False
        self.capabilities = ASRCapabilities(
            languages=frozenset({language}),
            supports_partial=True,
            supports_segment_timestamps=True,
            supports_word_timestamps=False,
            supports_hotwords=False,
            supports_speaker_labels=supports_speaker_labels,
            supports_native_diarization=False,
            supports_eof_flush=True,
        )

    @property
    def uri(self) -> str:
        return self._stream.uri

    async def connect(self) -> None:
        self._final_ready.clear()
        self._eof_sent = False
        await self._stream.connect()

    async def send_audio(self, chunk: bytes) -> None:
        await self._stream.send_audio(chunk)

    def normalize_snapshot(self, payload: dict[str, object]) -> TranscriptWindow:
        """在 vendor 边界把 WLK full snapshot 转为领域窗口。"""
        return self._normalizer.normalize(
            payload,
            source_epoch=self._context.source_epoch,
            offset_ms=self._context.offset_ms,
        )

    async def events(self) -> AsyncIterator[ASREvent]:
        previous_raw: dict[str, object] | None = None
        async for event in self._stream.events():
            if event.kind == "config":
                yield ASREvent(kind="ready")
                continue
            if event.kind == "error":
                message = event.text.strip() or "WhisperLiveKit error"
                yield ASREvent(
                    kind="error",
                    error_code="WLK_ERROR",
                    error_message=message[:1000],
                )
                continue
            if event.kind == "ready_to_stop":
                final = self._last_window or TranscriptWindow(
                    source_epoch=self._context.source_epoch
                )
                self._final_ready.set()
                yield ASREvent(kind="final", window=final)
                continue
            if event.kind not in {"partial", "confirmed"}:
                continue
            raw = event.raw
            if raw is previous_raw:
                continue
            previous_raw = raw
            window = self.normalize_snapshot(raw)
            self._last_window = window
            yield ASREvent(kind="snapshot", window=window)

    async def finish(self) -> TranscriptWindow:
        async with self._finish_lock:
            if not self._eof_sent:
                await self._stream.send_audio(b"")
                self._eof_sent = True
        await self._final_ready.wait()
        return self._last_window or TranscriptWindow(source_epoch=self._context.source_epoch)

    async def close(self) -> None:
        await self._stream.close()
