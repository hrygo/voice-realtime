"""Pipecat 交互管道装配：FunASR STT → LM Studio → TTS 桥 → 播放。

处理器链对齐 pipecat 1.7 官方组装（examples/getting-started/06a）：
  transport.input → EchoSuppressionProcessor → FunASRSTTService → SelfEchoFilter
  → LLMUserAggregator → LmStudioNativeLLMService → BotTextRecorder → OpenAITTSService
  → TTSStateObserver → transport.output → LLMAssistantAggregator

1.7 中 TTS 直接消费 LLM 的 LLMTextFrame 流；VAD 分析通过
LLMUserAggregatorParams(vad_analyzer=...) 集成在聚合器内（不再用独立节点）。

回声死循环两道防线（单机同麦同箱）：
- L1 EchoSuppressionProcessor + TTSStateObserver：TTS 播报全程及尾部挂起期丢弃输入音频帧，
  仅当输入 RMS 超过回声基线 × 增益（真人插话能量明显更高）才提前放行——音频域的默认防线。
- L2 SelfEchoFilter + BotTextRecorder：用户转写与机器人近端播报文本高度相似时
  判定自回声并吞帧，机器人永不把自己的话当新用户输入——内容域的兜底防线。
"""

from __future__ import annotations

import asyncio
import audioop
import difflib
import logging
import re
import time
from collections import deque
from typing import Any

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    TextFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)

try:
    from pypinyin import Style, lazy_pinyin

    _HAS_PYPINYIN = True
except ImportError:  # pragma: no cover
    _HAS_PYPINYIN = False
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.funasr.stt import FunASRSTTService, FunASRSTTSettings
from pipecat.transcriptions.language import Language
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.turns.user_mute.base_user_mute_strategy import BaseUserMuteStrategy

from voice_realtime.audio.audio_injector import AudioInjector
from voice_realtime.config import (
    TTS_ENGINE_DEFAULT_VOICE,
    TTS_OUTPUT_SAMPLE_RATE,
    InteractionSettings,
)
from voice_realtime.interaction.context_memory import MEMORY_PROTOCOL
from voice_realtime.interaction.reasoning import (
    DEFAULT_SYSTEM_PROMPT,
    LmStudioNativeLLMService,
)
from voice_realtime.interaction.tts import LocalBridgeTTSService
from voice_realtime.model_cache import resolve_model_snapshot

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


def _resolve_stt_model(model: str, *, allow_downloads: bool = False) -> str:
    """把 STT 模型配置解析为 funasr 可用的本地路径。

    funasr 的 hub 参数由 pipecat FunASRSTTService 硬编码为 modelscope
    （本环境 SSRF 拦截），因此任何 repo ID 都先经 snapshot_download 落到本地。
    已是本地路径（目录/文件存在）则原样透传。
    """
    return resolve_model_snapshot(
        model,
        default_repo=DEFAULT_SENSEVOICE_REPO,
        allow_downloads=allow_downloads,
    )


def build_system_prompt(persona: str | None = None) -> str:
    """构造语音助手系统提示词。persona 追加在默认约束之后。"""
    base_prompt = f"{DEFAULT_SYSTEM_PROMPT.rstrip()}\n\n{MEMORY_PROTOCOL.rstrip()}"
    if persona:
        return f"{base_prompt}\n\n{persona}"
    return base_prompt


_ECHO_BASELINE_WARMUP_FRAMES = 8  # 抑制开启后用于建立扬声器峰值包络的初始帧数（~128ms @16k/512B）
_ECHO_MIN_MATCH_CHARS = 2  # 自回声文本判定的最短用户文本长度（防短词误杀，去标点后）
_COMMON_ACKNOWLEDGEMENTS = frozenset({"好", "好的", "嗯", "嗯嗯", "行", "可以", "谢谢", "知道了"})
_PUNCTUATION_RE = re.compile(
    r"[\s\.,\?!;:，。？！；：、“”‘’\"\'\(\)\[\]（）—\-_…]+", re.UNICODE
)


def _normalize_text(text: str) -> str:
    """去标点、空格、转小写，用于自回声模糊与子串比对。"""
    return _PUNCTUATION_RE.sub("", text).strip().lower()


def _pinyin_tokens(text: str) -> list[str]:
    """提取纯中文拼音序列（用于抗 ASR 谐音错字的回声模糊判定）。"""
    if not _HAS_PYPINYIN:
        return []
    return [p for p in lazy_pinyin(text, style=Style.NORMAL) if p.isalnum()]


def _rms16(audio: bytes) -> float:
    """16bit PCM 波形 RMS（stdint 域）。"""
    return float(audioop.rms(audio, 2)) if audio else 0.0


class EchoState:
    """共享回声与播报状态：跨处理器同步 TTS 播报与麦克风物理闭麦窗口。"""

    def __init__(self) -> None:
        self.bot_speaking = False
        self.last_speaking_stop_time = 0.0
        self._tts_active = False
        self._speaker_active = False
        self.generation = 0

    def on_tts_started(self) -> None:
        was_active = self._tts_active or self._speaker_active
        self._tts_active = True
        self.bot_speaking = True
        if not was_active:
            self.generation += 1
        logger.info("echo-state: 机器人开始生成/播报，激活物理闭麦与回声抑制")

    def on_bot_speaking_started(self) -> None:
        was_active = self._tts_active or self._speaker_active
        self._speaker_active = True
        self.bot_speaking = True
        if not was_active:
            self.generation += 1
        logger.info("echo-state: 扬声器开始发声，保持物理闭麦")

    def on_tts_stopped(self) -> None:
        self._tts_active = False
        if not self._speaker_active:
            self.bot_speaking = False
            self.last_speaking_stop_time = time.monotonic()
            logger.info("echo-state: TTS 生成结束，进入尾部混响挂起抑制")

    def on_bot_speaking_stopped(self) -> None:
        self._speaker_active = False
        if not self._tts_active:
            self.bot_speaking = False
            self.last_speaking_stop_time = time.monotonic()
            logger.info("echo-state: 扬声器发声完毕，进入尾部混响挂起抑制")

    def reset(self) -> None:
        """重置状态（会话停止或强行打断时调用）。"""
        self.generation += 1
        self.bot_speaking = False
        self.last_speaking_stop_time = 0.0
        self._tts_active = False
        self._speaker_active = False

    def is_suppressing(self, now: float, tail_hangover_secs: float) -> bool:
        if self.bot_speaking or self._tts_active or self._speaker_active:
            return True
        return (now - self.last_speaking_stop_time) < tail_hangover_secs


class TTSStateObserver(FrameProcessor):
    """监控 TTS 输出流与扬声器发声状态驱动 EchoState。"""

    def __init__(self, echo_state: EchoState) -> None:
        super().__init__(name="tts-state-observer")
        self._echo_state = echo_state

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSStartedFrame):
            self._echo_state.on_tts_started()
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._echo_state.on_bot_speaking_started()
        elif isinstance(frame, TTSStoppedFrame):
            self._echo_state.on_tts_stopped()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._echo_state.on_bot_speaking_stopped()

        await self.push_frame(frame, direction)


class EchoTextBuffer:
    """机器人最近播报文本的环形缓冲（带时间戳），供自回声文本判定。"""

    def __init__(self, window_secs: float = 10.0, max_items: int = 16) -> None:
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
        """用户文本是否为自回声：与近期机器人文本相似、为其子串或拼音/声韵序列高度重合。"""
        norm_input = _normalize_text(text)
        if (
            not norm_input
            or len(norm_input) < min_chars
            or norm_input in _COMMON_ACKNOWLEDGEMENTS
        ):
            return False

        recent_texts = self._recent(now)
        if not recent_texts:
            return False

        combined_bot = "".join(_normalize_text(t) for t in recent_texts)
        if not combined_bot:
            return False

        # 1. 单句字面比对：完全子串包含、整句相似度、滑动窗口模糊匹配
        input_len = len(norm_input)
        for bot_text in recent_texts:
            norm_bot = _normalize_text(bot_text)
            if not norm_bot:
                continue
            # 精确子串包含（STT 转写是机器人的子串，或机器人转写是 STT 的子串）
            if norm_input in norm_bot or norm_bot in norm_input:
                return True
            # 整句字面相似度
            matcher = difflib.SequenceMatcher(None, norm_input, norm_bot)
            if matcher.ratio() >= min_ratio:
                return True
            longest = matcher.find_longest_match(0, input_len, 0, len(norm_bot))
            if longest.size and (longest.size / input_len >= min_ratio):
                return True
            # 短语滑动窗口模糊匹配（容忍 STT 1~2 个同音字误差，如 3~5 字短尾音）
            if 2 <= input_len <= 8 and len(norm_bot) >= input_len:
                for i in range(len(norm_bot) - input_len + 1):
                    sub = norm_bot[i : i + input_len]
                    sub_ratio = difflib.SequenceMatcher(None, norm_input, sub).ratio()
                    if sub_ratio >= max(0.65, min_ratio - 0.1):
                        return True

        # 2. 联合文本字面比对（跨分句/跨分段拼接转写）
        if norm_input in combined_bot:
            return True
        matcher_all = difflib.SequenceMatcher(None, norm_input, combined_bot)
        if matcher_all.ratio() >= min_ratio:
            return True
        longest_all = matcher_all.find_longest_match(0, input_len, 0, len(combined_bot))
        if longest_all.size and (longest_all.size / input_len >= min_ratio):
            return True

        # 3. 拼音级（Phonetic）模糊比对：抵御 ASR 谐音错字（如"白昼时长" -> "不市场"）
        cand_pinyin = _pinyin_tokens(norm_input)
        bot_pinyin = _pinyin_tokens(combined_bot)
        if cand_pinyin and bot_pinyin:
            cand_str = " ".join(cand_pinyin)
            bot_str = " ".join(bot_pinyin)
            if cand_str in bot_str:
                return True
            matcher_py = difflib.SequenceMatcher(None, cand_pinyin, bot_pinyin)
            longest_py = matcher_py.find_longest_match(0, len(cand_pinyin), 0, len(bot_pinyin))
            if longest_py.size >= 2 and (longest_py.size / len(cand_pinyin) >= 0.6):
                return True
            cand_py_compact = "".join(cand_pinyin)
            bot_py_compact = "".join(bot_pinyin)
            if (
                difflib.SequenceMatcher(None, cand_py_compact, bot_py_compact).ratio()
                >= max(0.6, min_ratio - 0.1)
            ):
                return True

        return False


class HangoverUserMuteStrategy(BaseUserMuteStrategy):
    """带尾部挂起窗口的用户静音策略，防止声学混响与未决转写触发 LLM。"""

    def __init__(
        self,
        tail_hangover_secs: float = 0.4,
        echo_state: EchoState | None = None,
    ) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self._tail_hangover_secs = tail_hangover_secs
        self._echo_state = echo_state or EchoState()

    @property
    def _hangover_until(self) -> float:
        return self._echo_state.last_speaking_stop_time + self._tail_hangover_secs

    async def process_frame(self, frame: Frame) -> bool:
        await super().process_frame(frame)
        now = time.monotonic()
        return self._echo_state.is_suppressing(now, self._tail_hangover_secs)


class BotTextRecorder(FrameProcessor):
    """记录 LLM/TTS 输出的机器人文本到共享缓冲，不修改声学播放状态。"""

    def __init__(
        self,
        buffer: EchoTextBuffer,
    ) -> None:
        super().__init__(name="bot-text-recorder")
        self._buffer = buffer
        self._current_tokens: list[str] = []

    def _flush_current(self) -> None:
        if self._current_tokens:
            full_text = "".join(self._current_tokens).strip()
            if full_text:
                self._buffer.add(full_text, time.monotonic())
            self._current_tokens.clear()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, (TTSStoppedFrame, BotStoppedSpeakingFrame)):
            self._flush_current()
        elif isinstance(frame, (TextFrame, TTSTextFrame)) and frame.text:
            self._current_tokens.append(frame.text)
            # 遇到标点/换行即刻同步当前累积语句（支持句子级实时拦截）
            if any(p in frame.text for p in ("。", "！", "？", "；", "\n", ".", "!", "?", ";")):
                full_text = "".join(self._current_tokens).strip()
                if full_text:
                    self._buffer.add(full_text, time.monotonic())
        elif isinstance(frame, LLMFullResponseEndFrame):
            self._flush_current()

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
        echo_state: EchoState | None = None,
        tail_hangover_secs: float = 0.4,
    ) -> None:
        super().__init__(name="self-echo-filter")
        self._buffer = buffer
        self._min_ratio = min_ratio
        self._min_chars = min_chars
        self._echo_state = echo_state or EchoState()
        self._tail_hangover_secs = tail_hangover_secs
        self._dropped = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TextFrame):
            if not _normalize_text(frame.text):
                self._dropped += 1
                logger.debug("self-echo: 丢弃纯标点/空转写文本帧 %r", frame.text)
                return
            if self._echo_state.is_suppressing(
                time.monotonic(), self._tail_hangover_secs
            ) and self._buffer.matches(
                frame.text, self._min_ratio, self._min_chars, time.monotonic()
            ):
                self._dropped += 1
                logger.info("self-echo: 丢弃与机器人近端播报相似的用户转写 %r", frame.text)
                return
        await self.push_frame(frame, direction)


class EchoSuppressionProcessor(FrameProcessor):
    """L1 音频域防线：机器人播报期物理闭麦（半双工）与回声抑制。

    单机同麦同箱外放场景下：
    - 机器人输出期间（TTS 开始生成 -> 扬声器播报完毕 -> 0.4s 混响挂起）：
      物理丢弃全部麦克风输入音频（InputAudioRawFrame），彻底隔绝扬声器自回声进入 STT/VAD；
      同时拦截全部 InterruptionFrame，防止自腰斩 TTS，确保机器人平稳完整地播报完每一句话。
    - 播报结束并经过声学混响吸收窗口（0.4s）后，自动恢复麦克风采集。
    """

    def __init__(
        self,
        barge_in_gain: float = 2.5,
        barge_in_frames: int = 3,
        tail_hangover_secs: float = 0.4,
        echo_state: EchoState | None = None,
        allow_barge_in: bool = False,
        enable_direct_mode: bool = False,
    ) -> None:
        super().__init__(name="echo-suppress", enable_direct_mode=enable_direct_mode)
        self._barge_in_gain = barge_in_gain
        self._barge_in_frames = barge_in_frames
        self._tail_hangover_secs = tail_hangover_secs
        self._echo_state = echo_state or EchoState()
        self._allow_barge_in = allow_barge_in
        self._barge_in_active = False
        self._echo_rms: deque[float] = deque(maxlen=50)
        self._peak_envelope: float = 0.0
        self._fast_ema: float = 0.0
        self._slow_ema: float = 0.0
        self._hot_streak = 0
        self._quiet_streak = 0
        self._dropped = 0
        self._warned_format = False
        self._seen_echo_generation = self._echo_state.generation

    def set_mode(
        self,
        allow_barge_in: bool,
        barge_in_gain: float = 1.15,
        barge_in_frames: int = 2,
    ) -> None:
        """动态热切换交互模式：外放物理闭麦 (allow_barge_in=False) 或耳机高敏双工 (True)。"""
        self._allow_barge_in = allow_barge_in
        self._barge_in_gain = barge_in_gain
        self._barge_in_frames = barge_in_frames
        self._barge_in_active = False
        self._reset_energy_detector()
        logger.info(
            "echo-suppress: 交互模式已更新 (allow_barge_in=%s, gain=%.2f, frames=%d)",
            allow_barge_in,
            barge_in_gain,
            barge_in_frames,
        )

    @property
    def _bot_speaking(self) -> bool:
        return self._echo_state.bot_speaking

    @property
    def _suppressing(self) -> bool:
        if self._barge_in_active:
            return False
        return self._echo_state.is_suppressing(time.monotonic(), self._tail_hangover_secs)

    @_suppressing.setter
    def _suppressing(self, val: bool) -> None:
        self._barge_in_active = not val

    @property
    def _hangover_until(self) -> float:
        return self._echo_state.last_speaking_stop_time + self._tail_hangover_secs

    async def process_frame(
        self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        now = time.monotonic()
        if self._seen_echo_generation != self._echo_state.generation:
            self._seen_echo_generation = self._echo_state.generation
            self._reset_energy_detector()
        if isinstance(frame, TTSStartedFrame):
            self._barge_in_active = False
            self._reset_energy_detector()
            logger.info(
                "echo-suppress: TTS 启动，物理闭麦激活 (尾部挂起 %.2fs)",
                self._tail_hangover_secs,
            )
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._barge_in_active = False
            self._reset_energy_detector()
        elif isinstance(frame, TTSStoppedFrame):
            pass
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._barge_in_active = False
            self._reset_energy_detector()
            logger.info(
                "echo-suppress: bot 播报完毕，进入尾部混响挂起 (%.2fs)",
                self._tail_hangover_secs,
            )
        elif isinstance(frame, InterruptionFrame):
            # 物理闭麦防自打断：播报期间未触发真人插话时拦截下发
            in_suppress = self._echo_state.is_suppressing(now, self._tail_hangover_secs)
            if in_suppress and not self._barge_in_active:
                logger.debug("echo-suppress: 拦截播报期自回声误触发的 InterruptionFrame")
                return
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)
            return
        elif isinstance(frame, InputAudioRawFrame):
            in_suppression = self._echo_state.is_suppressing(now, self._tail_hangover_secs)
            if in_suppression:
                if not self._allow_barge_in:
                    # 物理闭麦模式：输出期间直接丢弃全部麦克风输入，0 算力 0 回声 0 误打断
                    self._dropped += 1
                    return

                try:
                    rms = _rms16(frame.audio)
                except (TypeError, ValueError, OverflowError):
                    if not self._warned_format:
                        self._warned_format = True
                        logger.warning("echo-suppress: 无法计算 RMS（非 16bit PCM？），放行")
                else:
                    if not self._barge_in_active:
                        if self._barge_in(rms):
                            self._barge_in_active = True
                            self._quiet_streak = 0
                            logger.info("echo-suppress: 检测到真人插话（能量门控），放行音频")
                        else:
                            self._dropped += 1
                            if self._dropped % 100 == 0:
                                logger.debug("echo-suppress: 已丢弃 %d 帧回声音频", self._dropped)
                            return
                    else:
                        if rms <= self._peak_envelope * 0.8:
                            self._quiet_streak += 1
                            if self._quiet_streak >= 20:
                                self._barge_in_active = False
                                self._hot_streak = 0
                                self._quiet_streak = 0
                                logger.debug("echo-suppress: 音量回落平稳，自动重锁回声抑制")
                                self._dropped += 1
                                return
                        else:
                            self._quiet_streak = 0
            else:
                self._barge_in_active = False
                self._hot_streak = 0
                self._quiet_streak = 0

        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

    def _reset_energy_detector(self) -> None:
        self._echo_rms.clear()
        self._peak_envelope = 0.0
        self._fast_ema = 0.0
        self._slow_ema = 0.0
        self._hot_streak = 0
        self._quiet_streak = 0

    def _barge_in(self, rms: float) -> bool:
        """自适应峰值包络门控：快升慢降追踪扬声器声学包络，真人声音明显高出包络才判插话。"""
        self._echo_rms.append(rms)

        if self._fast_ema == 0.0:
            self._fast_ema = rms
            self._slow_ema = rms
        else:
            self._fast_ema = 0.3 * rms + 0.7 * self._fast_ema
            self._slow_ema = 0.05 * rms + 0.95 * self._slow_ema

        # 初始建峰期：耳机模式只需 2 帧极速建峰，外放模式保留 8 帧
        warmup_frames = 2 if self._barge_in_gain <= 1.5 else _ECHO_BASELINE_WARMUP_FRAMES
        if len(self._echo_rms) <= warmup_frames:
            if self._peak_envelope <= 0.0 or rms > self._peak_envelope:
                self._peak_envelope = rms
            else:
                self._peak_envelope = 0.96 * self._peak_envelope + 0.04 * rms
            return False

        # 计算真人插话门槛：耳机模式底限 350.0（近场轻语），外放模式底限 1200.0（防外放漏音）
        min_floor = 350.0 if self._barge_in_gain <= 1.5 else 1200.0
        threshold = max(self._peak_envelope * self._barge_in_gain, min_floor)
        if rms > threshold:
            self._hot_streak += 1
            # 判定为人声插话累积中，不提升扬声器基线包络
            return self._hot_streak >= self._barge_in_frames

        # 未超插话阈值：确认为扬声器正常发声或背景音，更新扬声器包络
        self._hot_streak = 0
        if not self._barge_in_active:
            if rms > self._peak_envelope:
                self._peak_envelope = rms
            else:
                self._peak_envelope = 0.96 * self._peak_envelope + 0.04 * rms
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
            model=_resolve_stt_model(
                settings.stt_model,
                allow_downloads=settings.allow_model_downloads,
            ),
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
        compaction_config=settings.context_compaction_config(),
    )

    tts = LocalBridgeTTSService(
        api_key="local",
        base_url=settings.tts_bridge_url,
        sample_rate=TTS_OUTPUT_SAMPLE_RATE,
        settings=LocalBridgeTTSService.Settings(voice=TTS_ENGINE_DEFAULT_VOICE),
    )

    context = context or LLMContext(
        messages=[{"role": "system", "content": build_system_prompt(persona)}]
    )
    echo_state = EchoState()
    echo_buffer = EchoTextBuffer(window_secs=settings.echo_text_window_secs)
    echo_suppressor = EchoSuppressionProcessor(
        barge_in_gain=settings.echo_barge_in_gain,
        barge_in_frames=settings.echo_barge_in_frames,
        tail_hangover_secs=settings.echo_tail_hangover_secs,
        echo_state=echo_state,
        allow_barge_in=settings.echo_allow_barge_in,
    )
    self_echo_filter = SelfEchoFilter(
        echo_buffer,
        min_ratio=settings.echo_text_similarity,
        echo_state=echo_state,
        tail_hangover_secs=settings.echo_tail_hangover_secs,
    )
    bot_text_recorder = BotTextRecorder(echo_buffer)
    tts_monitor = TTSStateObserver(echo_state)

    # 1.7 组装：VAD 分析与 HangoverUserMuteStrategy 集成进 LLMUserAggregatorParams，
    # LLM 之后直连 TTS（TTS 直接消费 LLMTextFrame 流），assistant 聚合器在管道末尾回写上下文。
    pair = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(
                sample_rate=settings.sample_rate,
                params=VADParams(stop_secs=settings.silence_secs),
            ),
            user_mute_strategies=[
                HangoverUserMuteStrategy(
                    tail_hangover_secs=settings.echo_tail_hangover_secs,
                    echo_state=echo_state,
                )
            ],
        ),
    )

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
            tts_monitor,
            transport.output(),
            pair.assistant(),
        ]
    )
