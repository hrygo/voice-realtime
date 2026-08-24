"""当前生产 ASR 后端的默认注册组合。"""

from __future__ import annotations

from voice_realtime.asr.adapters.wlk import (
    WLKRawEventSink,
    WLKStreamFactory,
    WLKStreamingAdapter,
)
from voice_realtime.asr.contracts import ASRSessionContext, StreamingTranscriber
from voice_realtime.asr.profiles import ASRProfile
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
