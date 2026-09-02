"""集中配置层：各子系统独立配置与顶层聚合根。

使用 pydantic-settings，支持环境变量覆盖（前缀 `SONA_`）与 `.env` 文件。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sona.config.audio import AudioCaptureSettings
from sona.config.interaction import InteractionSettings
from sona.config.lm_studio import LMStudioSettings
from sona.config.meeting import MeetingSettings
from sona.config.subtitles import SubtitleSettings
from sona.config.ui import UISettings
from sona.config.validators import (
    ALLOWED_STT_LANGUAGES,
    SPEECHRAIL_TTS_MODEL,
    SPEECHRAIL_TTS_VOICE_ALIASES,
    SPEECHRAIL_TTS_VOICE_IDS,
    TTS_OUTPUT_SAMPLE_RATE,
    normalize_speechrail_tts_voice,
    validate_listen_host,
    validate_service_url,
)


class Settings(BaseSettings):
    """聚合配置入口。"""

    model_config = SettingsConfigDict(env_prefix="SONA_", env_file=".env", extra="ignore")

    lm_studio: LMStudioSettings = Field(default_factory=LMStudioSettings)
    interaction: InteractionSettings = Field(default_factory=InteractionSettings)
    audio_capture: AudioCaptureSettings = Field(default_factory=AudioCaptureSettings)
    subtitles: SubtitleSettings = Field(default_factory=SubtitleSettings)
    meeting: MeetingSettings = Field(default_factory=MeetingSettings)
    ui: UISettings = Field(default_factory=UISettings)

    @model_validator(mode="after")
    def _synchronize_lm_studio_compatibility(self) -> Settings:
        explicit_lm_studio = "lm_studio" in self.model_fields_set
        explicit_interaction = "interaction" in self.model_fields_set
        if explicit_lm_studio or (not explicit_interaction and self.lm_studio.model_fields_set):
            self.interaction = self.interaction.model_copy(
                update={
                    "llm_base_url": self.lm_studio.base_url,
                    "llm_api_key": self.lm_studio.api_key,
                }
            )
        else:
            self.lm_studio = self.lm_studio.model_copy(
                update={
                    "base_url": self.interaction.llm_base_url,
                    "api_key": self.interaction.llm_api_key,
                }
            )
        return self

    def dump_table(self) -> str:
        """以可读表格输出当前生效配置（用于启动横幅与诊断）。"""
        lines = ["sona 配置"]
        for section in (
            self.lm_studio,
            self.interaction,
            self.audio_capture,
            self.subtitles,
            self.meeting,
            self.ui,
        ):
            lines.append(f"\n[{type(section).__name__}]")
            for key, value in section.model_dump(by_alias=True).items():
                safe_value = (
                    "<redacted>" if key == "database_url" or key.endswith("api_key") else value
                )
                lines.append(f"  {key}: {safe_value}")
        return "\n".join(lines)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级单例配置（FastAPI 依赖注入与 CLI 共用）。"""
    return Settings()


__all__ = [
    "ALLOWED_STT_LANGUAGES",
    "SPEECHRAIL_TTS_MODEL",
    "SPEECHRAIL_TTS_VOICE_ALIASES",
    "SPEECHRAIL_TTS_VOICE_IDS",
    "TTS_OUTPUT_SAMPLE_RATE",
    "AudioCaptureSettings",
    "InteractionSettings",
    "LMStudioSettings",
    "MeetingSettings",
    "Settings",
    "SubtitleSettings",
    "UISettings",
    "get_settings",
    "normalize_speechrail_tts_voice",
    "validate_listen_host",
    "validate_service_url",
]
