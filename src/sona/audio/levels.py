"""服务端 PCM16 能量计算与低频权威状态发布节流。"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from sona.audio.frame import AudioSourceKind

DEFAULT_PUBLISH_INTERVAL_NS = 50_000_000


@dataclass(frozen=True, slots=True)
class AudioLevels:
    """不包含 PCM 的归一化来源能量快照。"""

    microphone: float = 0.0
    physical_output: float = 0.0
    mixed: float = 0.0
    updated_at_ns: int = 0


def pcm16_level(pcm: bytes) -> float:
    """把 little-endian signed PCM16 映射到稳定的 0..1 dBFS 能量。"""
    if len(pcm) % 2:
        raise ValueError("PCM16 payload length must be even")
    if not pcm:
        return 0.0
    samples = memoryview(pcm).cast("h")
    sum_squares = sum(int(sample) ** 2 for sample in samples)
    rms = math.sqrt(sum_squares / len(samples))
    if rms == 0.0:
        return 0.0
    dbfs = 20.0 * math.log10(rms / 32768.0)
    return min(1.0, max(0.0, (dbfs + 60.0) / 60.0))


class AudioLevelMeter:
    """跟踪来源能量，并限制 runtime snapshot 广播频率。"""

    def __init__(
        self,
        *,
        publish_interval_ns: int = DEFAULT_PUBLISH_INTERVAL_NS,
    ) -> None:
        if publish_interval_ns < 0:
            raise ValueError("publish_interval_ns must be non-negative")
        self._publish_interval_ns = publish_interval_ns
        self._levels = AudioLevels()
        self._last_publish_ns: int | None = None

    def update(
        self,
        kind: AudioSourceKind,
        pcm: bytes,
        *,
        now_ns: int | None = None,
    ) -> bool:
        """更新单来源能量；返回本次是否达到发布间隔。"""
        timestamp = time.monotonic_ns() if now_ns is None else now_ns
        if timestamp < 0:
            raise ValueError("now_ns must be non-negative")
        level = pcm16_level(pcm)
        if kind is AudioSourceKind.MICROPHONE:
            self._levels = AudioLevels(
                microphone=level,
                physical_output=self._levels.physical_output,
                mixed=level,
                updated_at_ns=timestamp,
            )
        else:
            self._levels = AudioLevels(
                microphone=self._levels.microphone,
                physical_output=level,
                mixed=level,
                updated_at_ns=timestamp,
            )

        last_publish_ns = self._last_publish_ns
        if (
            last_publish_ns is None
            or timestamp < last_publish_ns
            or timestamp - last_publish_ns >= self._publish_interval_ns
        ):
            self._last_publish_ns = timestamp
            return True
        return False

    def clear(
        self,
        kind: AudioSourceKind,
        *,
        now_ns: int | None = None,
    ) -> None:
        """清零指定来源及当前单来源 mixed 值。"""
        timestamp = time.monotonic_ns() if now_ns is None else now_ns
        if timestamp < 0:
            raise ValueError("now_ns must be non-negative")
        if kind is AudioSourceKind.MICROPHONE:
            self._levels = AudioLevels(
                microphone=0.0,
                physical_output=self._levels.physical_output,
                mixed=0.0,
                updated_at_ns=timestamp,
            )
        else:
            self._levels = AudioLevels(
                microphone=self._levels.microphone,
                physical_output=0.0,
                mixed=0.0,
                updated_at_ns=timestamp,
            )
        self._last_publish_ns = timestamp

    def snapshot(self) -> AudioLevels:
        """返回当前不可变快照。"""
        return self._levels
