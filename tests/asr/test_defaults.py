"""默认 ASR registry 组合与 profile 接线测试。"""

from voice_realtime.asr.adapters.funasr_nano_ws import FunASRNanoWSAdapter
from voice_realtime.asr.contracts import ASRSessionContext
from voice_realtime.asr.defaults import build_funasr_nano_ws_registry
from voice_realtime.asr.profiles import FunASRNanoWSProfile


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
