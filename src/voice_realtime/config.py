"""集中配置层：TTS 桥 / 交互管道 / 字幕服务 三个子系统的全部可调参数。

使用 pydantic-settings，支持环境变量覆盖（前缀 `VR_`）与 `.env` 文件。
"""

from __future__ import annotations

from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from voice_realtime.asr.profiles import (
    ASRProfile,
    WLKAutoProfile,
    WLKQwen3Profile,
    WLKSenseVoiceProfile,
)
from voice_realtime.interaction.context_memory import ContextCompactionConfig
from voice_realtime.model_cache import (
    huggingface_snapshot_path,
    modelscope_snapshot_path,
)

DEFAULT_QWEN3_TTS_MODEL = "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-bf16"
DEFAULT_LM_STUDIO_URL = "http://localhost:1234/v1"
DEFAULT_LLM_MODEL = "qwen/qwen3.6-35b-a3b"
DEFAULT_ASR_CONTEXT = (
    "你是中文语音识别器。请逐字准确转写，保留英文、数字、连字符和大小写。"
    "专有名词词表：Voice Studio；LM Studio；Qwen3-ASR；WhisperLiveKit；SenseVoice；"
    "Sortformer；Pipecat；PostgreSQL；MLX。"
)
TTS_OUTPUT_SAMPLE_RATE = 24000  # Qwen3-TTS 原生输出采样率
# Pipecat 会在请求发出前强制校验 OpenAI 官方音色白名单；用合法的 alloy
# 作为内部占位，TTS 桥收到后仍解析为当前 engine.voice。
TTS_ENGINE_DEFAULT_VOICE = "alloy"
ALLOWED_STT_LANGUAGES = frozenset({"zh", "yue", "en", "ja", "ko"})


def _validate_listen_host(value: str) -> str:
    """校验并解析监听地址（支持 0.0.0.0、localhost、lan 别名、私网 IP 及合法主机名）。"""
    host = value.strip().removeprefix("[").removesuffix("]")
    lower = host.lower()
    if lower in {"lan", "lan_ip", "local_network"}:
        from voice_realtime.network import get_lan_ip

        return get_lan_ip()
    if lower in {"localhost", "local", "loopback"}:
        return host
    try:
        ip_address(host)
        return host
    except ValueError:
        import re

        if re.fullmatch(r"[a-zA-Z0-9.\-_]+", host):
            return host
        raise ValueError(f"监听地址无效: {value}") from None


def _validate_service_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError(f"服务 URL 无效: {value}")
    _validate_listen_host(parsed.hostname)
    return value.rstrip("/")


class BridgeSettings(BaseSettings):
    """qwen3-tts-openai 桥配置（mlx-audio Qwen3-TTS 引擎）。"""

    model_config = SettingsConfigDict(env_prefix="VR_BRIDGE_", env_file=".env", extra="ignore")

    host: str = Field(default="127.0.0.1", description="桥服务监听地址")
    port: int = Field(default=8765, description="桥服务监听端口")
    model: str = Field(default=DEFAULT_QWEN3_TTS_MODEL, description="mlx-audio Qwen3-TTS 模型 ID")
    allow_model_downloads: bool = Field(
        default=False,
        description="是否允许 TTS 启动时联网下载模型；默认只使用本地缓存",
    )
    voice: str = Field(default="default", description="VoiceDesign 音色 profile")
    sample_rate: int = Field(default=TTS_OUTPUT_SAMPLE_RATE, description="输出采样率 (Hz)")
    chunk_ms: int = Field(default=100, description="流式分块大小 (ms)")
    warmup_on_start: bool = Field(default=True, description="启动时预热模型")

    @field_validator("sample_rate")
    @classmethod
    def _validate_sample_rate(cls, v: int) -> int:
        if v != TTS_OUTPUT_SAMPLE_RATE:
            raise ValueError(f"TTS 原生输出仅支持 {TTS_OUTPUT_SAMPLE_RATE}Hz: {v}")
        return v

    _validate_host = field_validator("host")(_validate_listen_host)


class InteractionSettings(BaseSettings):
    """Pipecat 交互管道配置（FunASR STT → LM Studio → TTS 桥）。"""

    model_config = SettingsConfigDict(env_prefix="VR_INTERACTION_", env_file=".env", extra="ignore")

    llm_base_url: str = Field(
        default=DEFAULT_LM_STUDIO_URL, description="LM Studio OpenAI 兼容端点"
    )
    llm_model: str = Field(default=DEFAULT_LLM_MODEL, description="交互 LLM 模型 ID")
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
    stt_model: str = Field(
        default="",
        description=(
            "FunASR STT 模型：HF repo ID 或本地路径；空则自动解析 "
            "FunAudioLLM/SenseVoiceSmall 缓存快照"
        ),
    )
    allow_model_downloads: bool = Field(
        default=False,
        description="是否允许交互 STT 在缓存缺失时联网下载；默认严格离线",
    )
    tts_bridge_url: str = Field(
        default="http://127.0.0.1:8765/v1", description="TTS 桥 OpenAI 兼容端点"
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
        default=True,
        description="是否启用 LocalSmartTurn 进行语义端点判定",
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

    @field_validator("sample_rate")
    @classmethod
    def _validate_sample_rate(cls, v: int) -> int:
        if v != 16000:
            raise ValueError(f"交互音频管线仅支持 16000Hz: {v}")
        return v

    @field_validator("input_device_name", mode="before")
    @classmethod
    def _normalize_input_device_name(cls, v: object) -> object:
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

    _validate_local_urls = field_validator("llm_base_url", "tts_bridge_url")(
        _validate_service_url
    )


class UISettings(BaseSettings):
    """Voice Studio Web 控制台配置（React 前端 + 事件网关）。"""

    model_config = SettingsConfigDict(env_prefix="VR_UI_", env_file=".env", extra="ignore")

    host: str = Field(default="127.0.0.1", description="UI 服务监听地址")
    port: int = Field(default=8100, description="UI 服务监听端口")
    static_dir: Path = Field(default=Path("ui/dist"), description="React 构建产物目录（静态托管）")
    api_timeout: float = Field(default=3.0, description="服务探活超时 (秒)")

    _validate_host = field_validator("host")(_validate_listen_host)


class SubtitleSettings(BaseSettings):
    """WhisperLiveKit 字幕服务配置。"""

    model_config = SettingsConfigDict(env_prefix="VR_SUBTITLE_", env_file=".env", extra="ignore")

    repo_path: Path = Field(
        default=Path("tools/WhisperLiveKit"), description="WhisperLiveKit 克隆路径"
    )
    backend: str = Field(default="qwen3-streaming", description="ASR 后端 (qwen3-streaming/funasr)")
    language: str = Field(default="Chinese", description="字幕语言")
    host: str = Field(default="127.0.0.1", description="字幕服务监听地址")
    port: int = Field(default=8001, description="字幕服务端口")
    model_size: str = Field(default="Qwen3-ASR-1.7B", description="ASR 模型规模")
    model_dir: Path = Field(
        default_factory=lambda: modelscope_snapshot_path("Qwen/Qwen3-ASR-1.7B"),
        description="ASR 本地模型目录（离线环境必填，避免启动时拉取模型）",
    )
    output_dir: Path = Field(default=Path("runtime/subtitles"), description="SRT 输出目录")
    allow_model_downloads: bool = Field(
        default=False,
        description="是否允许 WhisperLiveKit 启动时联网下载模型；默认严格离线",
    )
    diarization: bool = Field(default=True, description="是否启用匿名说话人分离")
    diarization_backend: str = Field(default="sortformer", description="说话人分离后端")
    diarization_model_path: Path = Field(
        default_factory=lambda: huggingface_snapshot_path(
            "nvidia/diar_streaming_sortformer_4spk-v2",
            revision="5240a64075176943f677d30fa2171c780229f341",
        )
        / "diar_streaming_sortformer_4spk-v2.nemo",
        description="本地 Sortformer 模型路径",
    )
    diarization_max_speakers: int = Field(
        default=4, ge=1, le=4, description="最多匿名说话人数"
    )
    punctuation_split: bool = Field(
        default=True,
        description="使用转录标点改善说话人边界",
    )
    context: str = Field(
        default=DEFAULT_ASR_CONTEXT,
        max_length=2000,
        description="Qwen3-ASR 领域词、人名和缩写上下文",
    )
    qwen3_streaming_chunk_sec: float = Field(default=2.0, ge=0.5, le=10.0)
    qwen3_streaming_left_context_sec: float = Field(default=12.0, ge=2.0, le=60.0)
    qwen3_streaming_right_context_ms: int = Field(default=640, ge=0, le=5000)
    qwen3_streaming_hold_back_words: int = Field(default=6, ge=0, le=50)
    qwen3_streaming_stable_iterations: int = Field(default=2, ge=1, le=10)
    qwen3_streaming_max_new_tokens: int = Field(default=256, ge=32, le=2048)
    qwen3_streaming_device: Literal["auto", "mps", "cpu"] = "mps"

    @field_validator("backend")
    @classmethod
    def _validate_backend(cls, v: str) -> str:
        allowed = {"qwen3-streaming", "funasr", "auto"}
        if v not in allowed:
            raise ValueError(f"不支持的 ASR 后端: {v} (可选 {sorted(allowed)})")
        return v

    @field_validator("diarization_backend")
    @classmethod
    def _validate_diarization_backend(cls, v: str) -> str:
        if v != "sortformer":
            raise ValueError("首版仅支持 sortformer diarization 后端")
        return v

    @field_validator("context")
    @classmethod
    def _strip_context(cls, value: str) -> str:
        return value.strip()

    _validate_host = field_validator("host")(_validate_listen_host)

    @property
    def asr_profile(self) -> ASRProfile:
        """把旧字幕配置投影为内部无歧义的 ASR profile。"""
        if self.backend == "funasr":
            return WLKSenseVoiceProfile(
                model_dir=self.model_dir,
                language=self.language,
                host=self.host,
                port=self.port,
                speaker_labels=self.diarization,
            )
        if self.backend == "auto":
            return WLKAutoProfile(
                model_dir=self.model_dir,
                language=self.language,
                host=self.host,
                port=self.port,
                speaker_labels=self.diarization,
            )
        return WLKQwen3Profile(
            model_dir=self.model_dir,
            language=self.language,
            host=self.host,
            port=self.port,
            speaker_labels=self.diarization,
            device=self.qwen3_streaming_device,
            chunk_sec=self.qwen3_streaming_chunk_sec,
            left_context_sec=self.qwen3_streaming_left_context_sec,
            right_context_ms=self.qwen3_streaming_right_context_ms,
            hold_back_words=self.qwen3_streaming_hold_back_words,
            stable_iterations=self.qwen3_streaming_stable_iterations,
            max_new_tokens=self.qwen3_streaming_max_new_tokens,
            context=self.context,
        )


class MeetingSettings(BaseSettings):
    """会议助手 PostgreSQL、纪要和恢复 journal 配置。"""

    model_config = SettingsConfigDict(
        env_prefix="VR_MEETING_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str = Field(
        default="postgresql:///knowledge",
        description="本机 PostgreSQL DSN；不得把完整 DSN 写入日志",
    )
    schema_name: str = Field(
        default="voice_realtime",
        validation_alias=AliasChoices("schema", "VR_MEETING_SCHEMA"),
        serialization_alias="schema",
        description="会议表所在独立 schema",
    )
    summary_model: str = Field(default="qwen/qwen3.6-35b-a3b", description="会后纪要模型 ID")
    summary_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    summary_reasoning: str = Field(default="off", description="纪要推理开关，首版固定 off")
    summary_timeout_secs: float = Field(
        default=60.0,
        ge=5.0,
        le=600.0,
        description="纪要 LM Studio 流式读取空闲超时（秒，兼容旧配置）",
    )
    summary_request_timeout_secs: float = Field(
        default=180.0, ge=5.0, le=600.0, description="单次纪要模型调用总时限（秒）"
    )
    summary_job_timeout_secs: float = Field(
        default=600.0, ge=30.0, le=1800.0, description="整条纪要任务总时限（秒）"
    )
    summary_map_max_output_tokens: int = Field(default=2048, ge=256, le=8192)
    summary_reduce_max_output_tokens: int = Field(default=10240, ge=256, le=16384)
    summary_title_max_output_tokens: int = Field(default=128, ge=32, le=512)
    summary_max_output_chars: int = Field(default=65_536, ge=2_048, le=262_144)
    summary_max_input_chars: int = Field(default=20_000, ge=4_000, le=96_000)
    summary_chunk_max_duration_ms: int = Field(
        default=1_200_000, ge=60_000, le=7_200_000, description="单个 map chunk 最大时长"
    )
    summary_chunk_overlap_segments: int = Field(default=1, ge=0, le=10)
    finalization_timeout_secs: float = Field(default=8.0, ge=1.0, le=300.0)
    recovery_dir: Path = Field(default=Path("runtime/meetings/recovery"))
    summary_concurrency: int = Field(default=1, ge=1, le=8)
    diarization_smoothing_enabled: bool = Field(
        default=True,
        description="是否启用会议说话人时序平滑与短片段杂音滤波",
    )
    diarization_min_duration_ms: int = Field(
        default=350,
        ge=50,
        le=2000,
        description="短片段过滤最小有效时长（毫秒）",
    )
    diarization_hangover_gap_ms: int = Field(
        default=800,
        ge=100,
        le=5000,
        description="同一说话人相邻段落合并最大时间间隙（毫秒）",
    )
    allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "http://127.0.0.1:8100",
            "http://localhost:8100",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"postgresql", "postgres"}:
            raise ValueError("会议数据库必须使用 PostgreSQL URL")
        if parsed.hostname is not None:
            _validate_listen_host(parsed.hostname)
        elif not parsed.path.lstrip("/"):
            raise ValueError("PostgreSQL URL 必须指定数据库")
        return value

    @field_validator("schema_name")
    @classmethod
    def _validate_schema(cls, value: str) -> str:
        import re

        if re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", value) is None:
            raise ValueError("schema 必须是安全的 PostgreSQL 标识符")
        return value

    @field_validator("summary_reasoning")
    @classmethod
    def _validate_reasoning(cls, value: str) -> str:
        if value != "off":
            raise ValueError("会议纪要首版只允许 summary_reasoning=off")
        return value

    @field_validator("allowed_origins")
    @classmethod
    def _validate_origins(cls, values: list[str]) -> list[str]:
        return [_validate_service_url(value) for value in values]


class Settings(BaseSettings):
    """聚合配置入口。"""

    model_config = SettingsConfigDict(env_prefix="VR_", env_file=".env", extra="ignore")

    bridge: BridgeSettings = Field(default_factory=BridgeSettings)
    interaction: InteractionSettings = Field(default_factory=InteractionSettings)
    subtitles: SubtitleSettings = Field(default_factory=SubtitleSettings)
    meeting: MeetingSettings = Field(default_factory=MeetingSettings)
    ui: UISettings = Field(default_factory=UISettings)

    def dump_table(self) -> str:
        """以可读表格输出当前生效配置（用于启动横幅与诊断）。"""
        lines = ["voice-realtime 配置"]
        for section in (self.bridge, self.interaction, self.subtitles, self.meeting, self.ui):
            lines.append(f"\n[{type(section).__name__}]")
            for key, value in section.model_dump(by_alias=True).items():
                safe_value = "<redacted>" if key == "database_url" else value
                lines.append(f"  {key}: {safe_value}")
        return "\n".join(lines)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级单例配置（FastAPI 依赖注入与 CLI 共用）。"""
    return Settings()
