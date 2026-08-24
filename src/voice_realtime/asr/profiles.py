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


ASRProfile = Annotated[
    WLKQwen3Profile | WLKSenseVoiceProfile | WLKAutoProfile,
    Field(discriminator="kind"),
]
