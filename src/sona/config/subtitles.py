"""SpeechRail OpenAI Realtime 字幕与转录配置。"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sona.asr.profiles import SpeechRailRealtimeProfile


class SubtitleSettings(BaseSettings):
    """SpeechRail OpenAI Realtime 字幕与会议转录配置。"""

    model_config = SettingsConfigDict(env_prefix="SONA_SUBTITLE_", env_file=".env", extra="ignore")

    language: str = Field(default="Chinese", description="字幕与会议转写语言")
    output_dir: Path = Field(default=Path("runtime/subtitles"), description="SRT 输出目录")
    speechrail_url: str = Field(
        default="ws://127.0.0.1:8201/v1/realtime",
        description="SpeechRail OpenAI Realtime WebSocket 地址",
    )
    speechrail_connect_timeout_secs: float = Field(default=5.0, gt=0.0, le=30.0)
    speechrail_finish_timeout_secs: float = Field(default=10.0, gt=0.0, le=120.0)
    speechrail_api_key: str | None = Field(
        default=None,
        description="SpeechRail 可选 API key；仅通过 WebSocket Authorization header 发送",
    )

    @field_validator("speechrail_url")
    @classmethod
    def _validate_speechrail_url(cls, value: str) -> str:
        return SpeechRailRealtimeProfile(url=value, language="zh").url

    @field_validator("speechrail_api_key", mode="before")
    @classmethod
    def _normalize_speechrail_api_key(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @property
    def asr_profile(self) -> SpeechRailRealtimeProfile:
        return SpeechRailRealtimeProfile(
            url=self.speechrail_url,
            language=self.language,
            connect_timeout_secs=self.speechrail_connect_timeout_secs,
            final_timeout_secs=self.speechrail_finish_timeout_secs,
        )

    @property
    def speechrail_health_url(self) -> str:
        parsed = urlsplit(self.speechrail_url)
        scheme = "https" if parsed.scheme == "wss" else "http"
        return f"{scheme}://{parsed.netloc}/health"
