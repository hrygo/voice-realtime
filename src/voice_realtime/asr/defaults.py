"""当前生产 ASR 后端的默认注册组合。"""

from __future__ import annotations

from voice_realtime.asr.adapters.funasr_nano_ws import (
    FunASRNanoWSAdapter,
    FunASRNanoWSConnectFactory,
    FunASRNanoWSRawEventSink,
)
from voice_realtime.asr.adapters.wlk import (
    WLKRawEventSink,
    WLKStreamFactory,
    WLKStreamingAdapter,
)
from voice_realtime.asr.contracts import ASRSessionContext, StreamingTranscriber
from voice_realtime.asr.profiles import ASRProfile, FunASRNanoWSProfile
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
        if isinstance(profile, FunASRNanoWSProfile):
            raise TypeError("Fun-ASR profile cannot be constructed by the WLK registry")
        if stream_factory is None:
            return WLKStreamingAdapter(
                url=service_url,
                language=profile.language,
                context=context,
                backend_id=profile.kind,
                supports_speaker_labels=profile.speaker_labels,
                raw_event_sink=raw_event_sink,
            )
        return WLKStreamingAdapter(
            url=service_url,
            language=profile.language,
            context=context,
            backend_id=profile.kind,
            supports_speaker_labels=profile.speaker_labels,
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
