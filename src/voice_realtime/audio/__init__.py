"""音频子系统：归一化来源、麦克风采集、扇出与 Pipecat 注入。"""

from voice_realtime.audio.audio_injector import AudioInjector
from voice_realtime.audio.frame import (
    AudioFrame,
    AudioFrameFlag,
    AudioSourceKind,
    AudioSourceRole,
)
from voice_realtime.audio.hub import (
    CHANNELS,
    CHUNK_SIZE,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    AudioHub,
)
from voice_realtime.audio.profile import (
    CaptureMode,
    CaptureProfile,
    CaptureSourceSpec,
)
from voice_realtime.audio.router import (
    AudioSourceRouter,
    RouterHealth,
    UnsupportedCaptureProfileError,
)
from voice_realtime.audio.source import (
    AudioSource,
    AudioSourceHealth,
    AudioSourceState,
    MicrophoneSource,
)

__all__ = [
    "CHANNELS",
    "CHUNK_SIZE",
    "SAMPLE_RATE",
    "SAMPLE_WIDTH",
    "AudioFrame",
    "AudioFrameFlag",
    "AudioHub",
    "AudioInjector",
    "AudioSource",
    "AudioSourceHealth",
    "AudioSourceKind",
    "AudioSourceRole",
    "AudioSourceRouter",
    "AudioSourceState",
    "CaptureMode",
    "CaptureProfile",
    "CaptureSourceSpec",
    "MicrophoneSource",
    "RouterHealth",
    "UnsupportedCaptureProfileError",
]
