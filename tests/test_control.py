"""ControlBridge 单元测试：命令分发、参数校验、TTS 桥调用。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from voice_realtime.config import BridgeSettings
from voice_realtime.ui.control import ControlBridge


@pytest.fixture()
def runtime() -> MagicMock:
    """共享 fake runtime（异步命令方法为 AsyncMock，同步方法为 MagicMock）。"""
    rt = MagicMock()
    rt.clear_context = AsyncMock()
    rt.stop_session = AsyncMock()
    rt.restart_pipeline = AsyncMock()
    rt.set_persona = MagicMock()
    return rt


@pytest.fixture()
def bridge(runtime: MagicMock) -> ControlBridge:
    return ControlBridge(runtime, BridgeSettings())


class TestDispatch:
    async def test_unknown_command_rejected(self, bridge: ControlBridge) -> None:
        resp = await bridge.handle({"cmd": "self_destruct"})
        assert resp["ok"] is False
        assert "未知命令" in resp["error"]

    async def test_unknown_cmd_type_rejected(self, bridge: ControlBridge) -> None:
        resp = await bridge.handle({"cmd": 42})
        assert resp["ok"] is False

    async def test_clear_context_delegates(self, runtime: MagicMock, bridge: ControlBridge) -> None:
        resp = await bridge.handle({"cmd": "clear_context"})
        assert resp == {"ok": True, "cmd": "clear_context"}
        runtime.clear_context.assert_awaited_once()

    async def test_stop_session_delegates(self, runtime: MagicMock, bridge: ControlBridge) -> None:
        resp = await bridge.handle({"cmd": "stop_session"})
        assert resp["ok"] is True
        runtime.stop_session.assert_awaited_once()

    async def test_restart_delegates(self, runtime: MagicMock, bridge: ControlBridge) -> None:
        resp = await bridge.handle({"cmd": "restart"})
        assert resp["ok"] is True
        runtime.restart_pipeline.assert_awaited_once()

    async def test_set_persona_requires_prompt(self, bridge: ControlBridge) -> None:
        resp = await bridge.handle({"cmd": "set_persona"})
        assert resp["ok"] is False
        resp = await bridge.handle({"cmd": "set_persona", "prompt": "  "})
        assert resp["ok"] is False

    async def test_set_persona_sets_then_clears(
        self, runtime: MagicMock, bridge: ControlBridge
    ) -> None:
        resp = await bridge.handle({"cmd": "set_persona", "prompt": "  你是孔子  "})
        assert resp["ok"] is True
        runtime.set_persona.assert_called_once_with("你是孔子")
        runtime.clear_context.assert_awaited_once()

    async def test_execution_error_returns_failure(
        self, runtime: MagicMock, bridge: ControlBridge
    ) -> None:
        runtime.clear_context = AsyncMock(side_effect=RuntimeError("boom"))
        resp = await bridge.handle({"cmd": "clear_context"})
        assert resp["ok"] is False
        assert "boom" in resp["error"]


class TestSetVoice:
    def _http_client_mock(self) -> AsyncMock:
        """构造可 async with 的 httpx.AsyncClient mock（__aenter__ 返回自身）。"""
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock()
        return client

    async def test_set_voice_posts_to_bridge(
        self, runtime: MagicMock, bridge: ControlBridge
    ) -> None:
        client = self._http_client_mock()
        client.post.return_value.raise_for_status = MagicMock()
        with patch("voice_realtime.ui.control.httpx.AsyncClient", return_value=client) as cls:
            resp = await bridge.handle({"cmd": "set_voice", "voice": "warm"})
        assert resp["ok"] is True
        cls.assert_called_once()
        assert cls.call_args.kwargs["timeout"] == 5.0
        client.post.assert_awaited_once_with(
            "http://127.0.0.1:8765/v1/voice", json={"voice": "warm"}
        )

    async def test_set_voice_requires_value(self, bridge: ControlBridge) -> None:
        resp = await bridge.handle({"cmd": "set_voice"})
        assert resp["ok"] is False

    async def test_set_voice_bridge_error_propagates(
        self, runtime: MagicMock, bridge: ControlBridge
    ) -> None:
        client = self._http_client_mock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = RuntimeError("bridge down")
        client.post.return_value = mock_resp
        with patch("voice_realtime.ui.control.httpx.AsyncClient", return_value=client):
            resp = await bridge.handle({"cmd": "set_voice", "voice": "warm"})
        assert resp["ok"] is False
        assert "bridge down" in resp["error"]
