"""ASR 后端的判别配置；每个 profile 只暴露自身可用字段。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _WLKProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_dir: Path
    language: str = Field(min_length=1, max_length=64)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    speaker_labels: bool = True

    @field_validator("language", "host")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("字段不能为空")
        return stripped


class WLKQwen3Profile(_WLKProfile):
    kind: Literal["wlk-qwen3-streaming"] = "wlk-qwen3-streaming"
    device: Literal["auto", "mps", "cpu"] = "mps"
    chunk_sec: float = Field(default=2.0, ge=0.5, le=10.0)
    left_context_sec: float = Field(default=12.0, ge=2.0, le=60.0)
    right_context_ms: int = Field(default=640, ge=0, le=5000)
    hold_back_words: int = Field(default=6, ge=0, le=50)
    stable_iterations: int = Field(default=2, ge=1, le=10)
    max_new_tokens: int = Field(default=256, ge=32, le=2048)
    context: str = Field(default="", max_length=2000)

    @field_validator("context")
    @classmethod
    def _strip_context(cls, value: str) -> str:
        return value.strip()


class WLKSenseVoiceProfile(_WLKProfile):
    kind: Literal["wlk-sensevoice"] = "wlk-sensevoice"
    device: Literal["cpu"] = "cpu"


class WLKAutoProfile(_WLKProfile):
    """兼容旧 `backend=auto`；新配置不得主动选择。"""

    kind: Literal["wlk-auto"] = "wlk-auto"


class FunASRNanoWSProfile(BaseModel):
    """Fun-ASR Nano 官方实时 WebSocket 服务的冻结运行参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["funasr-nano-ws"] = "funasr-nano-ws"
    model_dir: Path
    language: str = Field(min_length=1, max_length=64)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    hotwords: tuple[str, ...] = Field(default=(), max_length=100)
    connect_timeout_secs: float = Field(default=5.0, gt=0.0, le=30.0)
    final_timeout_secs: float = Field(default=10.0, gt=0.0, le=120.0)

    @field_validator("model_dir")
    @classmethod
    def _require_external_model_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("Fun-ASR model_dir 必须是项目外模型缓存的绝对路径")
        return value

    @field_validator("language", "host")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("字段不能为空")
        return stripped

    @field_validator("hotwords")
    @classmethod
    def _normalize_hotwords(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values)
        if any(not value or len(value) > 100 for value in normalized):
            raise ValueError("热词必须为 1 到 100 个字符")
        return normalized


class FunASRNanoPyTorchProfile(BaseModel):
    """Fun-ASR Nano 原生 PyTorch 离线实验臂的冻结运行参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["funasr-nano-pytorch"] = "funasr-nano-pytorch"
    model_dir: Path
    language: str = Field(min_length=1, max_length=64)
    language_source: Literal["profile", "corpus"] = "profile"
    device: Literal["mps", "cpu"]
    hotwords: tuple[str, ...] = Field(default=(), max_length=100)
    itn: bool = True
    ncpu: int = Field(default=4, ge=1, le=32)

    @field_validator("model_dir")
    @classmethod
    def _require_external_model_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("Fun-ASR model_dir 必须是项目外模型缓存的绝对路径")
        return value

    @field_validator("language")
    @classmethod
    def _strip_language(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("字段不能为空")
        return stripped

    @field_validator("hotwords")
    @classmethod
    def _normalize_hotwords(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values)
        if any(not value or len(value) > 100 for value in normalized):
            raise ValueError("热词必须为 1 到 100 个字符")
        return normalized


ASRProfile = Annotated[
    WLKQwen3Profile
    | WLKSenseVoiceProfile
    | WLKAutoProfile
    | FunASRNanoWSProfile
    | FunASRNanoPyTorchProfile,
    Field(discriminator="kind"),
]
