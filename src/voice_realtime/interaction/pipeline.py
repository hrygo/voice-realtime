"""Pipecat 交互管道装配：FunASR STT → LM Studio → TTS 桥 → 播放。

处理器链（顺序即数据流）：
  transport.input → VADProcessor → FunASRSTTService → LLMUserAggregator
  → LmStudioNativeLLMService → LLMAssistantAggregator → OpenAITTSService → transport.output
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from huggingface_hub import snapshot_download
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.services.funasr.stt import FunASRSTTService, FunASRSTTSettings
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

from voice_realtime.config import InteractionSettings
from voice_realtime.interaction.reasoning import (
    DEFAULT_SYSTEM_PROMPT,
    LmStudioNativeLLMService,
)

OUTPUT_SAMPLE_RATE = 24000  # Qwen3-TTS 原生采样率
DEFAULT_SENSEVOICE_REPO = "FunAudioLLM/SenseVoiceSmall"


def _resolve_stt_model(model: str) -> str:
    """把 STT 模型配置解析为 funasr 可用的本地路径。

    funasr 的 hub 参数由 pipecat FunASRSTTService 硬编码为 modelscope
    （本环境 SSRF 拦截），因此任何 repo ID 都先经 snapshot_download 落到本地。
    已是本地路径（目录/文件存在）则原样透传。
    """
    if model and Path(model).exists():
        return model
    return snapshot_download(model or DEFAULT_SENSEVOICE_REPO)


def build_system_prompt(persona: str | None = None) -> str:
    """构造语音助手系统提示词。persona 追加在默认约束之后。"""
    if persona:
        return f"{DEFAULT_SYSTEM_PROMPT}\n{persona}"
    return DEFAULT_SYSTEM_PROMPT


def build_pipeline(
    settings: InteractionSettings,
    *,
    transport: Any | None = None,
    context: LLMContext | None = None,
    persona: str | None = None,
) -> Pipeline:
    """按配置装配交互管道。transport 可注入（测试/无麦克风环境）。"""
    transport = transport or LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=settings.sample_rate,
            audio_out_sample_rate=OUTPUT_SAMPLE_RATE,
            input_device_index=settings.input_device,
        )
    )

    vad_analyzer = SileroVADAnalyzer(
        sample_rate=settings.sample_rate,
        params=VADParams(stop_secs=settings.silence_secs),
    )

    stt = FunASRSTTService(
        device="cpu",
        settings=FunASRSTTSettings(
            model=_resolve_stt_model(settings.stt_model),
            language=Language.ZH,
            use_itn=True,
        ),
    )

    llm = LmStudioNativeLLMService(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        temperature=settings.llm_temperature,
        reasoning="off",
    )

    tts = OpenAITTSService(
        api_key="local",
        base_url=settings.tts_bridge_url,
        voice=settings.tts_voice,
        sample_rate=OUTPUT_SAMPLE_RATE,
    )

    context = context or LLMContext(
        messages=[{"role": "system", "content": build_system_prompt(persona)}]
    )
    pair = LLMContextAggregatorPair(context)

    return Pipeline(
        [
            transport.input(),
            VADProcessor(vad_analyzer=vad_analyzer),
            stt,
            pair.user(),
            llm,
            pair.assistant(),
            tts,
            transport.output(),
        ]
    )
