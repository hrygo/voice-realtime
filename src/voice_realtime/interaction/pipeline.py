"""Pipecat 交互管道装配：FunASR STT → LM Studio → TTS 桥 → 播放。

处理器链对齐 pipecat 1.7 官方组装（examples/getting-started/06a）：
  transport.input → EchoSuppressionProcessor → FunASRSTTService → LLMUserAggregator
  → LmStudioNativeLLMService → OpenAITTSService → transport.output
  → LLMAssistantAggregator（管道末尾，仅回写 assistant 消息到上下文）

1.7 中 TTS 直接消费 LLM 的 LLMTextFrame 流；VAD 分析通过
LLMUserAggregatorParams(vad_analyzer=...) 集成在聚合器内（不再用独立节点）。
EchoSuppressionProcessor 在 TTS 播报起始窗口内丢弃输入音频，
抑制扬声器回声被麦克风重拾造成的自我打断。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from huggingface_hub import snapshot_download
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.funasr.stt import FunASRSTTService, FunASRSTTSettings
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

from voice_realtime.audio.audio_injector import AudioInjector
from voice_realtime.config import TTS_OUTPUT_SAMPLE_RATE, InteractionSettings
from voice_realtime.interaction.reasoning import (
    DEFAULT_SYSTEM_PROMPT,
    LmStudioNativeLLMService,
)

DEFAULT_SENSEVOICE_REPO = "FunAudioLLM/SenseVoiceSmall"
_PIPECAT_LANGUAGES = {
    "zh": Language.ZH,
    "en": Language.EN,
    "yue": Language.YUE,
    "ja": Language.JA,
    "ko": Language.KO,
}
logger = logging.getLogger(__name__)


def _to_pipecat_language(lang_code: str) -> Language:
    normalized = lang_code.strip().lower()
    language = _PIPECAT_LANGUAGES.get(normalized)
    if language is not None:
        return language
    logger.warning("未知 STT 语言代码 %r，回退到中文", lang_code)
    return Language.ZH


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


class EchoSuppressionProcessor(FrameProcessor):
    """TTS 播报起始窗口内丢弃输入音频，抑制扬声器回声自打断。

    单机同麦同箱场景下麦克风会重拾扬声器声音；VAD 会在机器人开口后
    立即误判为"用户说话"并广播打断。本处理器在 BotStartedSpeakingFrame
    后的窗口内丢弃 InputAudioRawFrame，窗口结束后恢复（保留 barge-in）。
    """

    def __init__(self, window_ms: int = 500) -> None:
        super().__init__(name="echo-suppress")
        self._window_seconds = window_ms / 1000
        self._window_end: float | None = None
        self._dropped = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        now = time.monotonic()
        if isinstance(frame, BotStartedSpeakingFrame):
            self._window_end = now + self._window_seconds
            logger.info("echo-suppress: bot started, 抑制窗口 %.0fms", self._window_seconds * 1000)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._window_end = None
            logger.info("echo-suppress: bot stopped")
        elif isinstance(frame, InputAudioRawFrame) and self._window_end is not None:
            if now < self._window_end:
                self._dropped += 1
                if self._dropped % 50 == 0:
                    logger.debug("echo-suppress: 已丢弃 %d 帧回声音频", self._dropped)
                return
            self._window_end = None
        await self.push_frame(frame, direction)


def build_pipeline(
    settings: InteractionSettings,
    *,
    transport: Any = None,
    context: LLMContext | None = None,
    persona: str | None = None,
    audio_queue: asyncio.Queue[bytes] | None = None,
) -> Pipeline:
    """按配置装配交互管道。transport 可注入（测试/无麦克风环境）。

    audio_queue 非空时启用 AudioInjector 模式：由 AudioHub 单源采集扇出，
    transport 关闭麦克风输入（audio_in_enabled=False），管道首节点换成
    AudioInjector（推 InputAudioRawFrame，帧类型与本地采集一致，
    下游 VAD/回声抑制/STT 无感知）。
    """
    use_injector = audio_queue is not None
    transport = transport or LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=not use_injector,
            audio_out_enabled=True,
            audio_in_sample_rate=settings.sample_rate,
            audio_out_sample_rate=TTS_OUTPUT_SAMPLE_RATE,
            input_device_index=settings.input_device,
        )
    )

    stt = FunASRSTTService(
        device="cpu",
        settings=FunASRSTTSettings(
            model=_resolve_stt_model(settings.stt_model),
            language=_to_pipecat_language(settings.stt_language),
            use_itn=True,
        ),
        ttfs_p99_latency=0.5,
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
        voice="alloy",
        sample_rate=TTS_OUTPUT_SAMPLE_RATE,
    )

    context = context or LLMContext(
        messages=[{"role": "system", "content": build_system_prompt(persona)}]
    )
    # 1.7 组装：VAD 分析集成进 LLMUserAggregatorParams（官方 getting-started/06a 模式），
    # LLM 之后直连 TTS（TTS 直接消费 LLMTextFrame 流），assistant 聚合器在管道末尾回写上下文。
    pair = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(
                sample_rate=settings.sample_rate,
                params=VADParams(stop_secs=settings.silence_secs),
            )
        ),
    )
    echo_suppressor = EchoSuppressionProcessor(settings.interrupt_echo_suppression_ms)

    input_source = (
        AudioInjector(audio_queue, sample_rate=settings.sample_rate)
        if audio_queue is not None
        else transport.input()
    )

    return Pipeline(
        [
            input_source,
            echo_suppressor,
            stt,
            pair.user(),
            llm,
            tts,
            transport.output(),
            pair.assistant(),
        ]
    )
