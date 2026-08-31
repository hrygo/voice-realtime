"""SpeechRail Realtime v2 的唯一 ASR 运行配置。"""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SpeechRailRealtimeProfile(BaseModel):
    """字幕和会议共用的 SpeechRail Realtime v2 profile。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = "speechrail-realtime-v2"
    url: str
    language: str = Field(min_length=1, max_length=64)
    connect_timeout_secs: float = Field(default=5.0, gt=0.0, le=30.0)
    final_timeout_secs: float = Field(default=10.0, gt=0.0, le=120.0)

    @field_validator("kind")
    @classmethod
    def _require_speechrail_kind(cls, value: str) -> str:
        if value != "speechrail-realtime-v2":
            raise ValueError("仅支持 speechrail-realtime-v2 ASR")
        return value

    @field_validator("url")
    @classmethod
    def _require_v2_websocket_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if (
            parsed.scheme not in {"ws", "wss"}
            or parsed.hostname is None
            or parsed.path != "/v2/realtime"
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("SpeechRail URL 必须是无凭据的 ws(s)://host/v2/realtime")
        return normalized

    @field_validator("language")
    @classmethod
    def _strip_language(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("字段不能为空")
        return stripped
