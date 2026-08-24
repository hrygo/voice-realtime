"""严格控制协议到交互运行时与 TTS 桥的分发。"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Any, Protocol

from pydantic import ValidationError

from voice_realtime.config import BridgeSettings
from voice_realtime.network import local_async_client
from voice_realtime.ui.protocol import (
    ClearContextCommand,
    ClearSubtitlesCommand,
    CommandResponse,
    ControlCommand,
    DuplexMode,
    EndMeetingCommand,
    ErrorCode,
    RestartCommand,
    RuntimeStateSnapshot,
    SetBargeInModeCommand,
    SetDuplexModeCommand,
    SetMicMutedCommand,
    SetPersonaCommand,
    SetVoiceCommand,
    StartAssistantCommand,
    StartMeetingCommand,
    StopActiveModeCommand,
    StopSessionCommand,
    parse_command,
)

logger = logging.getLogger(__name__)

_COMMAND_NAMES = frozenset(
    {
        "clear_context",
        "clear_subtitles",
        "stop_session",
        "restart",
        "set_persona",
        "set_voice",
        "set_duplex_mode",
        "set_barge_in_mode",
        "set_mic_muted",
        "start_meeting",
        "end_meeting",
        "start_assistant",
        "stop_active_mode",
    }
)


class ControlRuntime(Protocol):
    async def clear_context(self) -> None: ...
    async def clear_subtitles(self) -> None: ...
    async def stop_session(self) -> None: ...
    async def restart_pipeline(self) -> None: ...
    async def set_mic_muted(self, muted: bool) -> None: ...
    async def start_meeting(self, title: str | None = None) -> Any: ...
    async def end_meeting(self, meeting_id: str | None = None) -> Any: ...
    async def start_assistant(self) -> None: ...
    async def stop_active_mode(self) -> None: ...
    def set_persona(self, persona: str) -> None: ...
    def set_voice(self, voice: str) -> None: ...
    def set_duplex_mode(self, mode: DuplexMode) -> None: ...
    def snapshot(self) -> RuntimeStateSnapshot: ...


class ControlBridge:
    """解析并执行一条命令，始终返回完整的服务端权威状态。"""

    def __init__(self, runtime: ControlRuntime, bridge: BridgeSettings) -> None:
        self._runtime = runtime
        self._bridge = bridge
        self._response_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._response_cache_size = 128
        self._lock = asyncio.Lock()

    async def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            return await self._handle(payload)

    async def _handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = payload.get("request_id")
        cmd = payload.get("cmd")
        safe_request_id = request_id if isinstance(request_id, str) else ""
        safe_cmd = cmd if isinstance(cmd, str) else ""
        if safe_request_id:
            cached = self._response_cache.get(safe_request_id)
            if cached is not None:
                return cached
        try:
            command = parse_command(payload)
        except (ValidationError, ValueError):
            error_code = (
                ErrorCode.INVALID_COMMAND
                if isinstance(cmd, str) and cmd not in _COMMAND_NAMES
                else ErrorCode.INVALID_PAYLOAD
            )
            return self._remember(
                safe_request_id,
                self._response(
                request_id=safe_request_id,
                cmd=safe_cmd,
                ok=False,
                error_code=error_code,
                message="控制命令无效",
                ),
            )
        try:
            await self._dispatch(command)
        except Exception as exc:
            logger.exception("ControlBridge: 命令 %s 执行失败", command.cmd)
            error_code = getattr(exc, "code", ErrorCode.COMMAND_FAILED)
            try:
                normalized_code = ErrorCode(error_code)
            except ValueError:
                normalized_code = ErrorCode.COMMAND_FAILED
            return self._remember(
                command.request_id,
                self._response(
                request_id=command.request_id,
                cmd=command.cmd,
                ok=False,
                error_code=normalized_code,
                message="命令执行失败，请检查相关服务状态",
                ),
            )
        return self._remember(
            command.request_id,
            self._response(request_id=command.request_id, cmd=command.cmd, ok=True),
        )

    async def _dispatch(self, command: ControlCommand) -> None:
        if isinstance(command, ClearContextCommand):
            await self._runtime.clear_context()
        elif isinstance(command, ClearSubtitlesCommand):
            await self._runtime.clear_subtitles()
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
        elif isinstance(command, StartMeetingCommand):
            await self._runtime.start_meeting(command.title)
        elif isinstance(command, EndMeetingCommand):
            await self._runtime.end_meeting(command.meeting_id)
        elif isinstance(command, StartAssistantCommand):
            await self._runtime.start_assistant()
        elif isinstance(command, StopActiveModeCommand):
            await self._runtime.stop_active_mode()

    async def _set_voice(self, voice: str) -> None:
        host = "127.0.0.1" if self._bridge.host in {"0.0.0.0", "::"} else self._bridge.host
        url = f"http://{host}:{self._bridge.port}/v1/voice"
        async with local_async_client(timeout=5.0) as client:
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
            contract_version="1" if cmd in {
                "start_meeting",
                "end_meeting",
                "start_assistant",
                "stop_active_mode",
            } else None,
            error=(
                {
                    "code": error_code.value,
                    "message": message or "命令执行失败",
                    "request_id": request_id,
                    "details": {},
                }
                if error_code is not None
                else None
            ),
        )
        return response.model_dump(mode="json")

    def _remember(self, request_id: str, response: dict[str, Any]) -> dict[str, Any]:
        if request_id:
            self._response_cache[request_id] = response
            self._response_cache.move_to_end(request_id)
            while len(self._response_cache) > self._response_cache_size:
                self._response_cache.popitem(last=False)
        return response
