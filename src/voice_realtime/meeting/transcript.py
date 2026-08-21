"""WhisperLiveKit 全量快照的规范化与轻量对账状态。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from voice_realtime.meeting.models import NormalizedSegment, TranscriptWindow


def _timestamp_ms(value: object) -> int:
    """把 WLK 常见的时钟字符串或数值时间转换成毫秒。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return 0
        # WLK JSON 使用秒；整数毫秒在内部测试/适配器中也很常见。
        return max(0, round(number * 1000))
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
    """生成不透明、仅在当前 ASR epoch 内稳定的匿名 speaker key。"""
    value = str(raw_speaker if raw_speaker is not None else "1").strip() or "1"
    return f"epoch:{source_epoch}:speaker:{value}"


class TranscriptNormalizer:
    """将 WhisperLiveKit 的 full snapshot 转换成共享领域模型。"""

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
            end_ms = _timestamp_ms(raw.get("end")) + offset_ms
            end_ms = max(start_ms, end_ms)
            speaker_key = _speaker_key(source_epoch, speaker)
            # UUID5 makes repeated full snapshots idempotent while a WLK text or
            # diarization revision naturally receives a new segment identity.
            identity = (
                f"{source_epoch}|{speaker_key}|{start_ms}|{end_ms}|{text}|"
                f"{raw.get('translation') or ''}|{raw.get('detected_language') or ''}"
            )
            segment = NormalizedSegment(
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
            segments.append(segment)
        segments.sort(key=lambda item: (item.start_ms, item.end_ms, item.order))
        ordered = tuple(
            segment.model_copy(update={"order": order})
            for order, segment in enumerate(segments)
        )
        partial = str(payload.get("buffer_transcription") or "").strip()
        return TranscriptWindow(source_epoch=source_epoch, partial=partial, segments=ordered)


class TranscriptAccumulator:
    """在内存中抑制重复快照，并记录当前可持久化窗口。"""

    def __init__(self) -> None:
        self._last_signature: tuple[Any, ...] | None = None
        self._window: TranscriptWindow | None = None

    @property
    def current(self) -> TranscriptWindow | None:
        return self._window

    def apply(self, window: TranscriptWindow) -> bool:
        """应用快照；返回是否相对上次发生了 durable/partial 变化。"""
        signature = (
            window.source_epoch,
            window.partial,
            tuple(
                (
                    segment.source_epoch,
                    segment.start_ms,
                    segment.end_ms,
                    segment.speaker_key,
                    segment.text,
                    segment.translation,
                    segment.detected_language,
                )
                for segment in window.segments
            ),
        )
        if signature == self._last_signature:
            return False
        self._last_signature = signature
        self._window = window
        return True
