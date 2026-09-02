from __future__ import annotations

import audioop
import difflib
import logging
import re
import time
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)
_PUNCTUATION_RE = re.compile(r"[\s\.,\?!;:，。？！；：、“”‘’\"\'\(\)\[\]（）—\-_…]+", re.UNICODE)
_ACKS = frozenset({"好", "好的", "嗯", "嗯嗯", "行", "可以", "谢谢", "知道了"})


def _normalize_text(text: str) -> str:
    return _PUNCTUATION_RE.sub("", text).strip().lower()


def _rms16(audio: bytes) -> float:
    return float(audioop.rms(audio, 2)) if audio else 0.0


def _pinyin_tokens(text: str) -> list[str]:
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:  # pragma: no cover
        return []
    return [item for item in lazy_pinyin(text, style=Style.NORMAL) if item.isalnum()]


class EchoState:
    def __init__(self) -> None:
        self.bot_speaking = False
        self.last_speaking_stop_time = 0.0
        self._tts_active = False
        self._speaker_active = False
        self.generation = 0

    def on_tts_started(self) -> None:
        active = self._tts_active or self._speaker_active
        self._tts_active = self.bot_speaking = True
        if not active:
            self.generation += 1

    def on_bot_speaking_started(self) -> None:
        active = self._tts_active or self._speaker_active
        self._speaker_active = self.bot_speaking = True
        if not active:
            self.generation += 1

    def on_tts_stopped(self) -> None:
        self._tts_active = False
        if not self._speaker_active:
            self._stop()

    def on_bot_speaking_stopped(self) -> None:
        self._speaker_active = False
        if not self._tts_active:
            self._stop()

    def _stop(self) -> None:
        self.bot_speaking = False
        self.last_speaking_stop_time = time.monotonic()

    def reset(self) -> None:
        self.generation += 1
        self.bot_speaking = self._tts_active = self._speaker_active = False
        self.last_speaking_stop_time = 0.0

    def is_suppressing(self, now: float, tail_hangover_secs: float) -> bool:
        return self.bot_speaking or self._tts_active or self._speaker_active or (
            now - self.last_speaking_stop_time < tail_hangover_secs
        )


class EchoTextBuffer:
    def __init__(self, window_secs: float = 10.0, max_items: int = 16) -> None:
        self._window_secs, self._max_items = window_secs, max_items
        self._items: deque[tuple[float, str]] = deque()

    def add(self, text: str, now: float) -> None:
        if text := text.strip():
            self._items.append((now, text))
            if len(self._items) > self._max_items:
                self._items.popleft()

    def matches(self, text: str, min_ratio: float, min_chars: int, now: float) -> bool:
        candidate = _normalize_text(text)
        if not candidate or len(candidate) < min_chars or candidate in _ACKS:
            return False
        recent = [value for stamp, value in self._items if stamp >= now - self._window_secs]
        combined = "".join(_normalize_text(value) for value in recent)
        if not combined:
            return False
        candidates = [_normalize_text(value) for value in recent] + [combined]
        for bot in candidates:
            if not bot:
                continue
            if candidate in bot or bot in candidate:
                return True
            matcher = difflib.SequenceMatcher(None, candidate, bot)
            if matcher.ratio() >= min_ratio:
                return True
            longest = matcher.find_longest_match(0, len(candidate), 0, len(bot))
            if longest.size and longest.size / len(candidate) >= min_ratio:
                return True
            if 2 <= len(candidate) <= 8 and any(
                difflib.SequenceMatcher(None, candidate, bot[i : i + len(candidate)]).ratio()
                >= max(0.65, min_ratio - 0.1)
                for i in range(max(0, len(bot) - len(candidate) + 1))
            ):
                return True
        candidate_py, bot_py = _pinyin_tokens(candidate), _pinyin_tokens(combined)
        if candidate_py and bot_py:
            compact_candidate, compact_bot = "".join(candidate_py), "".join(bot_py)
            longest = difflib.SequenceMatcher(None, candidate_py, bot_py).find_longest_match(
                0, len(candidate_py), 0, len(bot_py)
            )
            return (
                " ".join(candidate_py) in " ".join(bot_py)
                or (longest.size >= 2 and longest.size / len(candidate_py) >= 0.6)
                or difflib.SequenceMatcher(
                    None, compact_candidate, compact_bot
                ).ratio()
                >= max(0.6, min_ratio - 0.1)
            )
        return False


@dataclass(frozen=True, slots=True)
class EnergyGateDecision:
    allow_audio: bool
    barge_in_started: bool = False
    relocked: bool = False


class AdaptiveEnergyGate:
    def __init__(self, *, gain: float = 2.5, required_frames: int = 3) -> None:
        self.configure(gain=gain, required_frames=required_frames)
        self.reset()

    def configure(self, *, gain: float, required_frames: int) -> None:
        self.gain, self.required_frames = gain, required_frames

    def reset(self) -> None:
        self.echo_rms: deque[float] = deque(maxlen=50)
        self.peak = self.fast = self.slow = 0.0
        self.hot = self.quiet = 0
        self._barge_in_active = False

    @property
    def barge_in_active(self) -> bool:
        return self._barge_in_active

    def observe(self, rms: float) -> EnergyGateDecision:
        if self._barge_in_active:
            if rms <= self.peak * 0.8:
                self.quiet += 1
                if self.quiet >= 20:
                    self._barge_in_active, self.hot, self.quiet = False, 0, 0
                    return EnergyGateDecision(False, relocked=True)
            else:
                self.quiet = 0
            return EnergyGateDecision(True)
        self.echo_rms.append(rms)
        self.fast = rms if self.fast == 0 else 0.3 * rms + 0.7 * self.fast
        self.slow = rms if self.slow == 0 else 0.05 * rms + 0.95 * self.slow
        if len(self.echo_rms) <= (2 if self.gain <= 1.5 else 8):
            self._update(rms)
            return EnergyGateDecision(False)
        if rms > max(self.peak * self.gain, 350.0 if self.gain <= 1.5 else 1200.0):
            self.hot += 1
            if self.hot >= self.required_frames:
                self._barge_in_active, self.quiet = True, 0
                return EnergyGateDecision(True, barge_in_started=True)
            return EnergyGateDecision(False)
        self.hot = 0
        self._update(rms)
        return EnergyGateDecision(False)

    def _update(self, rms: float) -> None:
        self.peak = rms if rms > self.peak else 0.96 * self.peak + 0.04 * rms


@dataclass(frozen=True, slots=True)
class SelfEchoPolicy:
    min_ratio: float
    min_chars: int
    tail_hangover_secs: float

    def should_drop(
        self,
        text: str,
        *,
        now: float,
        protect_next_transcript: bool,
        echo_state: EchoState,
        buffer: EchoTextBuffer,
    ) -> bool:
        return not _normalize_text(text) or (
            not protect_next_transcript
            and echo_state.is_suppressing(now, self.tail_hangover_secs)
            and buffer.matches(text, self.min_ratio, self.min_chars, now)
        )
