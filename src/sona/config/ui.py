"""Sona Web 控制台与事件网关配置。"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sona.config.validators import validate_listen_host


class UISettings(BaseSettings):
    """Sona Web 控制台配置（React 前端 + 事件网关）。"""

    model_config = SettingsConfigDict(env_prefix="SONA_UI_", env_file=".env", extra="ignore")

    host: str = Field(default="127.0.0.1", description="UI 服务监听地址")
    port: int = Field(default=8100, description="UI 服务监听端口")
    static_dir: Path = Field(default=Path("ui/dist"), description="React 构建产物目录（静态托管）")
    api_timeout: float = Field(default=3.0, description="服务探活超时 (秒)")

    _validate_host = field_validator("host")(validate_listen_host)
