"""后端无关的转录窗口对账状态。"""

from __future__ import annotations

from typing import Any

from sona.meeting.models import TranscriptWindow


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
            window.partial_speaker_key,
            window.partial_speaker_name,
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
