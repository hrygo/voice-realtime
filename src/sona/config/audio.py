"""原生物理输出采集 Helper 配置与限额。"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AudioCaptureSettings(BaseSettings):
    """原生物理输出采集 Helper 的本机进程与 IPC 限额。"""

    model_config = SettingsConfigDict(
        env_prefix="SONA_AUDIO_CAPTURE_",
        env_file=".env",
        extra="ignore",
    )

    enabled: bool = False
    helper_executable: Path | None = None
    runtime_dir: Path = Path("runtime/audio-capture")
    startup_timeout_secs: float = Field(default=5.0, gt=0.0, le=30.0)
    command_timeout_secs: float = Field(default=30.0, gt=0.0, le=30.0)
    queue_size: int = Field(default=8, ge=1, le=128)
    restart_attempts: int = Field(default=3, ge=0, le=10)
    restart_backoff_secs: float = Field(default=0.25, ge=0.001, le=10.0)
    max_restart_backoff_secs: float = Field(default=2.0, ge=0.001, le=30.0)

    @model_validator(mode="after")
    def _validate_restart_backoff(self) -> AudioCaptureSettings:
        if self.max_restart_backoff_secs < self.restart_backoff_secs:
            raise ValueError(
                "max_restart_backoff_secs must be greater than or equal to restart_backoff_secs"
            )
        return self
