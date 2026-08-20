"""严格控制协议到交互运行时与 TTS 桥的分发。"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from voice_realtime.config import BridgeSettings
from voice_realtime.ui.protocol import (
    ClearContextCommand,
    CommandResponse,
    ControlCommand,
    DuplexMode,
    ErrorCode,
    RestartCommand,
    RuntimeStateSnapshot,
    SetBargeInModeCommand,
    SetDuplexModeCommand,
    SetMicMutedCommand,
    SetPersonaCommand,
    SetVoiceCommand,
    StopSessionCommand,
    parse_command,
)

logger = logging.getLogger(__name__)

_COMMAND_NAMES = frozenset(
    {
        "clear_context",
        "stop_session",
        "restart",
        "set_persona",
        "set_voice",
        "set_duplex_mode",
        "set_barge_in_mode",
        "set_mic_muted",
    }
)


class ControlRuntime(Protocol):
    async def clear_context(self) -> None: ...
    async def stop_session(self) -> None: ...
    async def restart_pipeline(self) -> None: ...
    async def set_mic_muted(self, muted: bool) -> None: ...
    def set_persona(self, persona: str) -> None: ...
    def set_voice(self, voice: str) -> None: ...
    def set_duplex_mode(self, mode: DuplexMode) -> None: ...
    def snapshot(self) -> RuntimeStateSnapshot: ...


class ControlBridge:
    """解析并执行一条命令，始终返回完整的服务端权威状态。"""

    def __init__(self, runtime: ControlRuntime, bridge: BridgeSettings) -> None:
        self._runtime = runtime
        self._bridge = bridge

    async def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = payload.get("request_id")
        cmd = payload.get("cmd")
        safe_request_id = request_id if isinstance(request_id, str) else ""
        safe_cmd = cmd if isinstance(cmd, str) else ""
        try:
            command = parse_command(payload)
        except (ValidationError, ValueError):
            error_code = (
                ErrorCode.INVALID_COMMAND
                if isinstance(cmd, str) and cmd not in _COMMAND_NAMES
                else ErrorCode.INVALID_PAYLOAD
            )
            return self._response(
                request_id=safe_request_id,
                cmd=safe_cmd,
                ok=False,
                error_code=error_code,
                message="控制命令无效",
            )
        try:
            await self._dispatch(command)
        except Exception:
            logger.exception("ControlBridge: 命令 %s 执行失败", command.cmd)
            return self._response(
                request_id=command.request_id,
                cmd=command.cmd,
                ok=False,
                error_code=ErrorCode.COMMAND_FAILED,
                message="命令执行失败，请检查相关服务状态",
            )
        return self._response(request_id=command.request_id, cmd=command.cmd, ok=True)

    async def _dispatch(self, command: ControlCommand) -> None:
        if isinstance(command, ClearContextCommand):
            await self._runtime.clear_context()
        elif isinstance(command, StopSessionCommand):
            await self._runtime.stop_session()
        elif isinstance(command, RestartCommand):
            await self._runtime.restart_pipeline()
        elif isinstance(command, SetPersonaCommand):
            self._runtime.set_persona(command.prompt)
            await self._runtime.clear_context()
        elif isinstance(command, SetVoiceCommand):
            await self._set_voice(command.voice)
            self._runtime.set_voice(command.voice)
        elif isinstance(command, (SetDuplexModeCommand, SetBargeInModeCommand)):
            self._runtime.set_duplex_mode(command.mode)
        elif isinstance(command, SetMicMutedCommand):
            await self._runtime.set_mic_muted(command.muted)

    async def _set_voice(self, voice: str) -> None:
        url = f"http://{self._bridge.host}:{self._bridge.port}/v1/voice"
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(url, json={"voice": voice})
            response.raise_for_status()

    def _response(
        self,
        *,
        request_id: str,
        cmd: str,
        ok: bool,
        error_code: ErrorCode | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        response = CommandResponse(
            request_id=request_id,
            cmd=cmd,
            ok=ok,
            state=self._runtime.snapshot(),
            error_code=error_code,
            message=message,
        )
        return response.model_dump(mode="json")
