"""ControlBridge：浏览器控制指令 → 管道 / TTS 桥。

把 `/ws/assistant/cmd` 的 JSON 指令分发到对应执行器：
`clear_context`（清空 LLM 上下文）、`stop_session`（停止交互管道）、
`restart`（重启交互管道）、`set_persona`（人格切换 + 清空）、
`set_voice`（TTS 桥 `/v1/voice` 热切换音色）。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from voice_realtime.config import BridgeSettings

logger = logging.getLogger(__name__)

_SUPPORTED_COMMANDS = frozenset(
    {"clear_context", "stop_session", "restart", "set_persona", "set_voice"}
)


class ControlBridge:
    """控制命令分发器（每个 WS 连接一个实例，共享 runtime）。"""

    def __init__(self, runtime: Any, bridge: BridgeSettings) -> None:
        self._runtime = runtime
        self._bridge = bridge

    async def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        """执行一条控制指令，返回 {ok, cmd[, error]}。"""
        cmd = payload.get("cmd")
        if not isinstance(cmd, str) or cmd not in _SUPPORTED_COMMANDS:
            return {"ok": False, "cmd": cmd, "error": f"未知命令: {cmd!r}"}
        try:
            await self._dispatch(cmd, payload)
        except Exception as exc:
            logger.exception("ControlBridge: 命令 %s 执行失败", cmd)
            return {"ok": False, "cmd": cmd, "error": str(exc)}
        return {"ok": True, "cmd": cmd}

    async def _dispatch(self, cmd: str, payload: dict[str, Any]) -> None:
        if cmd == "clear_context":
            await self._runtime.clear_context()
        elif cmd == "stop_session":
            await self._runtime.stop_session()
        elif cmd == "restart":
            await self._runtime.restart_pipeline()
        elif cmd == "set_persona":
            prompt = payload.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError("set_persona 需要非空 prompt")
            self._runtime.set_persona(prompt.strip())
            await self._runtime.clear_context()
        elif cmd == "set_voice":
            await self._set_voice(payload)

    async def _set_voice(self, payload: dict[str, Any]) -> None:
        voice = payload.get("voice")
        if not isinstance(voice, str) or not voice.strip():
            raise ValueError("set_voice 需要非空 voice")
        url = f"http://{self._bridge.host}:{self._bridge.port}/v1/voice"
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json={"voice": voice.strip()})
            resp.raise_for_status()
