"""音频子系统：AudioHub 单源采集 + 扇出 + AudioInjector 注入 Pipecat。"""

from voice_realtime.audio.audio_injector import AudioInjector
from voice_realtime.audio.hub import (
    CHANNELS,
    CHUNK_SIZE,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    AudioHub,
)

__all__ = [
    "CHANNELS",
    "CHUNK_SIZE",
    "SAMPLE_RATE",
    "SAMPLE_WIDTH",
    "AudioHub",
    "AudioInjector",
]
