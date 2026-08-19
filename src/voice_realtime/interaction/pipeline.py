"""Pipecat 交互管道装配：FunASR STT → LM Studio → TTS 桥 → 播放。

处理器链对齐 pipecat 1.7 官方组装（examples/getting-started/06a）：
  transport.input → EchoSuppressionProcessor → FunASRSTTService → SelfEchoFilter
  → LLMUserAggregator → LmStudioNativeLLMService → BotTextRecorder → OpenAITTSService
  → transport.output → LLMAssistantAggregator（管道末尾，仅回写 assistant 消息到上下文）

1.7 中 TTS 直接消费 LLM 的 LLMTextFrame 流；VAD 分析通过
LLMUserAggregatorParams(vad_analyzer=...) 集成在聚合器内（不再用独立节点）。

回声死循环两道防线（单机同麦同箱）：
- L1 EchoSuppressionProcessor：TTS 播报全程丢弃输入音频帧，仅当输入 RMS 超过
  回声基线 × 增益（真人插话能量明显更高）才提前放行——音频域的默认防线。
- L2 SelfEchoFilter + BotTextRecorder：用户转写与机器人近端播报文本高度相似时
  判定自回声并吞帧，机器人永不把自己的话当新用户输入——内容域的兜底防线。
"""

from __future__ import annotations

import asyncio
import audioop  # type: ignore[import-not-found]
import difflib
import logging
import time
from collections import deque
from pathlib import Path
from statistics import median
from typing import Any

from huggingface_hub import snapshot_download
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
    TextFrame,
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


_ECHO_BASELINE_WARMUP_FRAMES = 4  # 抑制开启后用于建立回声基线的帧数（~40ms @16k/512B）
_ECHO_MIN_MATCH_CHARS = 4  # 自回声文本判定的最短用户文本长度（防短词误杀）


def _rms16(audio: bytes) -> float:
    """16bit PCM 波形 RMS（stdint 域）。"""
    return float(audioop.rms(audio, 2)) if audio else 0.0


class EchoTextBuffer:
    """机器人最近播报文本的环形缓冲（带时间戳），供自回声文本判定。"""

    def __init__(self, window_secs: float = 10.0, max_items: int = 8) -> None:
        self._window_secs = window_secs
        self._max_items = max_items
        self._items: deque[tuple[float, str]] = deque()

    def add(self, text: str, now: float) -> None:
        text = text.strip()
        if not text:
            return
        self._items.append((now, text))
        if len(self._items) > self._max_items:
            self._items.popleft()

    def _recent(self, now: float) -> list[str]:
        cutoff = now - self._window_secs
        return [text for ts, text in self._items if ts >= cutoff]

    def matches(self, text: str, min_ratio: float, min_chars: int, now: float) -> bool:
        """用户文本是否为自回声：与近期机器人文本相似或基本是其子串。"""
        text = text.strip()
        if len(text) < min_chars:
            return False
        for bot_text in self._recent(now):
            matcher = difflib.SequenceMatcher(None, text, bot_text)
            ratio = matcher.ratio()
            longest = matcher.find_longest_match(0, len(text), 0, len(bot_text))
            containment = longest.size / len(text) if longest.size else 0.0
            if ratio >= min_ratio or containment >= min_ratio:
                return True
        return False


class BotTextRecorder(FrameProcessor):
    """记录 LLM 输出的机器人文本到共享缓冲（透传帧流，不改管道语义）。"""

    def __init__(self, buffer: EchoTextBuffer) -> None:
        super().__init__(name="bot-text-recorder")
        self._buffer = buffer

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TextFrame) and frame.text:
            self._buffer.add(frame.text, time.monotonic())
        await self.push_frame(frame, direction)


class SelfEchoFilter(FrameProcessor):
    """语义兜底防线：拦截与机器人近端播报文本高度相似的用户转写。

    挂在 STT 与 LLMUserAggregator 之间：命中自回声的文本帧直接吞掉
    （不进 LLM 上下文），保证机器人永远不会把自己的话当作新用户输入，
    内容层死循环必然中断。L1 音频域漏过的回声由本层兜底。
    """

    def __init__(
        self,
        buffer: EchoTextBuffer,
        min_ratio: float = 0.7,
        min_chars: int = _ECHO_MIN_MATCH_CHARS,
    ) -> None:
        super().__init__(name="self-echo-filter")
        self._buffer = buffer
        self._min_ratio = min_ratio
        self._min_chars = min_chars
        self._dropped = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if (
            isinstance(frame, TextFrame)
            and self._buffer.matches(
                frame.text, self._min_ratio, self._min_chars, time.monotonic()
            )
        ):
            self._dropped += 1
            logger.info("self-echo: 丢弃与机器人近端播报相似的用户转写 %r", frame.text)
            return
        await self.push_frame(frame, direction)


class EchoSuppressionProcessor(FrameProcessor):
    """L1 音频域防线：TTS 播报全程丢弃输入音频，能量门控真人插话。

    单机同麦同箱场景下麦克风会重拾扬声器声音；旧实现只在播报起始窗口
    （500ms）内抑制，长播报的尾部回声仍会被 VAD 误判为"用户说话"并
    自打断。本实现把抑制窗口覆盖整个播报期（BotStarted → BotStopped），
    并保留 barge-in：输入 RMS 持续超过回声基线 × gain（真人插话能量
    明显高于回声稳态电平）连续 barge_in_frames 帧 → 放行，交由 VAD
    驱动正常的用户插话打断。
    """

    def __init__(self, barge_in_gain: float = 2.5, barge_in_frames: int = 3) -> None:
        super().__init__(name="echo-suppress")
        self._barge_in_gain = barge_in_gain
        self._barge_in_frames = barge_in_frames
        self._suppressing = False
        self._echo_rms: deque[float] = deque()
        self._hot_streak = 0
        self._dropped = 0
        self._warned_format = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self._suppressing = True
            self._echo_rms.clear()
            self._hot_streak = 0
            logger.info(
                "echo-suppress: bot started，全播报期抑制（插话门槛 基线×%.1f × %d 帧）",
                self._barge_in_gain,
                self._barge_in_frames,
            )
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._suppressing = False
            self._echo_rms.clear()
            self._hot_streak = 0
            logger.info("echo-suppress: bot stopped")
        elif isinstance(frame, InputAudioRawFrame) and self._suppressing:
            try:
                rms = _rms16(frame.audio)
            except (TypeError, ValueError, OverflowError):
                # 非预期音频格式：fail-open 放行，避免抑制器崩溃拖垮管道
                if not self._warned_format:
                    self._warned_format = True
                    logger.warning("echo-suppress: 无法计算 RMS（非 16bit PCM？），放行")
                self._suppressing = False
            else:
                if self._barge_in(rms):
                    self._suppressing = False
                    logger.info("echo-suppress: 检测到真人插话（能量门控），恢复输入")
                    # 放行本帧（不 return，落到下方 push_frame），VAD 即刻可见插话起始
                else:
                    self._dropped += 1
                    if self._dropped % 100 == 0:
                        logger.debug("echo-suppress: 已丢弃 %d 帧回声音频", self._dropped)
                    return
        await self.push_frame(frame, direction)

    def _barge_in(self, rms: float) -> bool:
        """能量门控：输入 RMS 持续超过回声基线 × 增益则判定真人插话。"""
        self._echo_rms.append(rms)
        if len(self._echo_rms) <= _ECHO_BASELINE_WARMUP_FRAMES:
            return False  # 基线未建立，保守丢弃
        if rms > median(self._echo_rms) * self._barge_in_gain:
            self._hot_streak += 1
            return self._hot_streak >= self._barge_in_frames
        self._hot_streak = 0
        return False


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
    echo_buffer = EchoTextBuffer(window_secs=settings.echo_text_window_secs)
    echo_suppressor = EchoSuppressionProcessor(
        barge_in_gain=settings.echo_barge_in_gain,
        barge_in_frames=settings.echo_barge_in_frames,
    )
    self_echo_filter = SelfEchoFilter(
        echo_buffer, min_ratio=settings.echo_text_similarity
    )
    bot_text_recorder = BotTextRecorder(echo_buffer)

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
            self_echo_filter,
            pair.user(),
            llm,
            bot_text_recorder,
            tts,
            transport.output(),
            pair.assistant(),
        ]
    )
