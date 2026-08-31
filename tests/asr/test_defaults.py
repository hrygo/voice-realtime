"""默认 ASR registry 组合与 profile 接线测试。"""

from __future__ import annotations

from typing import Any

from voice_realtime.asr.adapters.funasr_nano_ws import FunASRNanoWSAdapter
from voice_realtime.asr.adapters.qwen3_native import Qwen3WorkerIdentity
from voice_realtime.asr.adapters.speechrail_realtime import SpeechRailStreamingTranscriber
from voice_realtime.asr.contracts import ASRSessionContext
from voice_realtime.asr.defaults import (
    build_funasr_nano_ws_registry,
    build_qwen3_native_registry,
    build_sensevoice_native_registry,
    build_speechrail_realtime_registry,
)
from voice_realtime.asr.profiles import (
    FunASRNanoWSProfile,
    Qwen3NativeProfile,
    SenseVoiceNativeProfile,
    SpeechRailRealtimeProfile,
)


def test_funasr_registry_freezes_profile_runtime_controls() -> None:
    profile = FunASRNanoWSProfile(
        model_dir="/model-cache/FunAudioLLM--Fun-ASR-Nano-2512/snapshots/master",
        language="中文",
        host="127.0.0.1",
        port=10095,
        hotwords=("Voice Studio",),
        connect_timeout_secs=3.0,
        final_timeout_secs=12.0,
    )
    registry = build_funasr_nano_ws_registry("ws://127.0.0.1:10095")

    backend = registry.create_streaming(
        profile,
        ASRSessionContext(source_epoch=1, offset_ms=0, purpose="subtitles"),
    )

    assert isinstance(backend, FunASRNanoWSAdapter)
    assert backend.backend_id == "funasr-nano-ws"
    assert backend.uri == "ws://127.0.0.1:10095"
    assert not backend.capabilities.supports_segment_timestamps


def test_sensevoice_native_registry_accepts_alias_language() -> None:
    def engine(audio: object, *, language: str, use_itn: bool) -> object:
        del audio, language, use_itn
        return [{"text": "结果"}]

    profile = SenseVoiceNativeProfile(
        model_dir="/model-cache/iic--SenseVoiceSmall/snapshots/master",
        language="中文",
    )
    registry = build_sensevoice_native_registry(engine)

    backend = registry.create_streaming(
        profile,
        ASRSessionContext(source_epoch=1, offset_ms=0, purpose="subtitles"),
    )

    assert backend.backend_id == "sensevoice-native"
    assert backend.uri == "offline://sensevoice-native"


def test_speechrail_registry_freezes_realtime_profile_controls() -> None:
    profile = SpeechRailRealtimeProfile(
        url="ws://127.0.0.1:8201/v2/realtime",
        language="Chinese",
        final_timeout_secs=12.0,
    )
    registry = build_speechrail_realtime_registry()

    backend = registry.create_streaming(
        profile,
        ASRSessionContext(source_epoch=1, offset_ms=0, purpose="meeting"),
    )

    assert isinstance(backend, SpeechRailStreamingTranscriber)
    assert backend.backend_id == "speechrail-realtime-v2"
    assert backend.uri == "ws://127.0.0.1:8201/v2/realtime"


async def test_qwen3_registry_does_not_close_run_scoped_worker_per_sample() -> None:
    class FakeWorker:
        identity = Qwen3WorkerIdentity(device="mps", dtype="float16")

        def __init__(self) -> None:
            self.close_calls = 0

        async def start(self) -> Qwen3WorkerIdentity:
            return self.identity

        async def transcribe(self, *args: object, **kwargs: object) -> Any:
            del args, kwargs
            raise AssertionError("not used")

        async def close(self) -> None:
            self.close_calls += 1

    worker = FakeWorker()
    profile = Qwen3NativeProfile(
        model_dir="/model-cache/Qwen--Qwen3-ASR-1.7B/snapshots/master",
        python_executable="/opt/whisperlivekit/.venv/bin/python",
        language="Chinese",
        device="mps",
    )
    registry = build_qwen3_native_registry(worker)  # type: ignore[arg-type]
    backend = registry.create_streaming(
        profile,
        ASRSessionContext(source_epoch=1, offset_ms=0, purpose="subtitles"),
    )

    await backend.connect()
    await backend.close()

    assert backend.backend_id == "qwen3-asr-native"
    assert worker.close_calls == 0
