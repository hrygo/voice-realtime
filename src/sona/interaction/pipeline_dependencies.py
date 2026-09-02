"""交互管道外部构造的 typed factory bundle。

default factories 只读取 SpeechRail Realtime / LM Studio 配置，不消费任何
历史 TTS bridge 兼容配置。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

from sona.asr.contracts import ConversationSTTFactory
from sona.audio.devices import resolve_input_device_index
from sona.config import TTS_OUTPUT_SAMPLE_RATE, InteractionSettings
from sona.interaction.reasoning import LmStudioNativeLLMService
from sona.interaction.tts import SpeechRailTTSService
from sona.speechrail import SpeechRailConversationSTTFactory


class TransportFactory(Protocol):
    def __call__(
        self, *, settings: InteractionSettings, audio_in_enabled: bool
    ) -> LocalAudioTransport: ...


class LLMFactory(Protocol):
    def __call__(self, *, settings: InteractionSettings) -> FrameProcessor: ...


class TTSFactory(Protocol):
    def __call__(self, *, settings: InteractionSettings) -> FrameProcessor: ...


class VADFactory(Protocol):
    def __call__(self, *, settings: InteractionSettings) -> SileroVADAnalyzer: ...


class SmartTurnFactory(Protocol):
    def __call__(self, *, settings: InteractionSettings) -> LocalSmartTurnAnalyzerV3: ...


@dataclass(frozen=True, slots=True)
class PipelineFactories:
    transport_factory: TransportFactory
    stt_factory: ConversationSTTFactory
    llm_factory: LLMFactory
    tts_factory: TTSFactory
    vad_factory: VADFactory
    smart_turn_factory: SmartTurnFactory


def default_pipeline_factories(settings: InteractionSettings) -> PipelineFactories:
    """生产默认构造：LocalAudioTransport、SpeechRail STT/LLM/TTS 与现有 analyzer。"""

    def transport(
        *, settings: InteractionSettings, audio_in_enabled: bool
    ) -> LocalAudioTransport:
        input_device = settings.input_device
        if audio_in_enabled and settings.input_device_name is not None:
            input_device = resolve_input_device_index(
                device_index=settings.input_device,
                device_name=settings.input_device_name,
            )
        return LocalAudioTransport(
            LocalAudioTransportParams(
                audio_in_enabled=audio_in_enabled,
                audio_out_enabled=True,
                audio_in_sample_rate=settings.sample_rate,
                audio_out_sample_rate=TTS_OUTPUT_SAMPLE_RATE,
                input_device_index=input_device,
            )
        )

    def llm(*, settings: InteractionSettings) -> FrameProcessor:
        return LmStudioNativeLLMService(
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            temperature=settings.llm_temperature,
            reasoning="off",
            compaction_config=settings.context_compaction_config(),
        )

    def tts(*, settings: InteractionSettings) -> FrameProcessor:
        return SpeechRailTTSService(
            url=settings.speechrail_realtime_url,
            api_key=settings.speechrail_api_key,
            fast_first_clause=settings.tts_fast_first_clause,
            first_clause_min_chars=settings.tts_first_clause_min_chars,
            settings=SpeechRailTTSService.Settings(
                model=settings.speechrail_tts_model,
                voice=settings.tts_voice,
                language=settings.tts_language,
            ),
        )

    def vad(*, settings: InteractionSettings) -> SileroVADAnalyzer:
        return SileroVADAnalyzer(
            sample_rate=settings.sample_rate,
            params=VADParams(
                confidence=settings.vad_confidence,
                start_secs=settings.vad_start_secs,
                stop_secs=settings.silence_secs,
                min_volume=settings.vad_min_volume,
            ),
        )

    def smart_turn(*, settings: InteractionSettings) -> LocalSmartTurnAnalyzerV3:
        return LocalSmartTurnAnalyzerV3(
            params=SmartTurnParams(stop_secs=settings.smart_turn_stop_secs)
        )

    return PipelineFactories(
        transport_factory=transport,
        stt_factory=SpeechRailConversationSTTFactory(
            url=settings.speechrail_realtime_url,
            api_key=settings.speechrail_api_key,
        ),
        llm_factory=llm,
        tts_factory=tts,
        vad_factory=vad,
        smart_turn_factory=smart_turn,
    )


__all__ = [
    "LLMFactory",
    "PipelineFactories",
    "SmartTurnFactory",
    "TTSFactory",
    "TransportFactory",
    "VADFactory",
    "default_pipeline_factories",
]
