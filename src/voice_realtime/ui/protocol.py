"""Voice Studio 控制 WebSocket 的严格消息契约。"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

from voice_realtime.interaction.types import DuplexMode as DuplexMode

RequestId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
PersonaText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4000),
]


class CommandBase(BaseModel):
    """所有控制命令的公共字段。"""

    model_config = ConfigDict(extra="forbid")

    request_id: RequestId


class ClearContextCommand(CommandBase):
    cmd: Literal["clear_context"]


class StopSessionCommand(CommandBase):
    cmd: Literal["stop_session"]


class RestartCommand(CommandBase):
    cmd: Literal["restart"]


class SetPersonaCommand(CommandBase):
    cmd: Literal["set_persona"]
    prompt: PersonaText


class SetVoiceCommand(CommandBase):
    cmd: Literal["set_voice"]
    voice: ShortText


class SetDuplexModeCommand(CommandBase):
    cmd: Literal["set_duplex_mode"]
    mode: DuplexMode


class SetBargeInModeCommand(CommandBase):
    """旧前端命令名；语义与 set_duplex_mode 相同。"""

    cmd: Literal["set_barge_in_mode"]
    mode: DuplexMode


class SetMicMutedCommand(CommandBase):
    cmd: Literal["set_mic_muted"]
    muted: bool


ControlCommand = Annotated[
    ClearContextCommand
    | StopSessionCommand
    | RestartCommand
    | SetPersonaCommand
    | SetVoiceCommand
    | SetDuplexModeCommand
    | SetBargeInModeCommand
    | SetMicMutedCommand,
    Field(discriminator="cmd"),
]
_COMMAND_ADAPTER: TypeAdapter[ControlCommand] = TypeAdapter(ControlCommand)


class RuntimeStateSnapshot(BaseModel):
    """服务端权威运行状态；连接建立和每次命令后完整返回。"""

    model_config = ConfigDict(extra="forbid")

    pipeline: str
    subtitle: str
    mic_muted: bool
    persona: str | None
    voice: str
    duplex_mode: DuplexMode
    session_started_at: str | None


class ErrorCode(StrEnum):
    INVALID_COMMAND = "invalid_command"
    INVALID_PAYLOAD = "invalid_payload"
    COMMAND_FAILED = "command_failed"
    SERVICE_UNAVAILABLE = "service_unavailable"


class CommandResponse(BaseModel):
    """命令确认；失败仅暴露稳定错误码与安全消息。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    cmd: str
    ok: bool
    state: RuntimeStateSnapshot
    error_code: ErrorCode | None = None
    message: str | None = None


class RuntimeStateEvent(BaseModel):
    """控制连接建立时主动发送的状态握手。"""

    model_config = ConfigDict(extra="forbid")

    event: Literal["state"] = "state"
    state: RuntimeStateSnapshot


def parse_command(payload: Mapping[str, Any]) -> ControlCommand:
    """严格解析控制命令，拒绝未知命令、缺失字段和额外字段。"""
    return _COMMAND_ADAPTER.validate_python(dict(payload))
