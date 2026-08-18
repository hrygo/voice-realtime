"""OpenAI 兼容 TTS 请求/响应模型（`POST /v1/audio/speech` 子集）。

对齐 OpenAI Audio API 规范；本地桥只实现 WAV/PCM 两种流式输出格式
（mp3/opus/aac/flac 需要额外编码器，非实时链路必需，故不实现）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

ResponseFormat = Literal["wav", "pcm"]
Voice = str  # VoiceDesign profile 名（如 "default" / "luna" / 自定义设计提示）


class SpeechRequest(BaseModel):
    """OpenAI 兼容语音合成请求体。"""

    model: str = Field(description="模型 ID（桥忽略该值，固定使用配置的 Qwen3-TTS）")
    input: str = Field(min_length=1, max_length=2000, description="待合成文本")
    voice: Voice = Field(
        default="default",
        description=(
            "兼容字段：桥忽略该值，输出音色固定由服务配置 VR_BRIDGE_VOICE 决定"
        ),
    )
    response_format: ResponseFormat = Field(
        default="wav", description="输出格式：wav(带头) / pcm(裸 16-bit LE)"
    )
    speed: float = Field(default=1.0, ge=0.25, le=4.0, description="语速倍率")
    lang: str = Field(default="auto", description="合成语言 (auto/chinese/english/…)")

    @field_validator("input")
    @classmethod
    def _strip_blank(cls, v: str) -> str:
        return v.strip()

    @field_validator("lang")
    @classmethod
    def _strip_blank_lang(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("lang must not be blank")
        return value


class HealthResponse(BaseModel):
    """健康检查响应。"""

    status: Literal["ok", "warming_up", "error"] = "ok"
    engine: str = "mlx-audio Qwen3-TTS"
    model: str
    voice: str
    sample_rate: int
    format: str = "wav"


class VoiceUpdateRequest(BaseModel):
    """音色热切换请求（`POST /v1/voice`）。"""

    voice: Voice = Field(min_length=1, max_length=200, description="音色 profile 名或自定义描述")

    @field_validator("voice")
    @classmethod
    def _strip_blank(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("voice must not be blank")
        return value


class VoiceResponse(BaseModel):
    """音色状态响应（`GET /v1/voices` 与 `POST /v1/voice` 共用）。"""

    voice: str
    available: list[str]
