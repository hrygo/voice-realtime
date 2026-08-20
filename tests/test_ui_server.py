"""Voice Studio UI 服务基础测试。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from voice_realtime.config import Settings, SubtitleSettings
from voice_realtime.ui.assistant_bridge import StatusBridgeObserver
from voice_realtime.ui.protocol import DuplexMode, RuntimeStateSnapshot
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
        self.restart_pipeline = AsyncMock()
        self.set_mic_muted = AsyncMock()
        self.set_persona = Mock()
        self.set_duplex_mode = Mock()
        self.set_voice = Mock()

    def snapshot(self) -> RuntimeStateSnapshot:
        return RuntimeStateSnapshot(
            pipeline="running",
            subtitle="connected",
            mic_muted=False,
            persona=None,
            voice="default",
            duplex_mode=DuplexMode.SPEAKER_FOCUS,
            session_started_at="2026-08-21T00:00:00+00:00",
        )


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

    def test_security_headers(self, app: TestClient) -> None:
        resp = app.get("/health")
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["x-frame-options"] == "DENY"
        assert "connect-src" in resp.headers["content-security-policy"]

    def test_runtime_snapshot(self) -> None:
        app = create_app(Settings())
        app.state.runtime = _FakeRuntime()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/runtime")
        assert resp.status_code == 200
        assert resp.json()["pipeline"] == "running"


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
        """模拟 httpx.AsyncClient.get 返回 200 时返回 status=ok。"""
        mock_settings = Settings(
            bridge={"host": "127.0.0.1", "port": 9999},
            subtitles={"host": "127.0.0.1", "port": 9999},
            interaction={"llm_base_url": "http://127.0.0.1:9997/v1"},
        )
        app = create_app(mock_settings)

        mock_resp = Mock()
        mock_resp.status_code = 200

        with patch(
            "voice_realtime.ui.server.UIRuntime"
        ) as fake_cls, patch(
            "voice_realtime.ui.server.httpx.AsyncClient.get",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            fake_cls.return_value.start = AsyncMock()
            fake_cls.return_value.stop = AsyncMock()
            with TestClient(app) as client:
                resp = client.get("/api/services")
                assert resp.status_code == 200
                for svc in resp.json()["services"]:
                    assert svc["status"] == "ok"


class TestVoices:
    def test_voices_proxies_from_bridge(self) -> None:
        """/v1/voices 代理 TTS 桥；桥返回音色列表时原样透传。"""
        mock_settings = Settings(
            bridge={"host": "127.0.0.1", "port": 9999},
            subtitles={"host": "127.0.0.1", "port": 9999},
            interaction={"llm_base_url": "http://127.0.0.1:9997/v1"},
        )
        app = create_app(mock_settings)

        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"voice": "default", "available": ["default", "warm"]}

        with patch("voice_realtime.ui.server.UIRuntime") as fake_cls, patch(
            "voice_realtime.ui.server.httpx.AsyncClient.get",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            fake_cls.return_value.start = AsyncMock()
            fake_cls.return_value.stop = AsyncMock()
            with TestClient(app) as client:
                resp = client.get("/v1/voices")
                assert resp.status_code == 200
                assert resp.json()["available"] == ["default", "warm"]

    def test_voices_bridge_down_returns_502(self) -> None:
        """桥不可达时返回 502，不抛未处理异常。"""
        mock_settings = Settings(
            bridge={"host": "127.0.0.1", "port": 9999},
            subtitles={"host": "127.0.0.1", "port": 9999},
            interaction={"llm_base_url": "http://127.0.0.1:9997/v1"},
        )
        app = create_app(mock_settings)

        with patch("voice_realtime.ui.server.UIRuntime") as fake_cls, patch(
            "voice_realtime.ui.server.httpx.AsyncClient.get",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("refused"),
        ):
            fake_cls.return_value.start = AsyncMock()
            fake_cls.return_value.stop = AsyncMock()
            with TestClient(app) as client:
                resp = client.get("/v1/voices")
                assert resp.status_code == 502



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
        with patch("voice_realtime.ui.server.UIRuntime") as fake_cls:
            fake_cls.return_value.start = AsyncMock()
            fake_cls.return_value.stop = AsyncMock()
            with TestClient(app) as client:
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

    def test_malicious_origin_is_rejected(self) -> None:
        client = self._app_with_runtime()
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(
            "/ws/assistant", headers={"origin": "https://evil.example"}
        ) as ws:
            ws.receive_text()

    def test_vite_origin_is_allowed(self) -> None:
        client = self._app_with_runtime()
        with client.websocket_connect(
            "/ws/assistant", headers={"origin": "http://localhost:5173"}
        ):
            assert client.app.state.runtime.observer.has_clients


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
            handshake = ws.receive_json()
            assert handshake["event"] == "state"
            ws.send_json({"request_id": "1", "cmd": "clear_context"})
            resp = ws.receive_json()
        assert resp["ok"] is True
        assert resp["request_id"] == "1"
        assert resp["state"]["pipeline"] == "running"
        client.app.state.runtime.clear_context.assert_awaited_once()

    def test_unknown_command_returns_error(self) -> None:
        client = self._app_with_runtime()
        with client.websocket_connect("/ws/assistant/cmd") as ws:
            ws.receive_json()
            ws.send_json({"request_id": "1", "cmd": "self_destruct"})
            resp = ws.receive_json()
        assert resp["ok"] is False
        assert resp["error_code"] == "invalid_command"

    def test_invalid_json_returns_error(self) -> None:
        client = self._app_with_runtime()
        with client.websocket_connect("/ws/assistant/cmd") as ws:
            ws.receive_json()
            ws.send_text("not json")
            resp = ws.receive_json()
        assert resp["ok"] is False
        assert resp["error_code"] == "invalid_payload"

    def test_cmd_closed_when_runtime_unavailable(self) -> None:
        client = TestClient(create_app(Settings()), raise_server_exceptions=False)
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(
            "/ws/assistant/cmd"
        ) as ws:
            ws.receive_text()
