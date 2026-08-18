"""Voice Studio UI 服务基础测试。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from voice_realtime.config import Settings, SubtitleSettings
from voice_realtime.ui.assistant_bridge import StatusBridgeObserver
from voice_realtime.ui.server import create_app
from voice_realtime.ui.subtitle_proxy import SubtitleProxy


class _FakeRuntime:
    """轻量 runtime：真实 observer/proxy（本测试不触发 lifespan）。"""

    def __init__(self) -> None:
        self.observer = StatusBridgeObserver()
        self.subtitle_proxy = SubtitleProxy(
            SubtitleSettings(host="127.0.0.1", port=9998)
        )
        self.clear_context = AsyncMock()
        self.stop_session = AsyncMock()


@pytest.fixture()
def app() -> TestClient:
    """构造一个注入 mock 服务的测试客户端。"""
    mock_settings = Settings(
        bridge={"host": "127.0.0.1", "port": 9999},  # unreachable
        subtitles={"host": "127.0.0.1", "port": 9998},  # unreachable
        interaction={"llm_base_url": "http://127.0.0.1:9997/v1"},  # unreachable
        ui={"static_dir": Path("/nonexistent/dist")},  # 无 dist 时走 placeholder
    )
    app = create_app(mock_settings)
    return TestClient(app, raise_server_exceptions=False)


class TestHealth:
    def test_health_ok(self, app: TestClient) -> None:
        resp = app.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestServices:
    def test_services_unreachable(self, app: TestClient) -> None:
        """探活失败时返回 200 + status=unreachable，不抛错。"""
        resp = app.get("/api/services")
        assert resp.status_code == 200
        data = resp.json()
        assert "services" in data
        names = {s["name"] for s in data["services"]}
        assert names == {"wlk", "tts", "lm"}
        for svc in data["services"]:
            assert svc["status"] in ("unreachable", "timeout", "error")
            assert svc["name"] in names

    def test_services_one_ok(self) -> None:
        """模拟 httpx.get 返回 200 时返回 status=ok。"""
        mock_settings = Settings(
            bridge={"host": "127.0.0.1", "port": 9999},
            subtitles={"host": "127.0.0.1", "port": 9999},
            interaction={"llm_base_url": "http://127.0.0.1:9997/v1"},
        )
        app = create_app(mock_settings)

        mock_resp = Mock()
        mock_resp.status_code = 200

        with TestClient(app) as client, patch(
            "voice_realtime.ui.server.httpx.get", return_value=mock_resp
        ), patch(
            "voice_realtime.ui.server.UIRuntime"
        ) as fake_cls:
            fake_cls.return_value.start = AsyncMock()
            fake_cls.return_value.stop = AsyncMock()
            resp = client.get("/api/services")
            assert resp.status_code == 200
            for svc in resp.json()["services"]:
                assert svc["status"] == "ok"


class TestStaticMount:
    def test_placeholder_when_no_dist(self, app: TestClient) -> None:
        """没有 ui/dist 时返回 placeholder JSON。"""
        resp = app.get("/")
        assert resp.status_code == 200
        data = resp.json()
        assert "message" in data
        assert "未构建" in data["message"]

    def test_static_mount_when_dist_exists(self, tmp_path: Path) -> None:
        """有 ui/dist/index.html 时返回 HTML 内容。"""
        dist = tmp_path / "ui" / "dist"
        dist.mkdir(parents=True)
        (dist / "index.html").write_text("<html><body>Voice Studio</body></html>")

        mock_settings = Settings(
            bridge={"host": "127.0.0.1", "port": 9999},
            subtitles={"host": "127.0.0.1", "port": 9999},
            interaction={"llm_base_url": "http://127.0.0.1:9997/v1"},
            ui={"static_dir": dist},
        )
        app = create_app(mock_settings)
        with TestClient(app) as client, patch(
            "voice_realtime.ui.server.UIRuntime"
        ) as fake_cls:
            fake_cls.return_value.start = AsyncMock()
            fake_cls.return_value.stop = AsyncMock()
            resp = client.get("/")
            assert resp.status_code == 200
            assert "Voice Studio" in resp.text


class TestWebSocketGateways:
    """WS 事件网关：连接注册到 observer/proxy，断开移除；未就绪时关闭。"""

    def _app_with_runtime(self) -> TestClient:
        mock_settings = Settings(
            bridge={"host": "127.0.0.1", "port": 9999},
            subtitles={"host": "127.0.0.1", "port": 9998},
            interaction={"llm_base_url": "http://127.0.0.1:9997/v1"},
        )
        app = create_app(mock_settings)
        app.state.runtime = _FakeRuntime()
        return TestClient(app, raise_server_exceptions=False)

    def test_assistant_connection_registers_and_unregisters(self) -> None:
        """/ws/assistant：连接加入 observer 广播，断开移除。"""
        client = self._app_with_runtime()
        with client.websocket_connect("/ws/assistant"):
            assert client.app.state.runtime.observer.has_clients
        assert not client.app.state.runtime.observer.has_clients

    def test_subtitles_connection_registers_and_unregisters(self) -> None:
        """/ws/subtitles：连接加入 proxy 广播，断开移除。"""
        client = self._app_with_runtime()
        with client.websocket_connect("/ws/subtitles"):
            assert client.app.state.runtime.subtitle_proxy.has_clients
        assert not client.app.state.runtime.subtitle_proxy.has_clients

    def test_ws_closed_when_runtime_unavailable(self) -> None:
        """runtime 未装配（测试直连/lifespan 未跑）时拒绝连接。"""
        client = TestClient(create_app(Settings()), raise_server_exceptions=False)
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(
            "/ws/assistant"
        ) as ws:
            ws.receive_text()


class TestCommandGateway:
    """/ws/assistant/cmd 控制面：指令执行 + 响应回传。"""

    def _app_with_runtime(self) -> TestClient:
        mock_settings = Settings(
            bridge={"host": "127.0.0.1", "port": 9999},
            subtitles={"host": "127.0.0.1", "port": 9998},
            interaction={"llm_base_url": "http://127.0.0.1:9997/v1"},
        )
        app = create_app(mock_settings)
        app.state.runtime = _FakeRuntime()
        return TestClient(app, raise_server_exceptions=False)

    def test_clear_context_command_executed(self) -> None:
        """/ws/assistant/cmd：clear_context 委托 runtime 并回执 ok。"""
        client = self._app_with_runtime()
        with client.websocket_connect("/ws/assistant/cmd") as ws:
            ws.send_json({"cmd": "clear_context"})
            resp = ws.receive_json()
        assert resp == {"ok": True, "cmd": "clear_context"}
        client.app.state.runtime.clear_context.assert_awaited_once()

    def test_unknown_command_returns_error(self) -> None:
        client = self._app_with_runtime()
        with client.websocket_connect("/ws/assistant/cmd") as ws:
            ws.send_json({"cmd": "self_destruct"})
            resp = ws.receive_json()
        assert resp["ok"] is False
        assert "未知命令" in resp["error"]

    def test_invalid_json_returns_error(self) -> None:
        client = self._app_with_runtime()
        with client.websocket_connect("/ws/assistant/cmd") as ws:
            ws.send_text("not json")
            resp = ws.receive_json()
        assert resp["ok"] is False

    def test_cmd_closed_when_runtime_unavailable(self) -> None:
        client = TestClient(create_app(Settings()), raise_server_exceptions=False)
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(
            "/ws/assistant/cmd"
        ) as ws:
            ws.receive_text()
