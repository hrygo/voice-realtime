"""归一化实时音频帧及来源语义。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntFlag, StrEnum, auto
from uuid import UUID

NORMALIZED_SAMPLE_RATE = 16_000
NORMALIZED_CHANNELS = 1
NORMALIZED_SAMPLE_WIDTH = 2
NORMALIZED_SAMPLES_PER_CHANNEL = 512


class AudioSourceKind(StrEnum):
    """应用层可识别的音频来源种类。"""

    MICROPHONE = "microphone"
    PHYSICAL_OUTPUT = "physical_output"


class AudioSourceRole(StrEnum):
    """来源在会议声学链路中的角色。"""

    NEAR_END = "near_end"
    FAR_END = "far_end"


class AudioFrameFlag(IntFlag):
    """不改变 PCM 格式的帧级状态标记。"""

    NONE = 0
    DISCONTINUITY = auto()
    SILENCE_FILL = auto()
    END_OF_STREAM = auto()


@dataclass(frozen=True, slots=True)
class AudioFrame:
    """路由边界上的不可变 16 kHz mono s16le 音频帧。"""

    capture_id: UUID
    source_id: str
    source_kind: AudioSourceKind
    source_role: AudioSourceRole
    device_generation: int
    sequence: int
    host_time_ns: int
    pcm: bytes
    sample_rate: int = NORMALIZED_SAMPLE_RATE
    channels: int = NORMALIZED_CHANNELS
    sample_width: int = NORMALIZED_SAMPLE_WIDTH
    samples_per_channel: int = NORMALIZED_SAMPLES_PER_CHANNEL
    flags: AudioFrameFlag = AudioFrameFlag.NONE

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise ValueError("source_id must not be empty")
        if self.device_generation < 0:
            raise ValueError("device_generation must be non-negative")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        if self.host_time_ns < 0:
            raise ValueError("host_time_ns must be non-negative")
        if self.sample_rate != NORMALIZED_SAMPLE_RATE:
            raise ValueError(f"sample_rate must be {NORMALIZED_SAMPLE_RATE}")
        if self.channels != NORMALIZED_CHANNELS:
            raise ValueError(f"channels must be {NORMALIZED_CHANNELS}")
        if self.sample_width != NORMALIZED_SAMPLE_WIDTH:
            raise ValueError(f"sample_width must be {NORMALIZED_SAMPLE_WIDTH}")
        if self.samples_per_channel != NORMALIZED_SAMPLES_PER_CHANNEL:
            raise ValueError(
                f"samples_per_channel must be {NORMALIZED_SAMPLES_PER_CHANNEL}"
            )

        expected_size = self.samples_per_channel * self.channels * self.sample_width
        empty_eof = not self.pcm and bool(self.flags & AudioFrameFlag.END_OF_STREAM)
        if not empty_eof and len(self.pcm) != expected_size:
            raise ValueError(
                f"PCM payload must be {expected_size} bytes or an empty EOF frame"
            )

    @property
    def duration_ns(self) -> int:
        """返回帧覆盖的音频时长；空 EOF 不占采样时间。"""
        if not self.pcm:
            return 0
        return self.samples_per_channel * 1_000_000_000 // self.sample_rate
