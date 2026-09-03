"""Pipecat 语音交互管道配置。"""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sona.asr.profiles import SpeechRailRealtimeProfile
from sona.config.lm_studio import DEFAULT_LLM_MODEL, DEFAULT_LM_STUDIO_URL
from sona.config.validators import (
    ALLOWED_STT_LANGUAGES,
    SPEECHRAIL_TTS_MODEL,
    normalize_speechrail_tts_voice,
    validate_service_url,
)
from sona.interaction.context_memory import ContextCompactionConfig
from sona.lm_studio import DEFAULT_LM_STUDIO_API_KEY


class InteractionSettings(BaseSettings):
    """Pipecat 交互管道配置（SpeechRail STT/TTS → LM Studio）。"""

    model_config = SettingsConfigDict(
        env_prefix="SONA_INTERACTION_",
        env_file=".env",
        extra="ignore",
    )

    llm_base_url: str = Field(
        default=DEFAULT_LM_STUDIO_URL, description="LM Studio OpenAI 兼容端点"
    )
    llm_model: str = Field(default=DEFAULT_LLM_MODEL, description="交互 LLM 模型 ID")
    llm_api_key: str = Field(
        default=DEFAULT_LM_STUDIO_API_KEY,
        min_length=1,
        description="LM Studio API key；仅通过 Authorization header 发送，日志中脱敏",
    )
    llm_temperature: float = Field(default=0.7, description="非 thinking 采样温度")
    context_compaction_enabled: bool = Field(
        default=True,
        description="是否启用 LM Studio 原生会话链后台压缩",
    )
    context_soft_input_tokens: int = Field(
        default=16384,
        ge=512,
        description="达到后后台生成压缩候选的真实 input token 水位",
    )
    context_hard_input_tokens: int = Field(
        default=32768,
        ge=1024,
        description="上下文延迟保护水位；失败时保留旧链而非破坏性截断",
    )
    context_target_input_tokens: int = Field(
        default=8192,
        ge=256,
        description="新链预热后的目标 input token 规模",
    )
    context_recent_turn_pairs: int = Field(
        default=16,
        ge=1,
        le=16,
        description="压缩时优先原样保留的最近完整问答组数",
    )
    context_max_unsummarized_messages: int = Field(
        default=128,
        ge=4,
        le=1000,
        description="低 token、多轮短对话的备用压缩触发器",
    )
    context_ttft_soft_seconds: float = Field(
        default=3.0,
        ge=0.1,
        le=30.0,
        description="连续两轮达到时提前触发压缩的 TTFT 秒数",
    )
    context_summary_max_output_tokens: int = Field(
        default=2048,
        ge=128,
        le=4096,
        description="结构化摘要的最大输出 token 数",
    )
    context_summary_timeout_seconds: float = Field(
        default=30.0,
        ge=1.0,
        le=120.0,
        description="单次原生摘要调用超时秒数",
    )
    context_capacity_ratio: float = Field(
        default=0.8,
        ge=0.5,
        le=0.95,
        description="已加载模型上下文容量的紧急保护比例",
    )
    stt_language: str = Field(default="zh", description="STT 语言 (zh/yue/en/ja/ko)")
    speechrail_realtime_url: str = Field(default="ws://127.0.0.1:8201/v1/realtime")
    speechrail_tts_rest_url: str = Field(
        default="http://127.0.0.1:8201/v1",
        description="SpeechRail TTS REST 试听端点；交互播放走 realtime OpenAI",
    )
    speechrail_tts_model: str = Field(
        default=SPEECHRAIL_TTS_MODEL,
        description="SpeechRail 公共 TTS 逻辑模型 ID",
    )
    tts_voice: str = Field(
        default="default",
        min_length=1,
        description="SpeechRail TTS preset；alloy 仅兼容到 2026-10-31",
    )
    tts_language: str = Field(default="auto", description="SpeechRail TTS 语言或 auto")
    speechrail_api_key: str | None = Field(
        default=None,
        description="SpeechRail 可选 API key；仅通过 HTTP/WebSocket Authorization header 发送",
    )
    input_device: int | None = Field(default=None, description="麦克风设备索引 (None=系统默认)")
    input_device_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="麦克风设备名称或唯一名称片段；配置后找不到设备即停止采集",
    )
    sample_rate: int = Field(default=16000, description="音频管线采样率")
    silence_secs: float = Field(
        default=0.45,
        ge=0.1,
        le=3.0,
        description="端点判定静音阈值 (秒)；需小于 STT ttfs_p99 以保留转写等待窗口",
    )
    vad_confidence: float = Field(
        default=0.7,
        ge=0.1,
        le=1.0,
        description="Silero VAD 人声判定置信度阈值",
    )
    vad_start_secs: float = Field(
        default=0.2,
        ge=0.05,
        le=1.0,
        description="VAD 判定人声开始所需持续语音秒数",
    )
    vad_min_volume: float = Field(
        default=0.65,
        ge=0.1,
        le=1.0,
        description="VAD 最小音量能量门槛（过滤环境微弱杂音/按键声/呼吸声）",
    )
    echo_barge_in_gain: float = Field(
        default=2.5,
        ge=1.2,
        le=8.0,
        description="插话能量门限：输入 RMS 超过回声基线 × 增益即判定为真人插话",
    )
    echo_barge_in_frames: int = Field(
        default=3,
        ge=1,
        le=20,
        description="插话判定所需连续超阈帧数（~32ms/帧 @16k/512B）",
    )
    echo_text_window_secs: float = Field(
        default=10.0,
        ge=0.5,
        le=120.0,
        description="自回声判定：保留机器人最近播报文本的秒数窗口",
    )
    echo_text_similarity: float = Field(
        default=0.7,
        ge=0.3,
        le=0.99,
        description="自回声文本相似度阈值（difflib ratio / 最长公共子串覆盖率）",
    )
    echo_allow_barge_in: bool = Field(
        default=False,
        description="是否允许真人插话打断（False=输出期物理闭麦，彻底防自打断与回声；True=能量门控插话）",
    )
    echo_tail_hangover_secs: float = Field(
        default=0.4,
        ge=0.0,
        le=3.0,
        description="TTS 播报结束后继续抑制麦克风输入的秒数（吸收声学混响与声卡缓冲尾延）",
    )
    max_session_seconds: int = Field(
        default=0,
        ge=0,
        description="单次会话上限 (秒)；0 表示随 UI 服务持续运行",
    )
    smart_turn_enabled: bool = Field(
        default=False,
        description="是否启用 LocalSmartTurn 进行语义端点判定（中文建议 False）",
    )
    smart_turn_stop_secs: float = Field(
        default=0.45,
        ge=0.1,
        le=3.0,
        description="Smart Turn 判定静音窗口（秒）",
    )
    tts_fast_first_clause: bool = Field(
        default=True,
        description="TTS 是否启用中文首句弱标点/连词加速以降低首字发音延迟 (TTFA)",
    )
    tts_first_clause_min_chars: int = Field(
        default=8,
        ge=2,
        le=50,
        description="首句弱标点加速所需的最小字符数门槛",
    )

    @field_validator("stt_language")
    @classmethod
    def _validate_stt_language(cls, v: str) -> str:
        normalized = v.lower()
        if normalized not in ALLOWED_STT_LANGUAGES:
            raise ValueError(f"不支持的 STT 语言: {v} (可选 {sorted(ALLOWED_STT_LANGUAGES)})")
        return normalized

    @field_validator("tts_language")
    @classmethod
    def _validate_tts_language(cls, value: str) -> str:
        normalized = value.lower()
        if normalized != "auto" and normalized not in ALLOWED_STT_LANGUAGES:
            raise ValueError(f"不支持的 TTS 语言: {value}")
        return normalized

    @field_validator("tts_voice")
    @classmethod
    def _validate_tts_voice(cls, value: str) -> str:
        return normalize_speechrail_tts_voice(value)

    @field_validator("speechrail_tts_model")
    @classmethod
    def _validate_speechrail_tts_model(cls, value: str) -> str:
        if value != SPEECHRAIL_TTS_MODEL:
            raise ValueError(f"TTS model 必须是 {SPEECHRAIL_TTS_MODEL}")
        return value

    @field_validator("sample_rate")
    @classmethod
    def _validate_sample_rate(cls, v: int) -> int:
        if v != 16000:
            raise ValueError(f"交互音频管线仅支持 16000Hz: {v}")
        return v

    @field_validator("speechrail_realtime_url")
    @classmethod
    def _validate_speechrail_realtime_url(cls, value: str) -> str:
        return SpeechRailRealtimeProfile(url=value, language="zh").url

    @field_validator("input_device_name", mode="before")
    @classmethod
    def _normalize_input_device_name(cls, v: object) -> object:
        if isinstance(v, str):
            stripped = v.strip()
            return stripped or None
        return v

    @field_validator("llm_api_key", "speechrail_api_key", mode="before")
    @classmethod
    def _normalize_api_key(cls, v: object) -> object:
        if isinstance(v, str):
            stripped = v.strip()
            return stripped or None
        return v

    @model_validator(mode="after")
    def _validate_input_device_selector(self) -> InteractionSettings:
        if self.input_device is not None and self.input_device_name is not None:
            raise ValueError("麦克风设备索引与名称不能同时配置")
        return self

    @model_validator(mode="after")
    def _validate_context_compaction_thresholds(self) -> InteractionSettings:
        if not (
            self.context_target_input_tokens
            < self.context_soft_input_tokens
            < self.context_hard_input_tokens
        ):
            raise ValueError("上下文 token 水位必须满足 target < soft < hard")
        return self

    def context_compaction_config(self) -> ContextCompactionConfig:
        """映射为不依赖 pydantic-settings 的运行时压缩配置。"""
        return ContextCompactionConfig(
            enabled=self.context_compaction_enabled,
            soft_input_tokens=self.context_soft_input_tokens,
            hard_input_tokens=self.context_hard_input_tokens,
            target_input_tokens=self.context_target_input_tokens,
            recent_turn_pairs=self.context_recent_turn_pairs,
            max_unsummarized_messages=self.context_max_unsummarized_messages,
            ttft_soft_seconds=self.context_ttft_soft_seconds,
            summary_max_output_tokens=self.context_summary_max_output_tokens,
            summary_timeout_seconds=self.context_summary_timeout_seconds,
            capacity_ratio=self.context_capacity_ratio,
        )

    _validate_local_urls = field_validator(
        "llm_base_url", "speechrail_tts_rest_url"
    )(validate_service_url)
