"""当前生产 ASR 后端的默认注册组合。"""

from __future__ import annotations

from voice_realtime.asr.adapters.funasr_nano_pytorch import (
    FunASRNanoPyTorchAdapter,
    FunASRNanoPyTorchInference,
    FunASRNanoPyTorchRawEventSink,
)
from voice_realtime.asr.adapters.funasr_nano_ws import (
    FunASRNanoWSAdapter,
    FunASRNanoWSConnectFactory,
    FunASRNanoWSRawEventSink,
)
from voice_realtime.asr.adapters.qwen3_native import (
    Qwen3NativeOfflineAdapter,
    Qwen3NativeRawEventSink,
    Qwen3NativeWorker,
)
from voice_realtime.asr.adapters.sensevoice_native import (
    SenseVoiceNativeAdapter,
    SenseVoiceNativeInference,
    SenseVoiceNativeRawEventSink,
)
from voice_realtime.asr.adapters.speechrail_realtime import (
    ConnectionFactory,
    SpeechRailRealtimeClient,
    SpeechRailStreamingTranscriber,
)
from voice_realtime.asr.adapters.wlk import (
    WLKRawEventSink,
    WLKStreamFactory,
    WLKStreamingAdapter,
)
from voice_realtime.asr.contracts import ASRSessionContext, StreamingTranscriber
from voice_realtime.asr.profiles import (
    ASRProfile,
    FunASRNanoPyTorchProfile,
    FunASRNanoWSProfile,
    Qwen3NativeProfile,
    SenseVoiceNativeProfile,
    SpeechRailRealtimeProfile,
)
from voice_realtime.asr.registry import ASRBackendRegistry


def build_wlk_registry(
    service_url: str,
    *,
    stream_factory: WLKStreamFactory | None = None,
    raw_event_sink: WLKRawEventSink | None = None,
) -> ASRBackendRegistry:
    """注册当前 WLK 三种兼容 profile，不改变其服务端选择语义。"""
    registry = ASRBackendRegistry()

    def create(profile: ASRProfile, context: ASRSessionContext) -> StreamingTranscriber:
        if isinstance(
            profile,
            (
                FunASRNanoWSProfile,
                FunASRNanoPyTorchProfile,
                Qwen3NativeProfile,
                SenseVoiceNativeProfile,
                SpeechRailRealtimeProfile,
            ),
        ):
            raise TypeError("non-WLK profile cannot be constructed by the WLK registry")
        max_speakers = getattr(profile, "diarization_max_speakers", None)
        if stream_factory is None:
            return WLKStreamingAdapter(
                url=service_url,
                language=profile.language,
                context=context,
                backend_id=profile.kind,
                supports_speaker_labels=profile.speaker_labels,
                max_speakers=max_speakers,
                raw_event_sink=raw_event_sink,
            )
        return WLKStreamingAdapter(
            url=service_url,
            language=profile.language,
            context=context,
            backend_id=profile.kind,
            supports_speaker_labels=profile.speaker_labels,
            max_speakers=max_speakers,
            stream_factory=stream_factory,
            raw_event_sink=raw_event_sink,
        )

    for backend_id in ("wlk-qwen3-streaming", "wlk-sensevoice", "wlk-auto"):
        registry.register_streaming(backend_id, create)
    return registry


def build_funasr_nano_ws_registry(
    service_url: str,
    *,
    connect_factory: FunASRNanoWSConnectFactory | None = None,
    raw_event_sink: FunASRNanoWSRawEventSink | None = None,
) -> ASRBackendRegistry:
    """注册 Fun-ASR Nano 官方实时 WebSocket 候选。"""
    registry = ASRBackendRegistry()

    def create(profile: ASRProfile, context: ASRSessionContext) -> StreamingTranscriber:
        if not isinstance(profile, FunASRNanoWSProfile):
            raise TypeError("WLK profile cannot be constructed by the Fun-ASR registry")
        return FunASRNanoWSAdapter(
            url=service_url,
            language=profile.language,
            context=context,
            hotwords=profile.hotwords,
            connect_factory=connect_factory,
            raw_event_sink=raw_event_sink,
            handshake_timeout_secs=profile.connect_timeout_secs,
            finish_timeout_secs=profile.final_timeout_secs,
        )

    registry.register_streaming("funasr-nano-ws", create)
    return registry


def build_speechrail_realtime_registry(
    *,
    connection_factory: ConnectionFactory | None = None,
) -> ASRBackendRegistry:
    """注册显式 opt-in 的 SpeechRail Realtime v2 ASR profile。"""
    registry = ASRBackendRegistry()

    def create(profile: ASRProfile, context: ASRSessionContext) -> StreamingTranscriber:
        if not isinstance(profile, SpeechRailRealtimeProfile):
            raise TypeError("non-SpeechRail profile cannot use the SpeechRail registry")
        return SpeechRailStreamingTranscriber(
            client=SpeechRailRealtimeClient(
                url=profile.url,
                connection_factory=connection_factory,
            ),
            language=profile.language,
            context=context,
            finish_timeout_secs=profile.final_timeout_secs,
        )

    registry.register_streaming("speechrail-realtime-v2", create)
    return registry


def build_funasr_nano_pytorch_registry(
    engine: FunASRNanoPyTorchInference,
    *,
    raw_event_sink: FunASRNanoPyTorchRawEventSink | None = None,
) -> ASRBackendRegistry:
    """注册复用同一个 engine 的 Fun-ASR Nano 原生离线实验臂。"""
    registry = ASRBackendRegistry()

    def create(profile: ASRProfile, context: ASRSessionContext) -> StreamingTranscriber:
        if not isinstance(profile, FunASRNanoPyTorchProfile):
            raise TypeError("non-PyTorch profile cannot use the Fun-ASR PyTorch registry")
        return FunASRNanoPyTorchAdapter(
            engine=engine,
            language=profile.language,
            context=context,
            hotwords=profile.hotwords,
            itn=profile.itn,
            raw_event_sink=raw_event_sink,
        )

    registry.register_streaming("funasr-nano-pytorch", create)
    return registry


def build_sensevoice_native_registry(
    engine: SenseVoiceNativeInference,
    *,
    raw_event_sink: SenseVoiceNativeRawEventSink | None = None,
) -> ASRBackendRegistry:
    """注册复用同一个 engine 的 SenseVoice 原生 CPU 实验臂。"""
    registry = ASRBackendRegistry()

    def create(profile: ASRProfile, context: ASRSessionContext) -> StreamingTranscriber:
        if not isinstance(profile, SenseVoiceNativeProfile):
            raise TypeError("non-SenseVoice profile cannot use the native registry")
        return SenseVoiceNativeAdapter(
            engine=engine,
            language=profile.language,
            context=context,
            use_itn=profile.use_itn,
            raw_event_sink=raw_event_sink,
        )

    registry.register_streaming("sensevoice-native", create)
    return registry


def build_qwen3_native_registry(
    worker: Qwen3NativeWorker,
    *,
    raw_event_sink: Qwen3NativeRawEventSink | None = None,
) -> ASRBackendRegistry:
    """注册 run 级共享 Qwen3 隔离 worker；样本 adapter 不拥有 worker。"""
    registry = ASRBackendRegistry()

    def create(profile: ASRProfile, context: ASRSessionContext) -> StreamingTranscriber:
        if not isinstance(profile, Qwen3NativeProfile):
            raise TypeError("non-Qwen3 profile cannot use the native registry")
        return Qwen3NativeOfflineAdapter(
            worker=worker,
            language=profile.language,
            context=profile.context,
            session_context=context,
            owns_worker=False,
            raw_event_sink=raw_event_sink,
        )

    registry.register_streaming("qwen3-asr-native", create)
    return registry
