"""集中配置层：TTS 桥 / 交互管道 / 字幕服务 三个子系统的全部可调参数。

使用 pydantic-settings，支持环境变量覆盖（前缀 `VR_`）与 `.env` 文件。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_QWEN3_TTS_MODEL = "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-bf16"
DEFAULT_LM_STUDIO_URL = "http://localhost:1234/v1"
DEFAULT_LLM_MODEL = "qwen/qwen3.6-35b-a3b"
TTS_OUTPUT_SAMPLE_RATE = 24000  # Qwen3-TTS 原生输出采样率
ALLOWED_STT_LANGUAGES = frozenset({"zh", "yue", "en", "ja", "ko"})


class BridgeSettings(BaseSettings):
    """qwen3-tts-openai 桥配置（mlx-audio Qwen3-TTS 引擎）。"""

    model_config = SettingsConfigDict(env_prefix="VR_BRIDGE_", env_file=".env", extra="ignore")

    host: str = Field(default="127.0.0.1", description="桥服务监听地址")
    port: int = Field(default=8765, description="桥服务监听端口")
    model: str = Field(default=DEFAULT_QWEN3_TTS_MODEL, description="mlx-audio Qwen3-TTS 模型 ID")
    voice: str = Field(default="default", description="VoiceDesign 音色 profile")
    sample_rate: int = Field(default=TTS_OUTPUT_SAMPLE_RATE, description="输出采样率 (Hz)")
    chunk_ms: int = Field(default=100, description="流式分块大小 (ms)")
    warmup_on_start: bool = Field(default=True, description="启动时预热模型")

    @field_validator("sample_rate")
    @classmethod
    def _validate_sample_rate(cls, v: int) -> int:
        if v not in (16000, 24000, 44100, 48000):
            raise ValueError(f"不支持的采样率: {v}")
        return v


class InteractionSettings(BaseSettings):
    """Pipecat 交互管道配置（FunASR STT → LM Studio → TTS 桥）。"""

    model_config = SettingsConfigDict(env_prefix="VR_INTERACTION_", env_file=".env", extra="ignore")

    llm_base_url: str = Field(
        default=DEFAULT_LM_STUDIO_URL, description="LM Studio OpenAI 兼容端点"
    )
    llm_model: str = Field(default=DEFAULT_LLM_MODEL, description="交互 LLM 模型 ID")
    llm_temperature: float = Field(default=0.7, description="非 thinking 采样温度")
    stt_language: str = Field(default="zh", description="STT 语言 (zh/yue/en/ja/ko)")
    stt_model: str = Field(
        default="",
        description=(
            "FunASR STT 模型：HF repo ID 或本地路径；空则自动解析 "
            "FunAudioLLM/SenseVoiceSmall 缓存快照"
        ),
    )
    tts_bridge_url: str = Field(
        default="http://127.0.0.1:8765/v1", description="TTS 桥 OpenAI 兼容端点"
    )
    tts_voice: str = Field(default="default", description="交互 TTS 音色")
    input_device: int | None = Field(default=None, description="麦克风设备索引 (None=系统默认)")
    sample_rate: int = Field(default=16000, description="音频管线采样率")
    silence_secs: float = Field(
        default=0.45,
        ge=0.1,
        le=3.0,
        description="端点判定静音阈值 (秒)；需小于 STT ttfs_p99 以保留转写等待窗口",
    )
    interrupt_echo_suppression_ms: int = Field(
        default=500,
        ge=0,
        le=3000,
        description="TTS 播报起始窗口内丢弃麦克风音频 (ms) 以抑制扬声器回声自打断；0=关闭",
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
    max_session_seconds: int = Field(default=600, description="单次会话上限 (秒)")

    @field_validator("stt_language")
    @classmethod
    def _validate_stt_language(cls, v: str) -> str:
        normalized = v.lower()
        if normalized not in ALLOWED_STT_LANGUAGES:
            raise ValueError(f"不支持的 STT 语言: {v} (可选 {sorted(ALLOWED_STT_LANGUAGES)})")
        return normalized


class UISettings(BaseSettings):
    """Voice Studio Web 控制台配置（React 前端 + 事件网关）。"""

    model_config = SettingsConfigDict(env_prefix="VR_UI_", env_file=".env", extra="ignore")

    host: str = Field(default="127.0.0.1", description="UI 服务监听地址")
    port: int = Field(default=8100, description="UI 服务监听端口")
    static_dir: Path = Field(default=Path("ui/dist"), description="React 构建产物目录（静态托管）")
    api_timeout: float = Field(default=3.0, description="服务探活超时 (秒)")


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
    device: str = Field(default="mps", description="推理设备 (mps/cpu)")
    model_size: str = Field(default="Qwen3-ASR-1.7B", description="ASR 模型规模")
    output_dir: Path = Field(default=Path("runtime/subtitles"), description="SRT 输出目录")

    @field_validator("backend")
    @classmethod
    def _validate_backend(cls, v: str) -> str:
        allowed = {"qwen3-streaming", "funasr", "auto"}
        if v not in allowed:
            raise ValueError(f"不支持的 ASR 后端: {v} (可选 {sorted(allowed)})")
        return v


class Settings(BaseSettings):
    """聚合配置入口。"""

    model_config = SettingsConfigDict(env_prefix="VR_", env_file=".env", extra="ignore")

    bridge: BridgeSettings = Field(default_factory=BridgeSettings)
    interaction: InteractionSettings = Field(default_factory=InteractionSettings)
    subtitles: SubtitleSettings = Field(default_factory=SubtitleSettings)
    ui: UISettings = Field(default_factory=UISettings)

    def dump_table(self) -> str:
        """以可读表格输出当前生效配置（用于启动横幅与诊断）。"""
        lines = ["voice-realtime 配置"]
        for section in (self.bridge, self.interaction, self.subtitles):
            lines.append(f"\n[{type(section).__name__}]")
            for key, value in section.model_dump().items():
                lines.append(f"  {key}: {value}")
        return "\n".join(lines)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级单例配置（FastAPI 依赖注入与 CLI 共用）。"""
    return Settings()
