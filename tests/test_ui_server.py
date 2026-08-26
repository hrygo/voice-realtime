"""Voice Studio UI 服务基础测试。"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastapi import WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from voice_realtime.config import Settings, SubtitleSettings
from voice_realtime.meeting.models import PCMOwner, RuntimeMode, TranscriptWindow
from voice_realtime.ui import server as server_module
from voice_realtime.ui.assistant_bridge import StatusBridgeObserver
from voice_realtime.ui.protocol import DuplexMode, RuntimeStateSnapshot
from voice_realtime.ui.runtime_events import RuntimeStateBroadcaster
from voice_realtime.ui.server import _initialize_meeting_backend, create_app
from voice_realtime.ui.subtitle_proxy import SubtitleProxy, SubtitleProxyState


class _FakeRuntime:
    """轻量 runtime：真实 observer/proxy（本测试不触发 lifespan）。"""

    def __init__(self, *, mode: RuntimeMode = RuntimeMode.ASSISTANT) -> None:
        self.observer = StatusBridgeObserver()
        self.subtitle_proxy = SubtitleProxy(
            SubtitleSettings(host="127.0.0.1", port=9998)
        )
        self.start = AsyncMock()
        self.stop = AsyncMock()
        self.clear_context = AsyncMock()
        self.clear_subtitles = AsyncMock()
        self.stop_session = AsyncMock()
        self.restart_pipeline = AsyncMock()
        self.set_mic_muted = AsyncMock()
        self.start_subtitles = AsyncMock(side_effect=self._start_subtitles)
        self.stop_active_mode = AsyncMock(side_effect=self._stop_active_mode)
        self.send_text = AsyncMock()
        self.set_persona = Mock()
        self.set_duplex_mode = Mock()
        self.set_voice = Mock()
        owner = PCMOwner.NONE if mode is RuntimeMode.IDLE else PCMOwner(mode.value)
        self._state = RuntimeStateSnapshot(
            pipeline="running",
            subtitle="connected",
            mic_muted=False,
            persona=None,
            voice="default",
            duplex_mode=DuplexMode.SPEAKER_FOCUS,
            session_started_at="2026-08-21T00:00:00+00:00",
            mode=mode,
            pcm_owner=owner,
            runtime_revision=1,
        )
        self.runtime_events = RuntimeStateBroadcaster(self.snapshot)

    def snapshot(self) -> RuntimeStateSnapshot:
        return self._state

    def force_state(
        self,
        mode: RuntimeMode,
        owner: PCMOwner,
        revision: int,
    ) -> None:
        self._state = self._state.model_copy(
            update={
                "mode": mode,
                "pcm_owner": owner,
                "runtime_revision": revision,
            }
        )
        self.runtime_events.publish(self._state)

    async def _start_subtitles(self) -> None:
        self.force_state(
            RuntimeMode.SUBTITLES,
            PCMOwner.SUBTITLES,
            self._state.runtime_revision + 1,
        )

    async def _stop_active_mode(self) -> None:
        self.force_state(
            RuntimeMode.IDLE,
            PCMOwner.NONE,
            self._state.runtime_revision + 1,
        )


class _BlockingRuntime(_FakeRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.command_accepted = threading.Event()
        self.release_command = threading.Event()
        self.start_subtitles = AsyncMock(side_effect=self._blocking_start_subtitles)

    async def _blocking_start_subtitles(self) -> None:
        self.command_accepted.set()
        await asyncio.to_thread(self.release_command.wait)
        await self._start_subtitles()


class _RecordingWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.active_sends = 0
        self.max_active_sends = 0
        self.changed = asyncio.Condition()

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.active_sends += 1
        self.max_active_sends = max(self.max_active_sends, self.active_sends)
        try:
            await asyncio.sleep(0)
            self.messages.append(payload)
            async with self.changed:
                self.changed.notify_all()
        finally:
            self.active_sends -= 1

    async def wait_for_messages(self, count: int) -> None:
        async with self.changed:
            await self.changed.wait_for(lambda: len(self.messages) >= count)


class _HandshakeRecordingWebSocket:
    def __init__(self) -> None:
        self.events: list[tuple[str, int | None]] = []

    async def accept(self) -> None:
        self.events.append(("accept", None))

    async def close(self, *, code: int, reason: str) -> None:
        del reason
        self.events.append(("close", code))

    async def send_text(self, payload: str) -> None:
        del payload


def _settings() -> Settings:
    return Settings(
        bridge={"host": "127.0.0.1", "port": 9999},
        subtitles={"host": "127.0.0.1", "port": 9998},
        interaction={"llm_base_url": "http://127.0.0.1:9997/v1"},
    )


@contextmanager
def _running_client(runtime: _FakeRuntime) -> Iterator[TestClient]:
    application = create_app(_settings(), initialize_meeting=False)
    with (
        patch("voice_realtime.ui.server.UIRuntime", return_value=runtime),
        TestClient(
            application,
            raise_server_exceptions=False,
        ) as client,
    ):
        yield client


def _receive_ack_and_runtime_state(
    websocket: Any,
    request_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    messages = [websocket.receive_json(), websocket.receive_json()]
    ack = next(message for message in messages if message.get("request_id") == request_id)
    state = next(message for message in messages if message.get("event") == "runtime_state")
    return ack, state


@pytest.fixture()
def app() -> TestClient:
    """构造一个注入 mock 服务的测试客户端。"""
    mock_settings = Settings(
        bridge={"host": "127.0.0.1", "port": 9999},  # unreachable
        subtitles={"host": "127.0.0.1", "port": 9998},  # unreachable
        interaction={"llm_base_url": "http://127.0.0.1:9997/v1"},  # unreachable
        ui={"static_dir": Path("/nonexistent/dist")},  # 无 dist 时走 placeholder
    )
    app = create_app(mock_settings, initialize_meeting=False)
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
        assert "media-src 'self' blob: data:" in resp.headers["content-security-policy"]

    def test_meeting_api_allows_configured_loopback_frontend(self) -> None:
        application = create_app(Settings(), initialize_meeting=False)
        client = TestClient(application, raise_server_exceptions=False)
        response = client.options(
            "/api/v1/meetings",
            headers={
                "origin": "http://localhost:5173",
                "access-control-request-method": "GET",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://localhost:5173"

    def test_runtime_snapshot(self) -> None:
        app = create_app(Settings(), initialize_meeting=False)
        app.state.runtime = _FakeRuntime()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/runtime")
        assert resp.status_code == 200
        assert resp.json()["pipeline"] == "running"

    async def test_meeting_backend_lifecycle_is_injected(self) -> None:
        settings = Settings()
        app = create_app(settings, initialize_meeting=False)
        runtime = Mock()
        runtime.subtitle_proxy = Mock()
        runtime.configure_meeting = Mock()
        repository = Mock()
        repository.start = AsyncMock()
        repository.recover_stale = AsyncMock(return_value=0)
        repository.close = AsyncMock()
        journal = Mock()
        journal.replay = AsyncMock(return_value=0)
        summary_service = Mock()
        summary_service.start = AsyncMock()
        summary_service.stop = AsyncMock()
        meeting_session = Mock()

        with patch(
            "voice_realtime.ui.server.run_migrations", new_callable=AsyncMock
        ) as migrations, patch(
            "voice_realtime.ui.server.PostgresMeetingRepository",
            return_value=repository,
        ), patch(
            "voice_realtime.ui.server.RecoveryJournal", return_value=journal
        ), patch(
            "voice_realtime.ui.server.MeetingSummaryClient"
        ), patch(
            "voice_realtime.ui.server.MeetingSummaryService",
            return_value=summary_service,
        ), patch(
            "voice_realtime.ui.server.MeetingSession", return_value=meeting_session
        ):
            ready = await _initialize_meeting_backend(app, settings, runtime)

        assert ready
        migrations.assert_awaited_once_with(
            settings.meeting.database_url,
            schema=settings.meeting.schema_name,
        )
        repository.start.assert_awaited_once()
        journal.replay.assert_awaited_once_with(repository)
        repository.recover_stale.assert_awaited_once()
        runtime.configure_meeting.assert_called_once_with(meeting_session)
        summary_service.start.assert_awaited_once()
        assert app.state.meeting_repository is repository


class TestServices:
    def test_services_unreachable(self, app: TestClient) -> None:
        """探活失败时返回 200 + status=unreachable，不抛错。"""
        resp = app.get("/api/services")
        assert resp.status_code == 200
        data = resp.json()
        assert data["network_scope"] == "local"
        assert "services" in data
        names = {s["name"] for s in data["services"]}
        assert names == {"wlk", "tts", "lm"}
        for svc in data["services"]:
            assert svc["status"] in ("unreachable", "timeout", "error")
            assert svc["name"] in names
        assert data["diagnostics"] == {
            "audio_hub": {},
            "interaction": {},
            "subtitles": {},
            "tts": {},
            "last_transition": None,
        }

    @pytest.mark.parametrize(
        ("host", "expected_scope"),
        [
            ("127.0.0.1", "local"),
            ("localhost", "local"),
            ("192.168.1.20", "network"),
            ("0.0.0.0", "network"),
        ],
    )
    def test_network_scope_distinguishes_loopback_from_network_bindings(
        self,
        host: str,
        expected_scope: str,
    ) -> None:
        assert server_module._network_scope(host) == expected_scope

    def test_services_adds_runtime_workload_diagnostics(self) -> None:
        """HTTP 探活与 paused workload 独立，并复制五类运行时诊断。"""

        @dataclass(frozen=True, slots=True)
        class QueueDiagnostics:
            queued_chunks: int
            dropped_chunks: int

        runtime = _FakeRuntime(mode=RuntimeMode.ASSISTANT)
        runtime.diagnostics = Mock(  # type: ignore[attr-defined]
            return_value={
                "audio_hub": {
                    "interaction": QueueDiagnostics(
                        queued_chunks=2,
                        dropped_chunks=1,
                    )
                },
                "interaction": QueueDiagnostics(
                    queued_chunks=3,
                    dropped_chunks=4,
                ),
                "subtitles": runtime.subtitle_proxy.diagnostics(PCMOwner.ASSISTANT),
                "tts": {
                    "source_chunk_gaps_over_200ms": 0,
                    "pcm": b"must-not-leak",
                    "queue": asyncio.Queue(),
                },
                "last_transition": ("idle", "assistant"),
                "internal_pcm": b"must-not-leak",
            }
        )
        mock_resp = Mock(status_code=200)
        mock_resp.json.return_value = {
            "data": [{"id": _settings().interaction.llm_model}]
        }

        with (
            patch(
                "voice_realtime.ui.server.httpx.AsyncClient.get",
                new_callable=AsyncMock,
                return_value=mock_resp,
            ),
            _running_client(runtime) as client,
        ):
            response = client.get("/api/services")

        assert response.status_code == 200
        payload = response.json()
        services = {item["name"]: item for item in payload["services"]}
        assert set(services) == {"wlk", "tts", "lm"}
        assert services["wlk"] == {
            "name": "wlk",
            "status": "ok",
            "url": "http://127.0.0.1:9998/health",
            "workload": "paused",
            "ws_state": "paused",
            "reconnect_count": 0,
            "last_event_age_ms": None,
        }
        assert services["tts"] == {
            "name": "tts",
            "status": "ok",
            "url": "http://127.0.0.1:9999/health",
        }
        assert services["lm"]["status"] == "ok"
        assert services["lm"]["target_model"] == _settings().interaction.llm_model
        assert services["lm"]["model_present"] is True
        assert payload["diagnostics"] == {
            "audio_hub": {
                "interaction": {"queued_chunks": 2, "dropped_chunks": 1}
            },
            "interaction": {"queued_chunks": 3, "dropped_chunks": 4},
            "subtitles": {
                "workload": "paused",
                "ws_state": "paused",
                "reconnect_count": 0,
                "last_event_age_ms": None,
                "dropped_chunks": 0,
                "gap_count": 0,
            },
            "tts": {
                "source_chunk_gaps_over_200ms": 0,
                "pcm": None,
                "queue": None,
            },
            "last_transition": ["idle", "assistant"],
        }
        assert "must-not-leak" not in response.text

    def test_services_http_ok_reports_backoff_workload_as_degraded(self) -> None:
        runtime = _FakeRuntime(mode=RuntimeMode.SUBTITLES)
        runtime.subtitle_proxy._state = SubtitleProxyState.BACKOFF
        mock_resp = Mock(status_code=200)

        with (
            patch(
                "voice_realtime.ui.server.httpx.AsyncClient.get",
                new_callable=AsyncMock,
                return_value=mock_resp,
            ),
            _running_client(runtime) as client,
        ):
            response = client.get("/api/services")

        wlk = next(
            item for item in response.json()["services"] if item["name"] == "wlk"
        )
        assert wlk["status"] == "ok"
        assert wlk["workload"] == "degraded"
        assert wlk["ws_state"] == "backoff"

    def test_services_ready_workload_ignores_long_event_silence(self) -> None:
        runtime = _FakeRuntime(mode=RuntimeMode.SUBTITLES)
        proxy = runtime.subtitle_proxy
        proxy._state = SubtitleProxyState.CONNECTED
        proxy._browser_stream = object()
        proxy._browser_ready.set()
        proxy._last_event_at = proxy._clock() - 120.0
        mock_resp = Mock(status_code=200)

        with (
            patch(
                "voice_realtime.ui.server.httpx.AsyncClient.get",
                new_callable=AsyncMock,
                return_value=mock_resp,
            ),
            _running_client(runtime) as client,
        ):
            response = client.get("/api/services")

        wlk = next(
            item for item in response.json()["services"] if item["name"] == "wlk"
        )
        assert wlk["status"] == "ok"
        assert wlk["workload"] == "ready"
        assert wlk["ws_state"] == "connected"
        assert wlk["last_event_age_ms"] >= 120_000

    def test_services_runtime_diagnostics_failure_is_redacted(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        runtime = _FakeRuntime(mode=RuntimeMode.ASSISTANT)
        runtime.diagnostics = Mock(  # type: ignore[attr-defined]
            side_effect=RuntimeError("private upstream response")
        )
        runtime.subtitle_proxy.diagnostics = Mock(
            side_effect=ValueError("private PCM content")
        )
        mock_resp = Mock(status_code=200)

        with (
            caplog.at_level(logging.WARNING),
            patch(
                "voice_realtime.ui.server.httpx.AsyncClient.get",
                new_callable=AsyncMock,
                return_value=mock_resp,
            ),
            _running_client(runtime) as client,
        ):
            response = client.get("/api/services")

        payload = response.json()
        assert {item["name"] for item in payload["services"]} == {
            "wlk",
            "tts",
            "lm",
        }
        wlk = next(item for item in payload["services"] if item["name"] == "wlk")
        assert set(wlk) == {"name", "status", "url"}
        assert payload["diagnostics"] == {
            "audio_hub": {},
            "interaction": {},
            "subtitles": {},
            "tts": {},
            "last_transition": None,
        }
        assert "RuntimeError" in caplog.text
        assert "ValueError" in caplog.text
        assert "private upstream response" not in caplog.text
        assert "private PCM content" not in caplog.text

    def test_services_probes_remain_concurrent(self) -> None:
        active = 0
        peak = 0
        all_started = asyncio.Event()

        async def concurrent_get(_url: str) -> Mock:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 3:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=1.0)
            active -= 1
            return Mock(status_code=200)

        application = create_app(_settings(), initialize_meeting=False)
        with (
            patch(
                "voice_realtime.ui.server.httpx.AsyncClient.get",
                new_callable=AsyncMock,
                side_effect=concurrent_get,
            ),
            TestClient(application, raise_server_exceptions=False) as client,
        ):
            response = client.get("/api/services")

        assert response.status_code == 200
        assert peak == 3

    def test_services_one_ok(self) -> None:
        """模拟 httpx.AsyncClient.get 返回 200 时返回 status=ok。"""
        mock_settings = Settings(
            bridge={"host": "127.0.0.1", "port": 9999},
            subtitles={"host": "127.0.0.1", "port": 9999},
            interaction={"llm_base_url": "http://127.0.0.1:9997/v1"},
        )
        app = create_app(mock_settings, initialize_meeting=False)

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
                payload = resp.json()
                for svc in payload["services"]:
                    assert svc["status"] == "ok"
                    assert {"name", "status", "url"} <= set(svc)
                assert payload["diagnostics"] == {
                    "audio_hub": {},
                    "interaction": {},
                    "subtitles": {},
                    "tts": {},
                    "last_transition": None,
                }


class TestVoices:
    def test_voices_proxies_from_bridge(self) -> None:
        """/v1/voices 代理 TTS 桥；桥返回音色列表时原样透传。"""
        mock_settings = Settings(
            bridge={"host": "127.0.0.1", "port": 9999},
            subtitles={"host": "127.0.0.1", "port": 9999},
            interaction={"llm_base_url": "http://127.0.0.1:9997/v1"},
        )
        app = create_app(mock_settings, initialize_meeting=False)

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
        app = create_app(mock_settings, initialize_meeting=False)

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
        app = create_app(mock_settings, initialize_meeting=False)
        with patch("voice_realtime.ui.server.UIRuntime") as fake_cls:
            fake_cls.return_value.start = AsyncMock()
            fake_cls.return_value.stop = AsyncMock()
            with TestClient(app) as client:
                resp = client.get("/")
                assert resp.status_code == 200
                assert "Voice Studio" in resp.text


class TestWebSocketGateways:
    """WS 事件网关：连接注册到 observer/proxy，断开移除；未就绪时关闭。"""

    def _app_with_runtime(
        self,
        *,
        allowed_origins: list[str] | None = None,
    ) -> TestClient:
        meeting = (
            {"allowed_origins": allowed_origins}
            if allowed_origins is not None
            else {}
        )
        mock_settings = Settings(
            bridge={"host": "127.0.0.1", "port": 9999},
            subtitles={"host": "127.0.0.1", "port": 9998},
            interaction={"llm_base_url": "http://127.0.0.1:9997/v1"},
            meeting=meeting,
        )
        app = create_app(mock_settings, initialize_meeting=False)
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
        client.app.state.runtime.force_state(
            RuntimeMode.SUBTITLES,
            PCMOwner.SUBTITLES,
            2,
        )
        with client.websocket_connect("/ws/subtitles"):
            assert client.app.state.runtime.subtitle_proxy.has_clients
        assert not client.app.state.runtime.subtitle_proxy.has_clients

    def test_ws_closed_when_runtime_unavailable(self) -> None:
        """runtime 未装配（测试直连/lifespan 未跑）时拒绝连接。"""
        client = TestClient(
            create_app(Settings(), initialize_meeting=False),
            raise_server_exceptions=False,
        )
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
        client = self._app_with_runtime(allowed_origins=[])
        with client.websocket_connect(
            "/ws/assistant",
            headers={
                "host": "localhost:8100",
                "origin": "http://localhost:5173",
            },
        ):
            assert client.app.state.runtime.observer.has_clients

    @pytest.mark.parametrize(
        "origin",
        [
            "http://localhost:8100",
            "http://localhost:5173",
            "http://127.0.0.1:8100",
            "http://127.0.0.1:5173",
            "http://[::1]:8100",
            "http://[::1]:5173",
        ],
    )
    def test_lan_host_rejects_implicit_loopback_origin(self, origin: str) -> None:
        client = self._app_with_runtime()
        with pytest.raises(WebSocketDisconnect) as caught, client.websocket_connect(
            "/ws/assistant",
            headers={"host": "192.168.1.10:8100", "origin": origin},
        ) as websocket:
            websocket.close()

        assert caught.value.code == 1008
        assert caught.value.reason == "Origin 不受信任"

    @pytest.mark.parametrize(
        ("request_host", "origin"),
        [
            ("192.168.1.10:8100", "http://192.168.1.100:8100"),
            ("192.168.1.10:8100", "http://192.168.1.100:5173"),
            ("192.168.1.10:8100", "http://192.168.1.10:5173"),
            ("[fd00::10]:8100", "http://[fd00::100]:8100"),
        ],
    )
    def test_other_private_origin_is_rejected(
        self,
        request_host: str,
        origin: str,
    ) -> None:
        client = self._app_with_runtime(allowed_origins=[])
        with pytest.raises(WebSocketDisconnect) as caught, client.websocket_connect(
            "/ws/assistant",
            headers={"host": request_host, "origin": origin},
        ) as websocket:
            websocket.close()

        assert caught.value.code == 1008
        assert caught.value.reason == "Origin 不受信任"

    @pytest.mark.parametrize(
        ("request_host", "origin"),
        [
            ("192.168.1.10:8100", "http://192.168.1.10:8100"),
            ("[fd00::10]:8100", "http://[fd00::10]:8100"),
        ],
    )
    def test_lan_same_origin_is_allowed(
        self,
        request_host: str,
        origin: str,
    ) -> None:
        client = self._app_with_runtime(allowed_origins=[])
        with client.websocket_connect(
            "/ws/assistant",
            headers={"host": request_host, "origin": origin},
        ):
            assert client.app.state.runtime.observer.has_clients

    @pytest.mark.parametrize(
        "origin",
        ["http://192.168.1.100:5173", "http://localhost:5173"],
    )
    def test_explicit_origin_is_allowed(self, origin: str) -> None:
        client = self._app_with_runtime(
            allowed_origins=[origin],
        )
        with client.websocket_connect(
            "/ws/assistant",
            headers={"host": "192.168.1.10:8100", "origin": origin},
        ):
            assert client.app.state.runtime.observer.has_clients

    def test_forwarded_host_does_not_override_request_host(self) -> None:
        client = self._app_with_runtime(
            allowed_origins=[],
        )
        with pytest.raises(WebSocketDisconnect) as caught, client.websocket_connect(
            "/ws/assistant",
            headers={
                "host": "192.168.1.10:8100",
                "origin": "http://192.168.1.100:8100",
                "x-forwarded-host": "192.168.1.100:8100",
            },
        ) as websocket:
            websocket.close()

        assert caught.value.code == 1008


class TestCommandGateway:
    """/ws/assistant/cmd 控制面：指令执行 + 响应回传。"""

    def _app_with_runtime(self) -> TestClient:
        mock_settings = Settings(
            bridge={"host": "127.0.0.1", "port": 9999},
            subtitles={"host": "127.0.0.1", "port": 9998},
            interaction={"llm_base_url": "http://127.0.0.1:9997/v1"},
        )
        app = create_app(mock_settings, initialize_meeting=False)
        app.state.runtime = _FakeRuntime()
        return TestClient(app, raise_server_exceptions=False)

    def test_clear_context_command_executed(self) -> None:
        """/ws/assistant/cmd：clear_context 委托 runtime 并回执 ok。"""
        client = self._app_with_runtime()
        with client.websocket_connect("/ws/assistant/cmd") as ws:
            handshake = ws.receive_json()
            assert handshake["event"] == "runtime_state"
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
        client = TestClient(
            create_app(Settings(), initialize_meeting=False),
            raise_server_exceptions=False,
        )
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(
            "/ws/assistant/cmd"
        ) as ws:
            ws.receive_text()


class TestMeetingV1Gateway:
    def test_meeting_socket_sends_contract_snapshot(self) -> None:
        app = create_app(Settings(), initialize_meeting=False)
        app.state.runtime = _FakeRuntime()
        client = TestClient(app, raise_server_exceptions=False)
        with client.websocket_connect("/ws/v1/meetings") as ws:
            event = ws.receive_json()
        assert event["contract_version"] == "1"
        assert event["type"] == "meeting_snapshot"
        assert "meeting" in event["payload"]

    def test_idle_meeting_snapshot_uses_nil_meeting_and_null_partial(self) -> None:
        app = create_app(Settings(), initialize_meeting=False)
        app.state.runtime = _FakeRuntime(mode=RuntimeMode.IDLE)
        client = TestClient(app, raise_server_exceptions=False)

        with client.websocket_connect("/ws/v1/meetings") as ws:
            event = ws.receive_json()

        assert event["meeting_id"] == ""
        assert event["payload"]["meeting"]["id"] == "00000000-0000-4000-8000-000000000000"
        assert event["payload"]["partial"] is None

    def test_snapshot_partial_serializes_known_speaker_without_guessing_unknown(self) -> None:
        known = server_module._partial_snapshot_json(
            TranscriptWindow(
                source_epoch=1,
                partial="正在说",
                partial_speaker_key="epoch:1:speaker:2",
            )
        )
        unknown = server_module._partial_snapshot_json(
            TranscriptWindow(source_epoch=1, partial="尚未分人")
        )

        assert known == {
            "text": "正在说",
            "speaker_key": "epoch:1:speaker:2",
            "speaker_name": "说话人 2",
        }
        assert unknown == {"text": "尚未分人", "speaker_key": None, "speaker_name": None}
        opaque = server_module._partial_snapshot_json(
            TranscriptWindow(
                source_epoch=1,
                partial="尚未命名",
                partial_speaker_key="opaque-internal-key",
            )
        )

        assert opaque == {
            "text": "尚未命名",
            "speaker_key": "opaque-internal-key",
            "speaker_name": None,
        }

    def test_control_socket_uses_v1_envelope(self) -> None:
        app = create_app(Settings(), initialize_meeting=False)
        app.state.runtime = _FakeRuntime()
        client = TestClient(app, raise_server_exceptions=False)
        with client.websocket_connect("/ws/v1/control") as ws:
            handshake = ws.receive_json()
            assert handshake["contract_version"] == "1"
            ws.send_json({"contract_version": "1", "request_id": "r1", "cmd": "start_meeting"})
            response = ws.receive_json()
        assert response["contract_version"] == "1"
        assert response["request_id"] == "r1"
        assert response["ok"] is False

    def test_control_socket_rejects_unknown_fields(self) -> None:
        app = create_app(Settings(), initialize_meeting=False)
        app.state.runtime = _FakeRuntime()
        client = TestClient(app, raise_server_exceptions=False)
        with client.websocket_connect("/ws/v1/control") as ws:
            ws.receive_json()
            ws.send_json(
                {
                    "contract_version": "1",
                    "request_id": "r2",
                    "cmd": "start_meeting",
                    "unexpected": True,
                }
            )
            response = ws.receive_json()
        assert response["ok"] is False
        assert response["error"]["code"] == "invalid_payload"


class TestRuntimeControlBroadcast:
    def test_control_routes_share_only_control_bridge_dispatch(self) -> None:
        route_source = inspect.getsource(server_module._mount_websocket_routes)
        handler_source = inspect.getsource(server_module._serve_control_websocket)

        assert route_source.count(
            "await _serve_control_websocket(websocket, runtime, cfg)"
        ) == 2
        assert handler_source.count("ControlBridge(runtime, cfg.bridge)") == 1
        assert not hasattr(server_module, "_handle_v1_control")

    def test_v1_control_broadcasts_to_all_and_acks_only_requester(self) -> None:
        runtime = _FakeRuntime()
        with (
            _running_client(runtime) as client,
            client.websocket_connect("/ws/v1/control") as requesting,
            client.websocket_connect("/ws/v1/control") as observing,
        ):
            assert len(runtime.runtime_events._clients) == 2
            request_handshake = requesting.receive_json()
            observer_handshake = observing.receive_json()
            assert request_handshake["event"] == "runtime_state"
            assert observer_handshake["event"] == "runtime_state"
            assert request_handshake["state"] == runtime.snapshot().model_dump(mode="json")
            assert observer_handshake["state"] == runtime.snapshot().model_dump(mode="json")
            assert request_handshake["state"]["runtime_revision"] == 1
            assert observer_handshake["state"]["runtime_revision"] == 1

            requesting.send_json(
                {
                    "contract_version": "1",
                    "request_id": "subtitles-1",
                    "cmd": "start_subtitles",
                }
            )
            ack, request_broadcast = _receive_ack_and_runtime_state(
                requesting,
                "subtitles-1",
            )
            observer_broadcast = observing.receive_json()

        assert ack["ok"] is True
        assert ack["state"]["runtime_revision"] == 2
        assert request_broadcast["state"]["runtime_revision"] == 2
        assert observer_broadcast["event"] == "runtime_state"
        assert observer_broadcast["state"]["runtime_revision"] == 2
        assert "request_id" not in observer_broadcast
        assert not runtime.runtime_events._clients

    def test_legacy_control_receives_continuous_runtime_broadcasts(self) -> None:
        runtime = _FakeRuntime()
        with (
            _running_client(runtime) as client,
            client.websocket_connect("/ws/assistant/cmd") as websocket,
        ):
            assert len(runtime.runtime_events._clients) == 1
            handshake = websocket.receive_json()
            assert handshake["event"] == "runtime_state"

            assert client.portal is not None
            client.portal.call(
                runtime.force_state,
                RuntimeMode.SUBTITLES,
                PCMOwner.SUBTITLES,
                2,
            )
            broadcast = websocket.receive_json()

        assert broadcast["event"] == "runtime_state"
        assert broadcast["state"]["runtime_revision"] == 2
        assert not runtime.runtime_events._clients

    def test_accepted_command_survives_request_socket_disconnect(self) -> None:
        runtime = _BlockingRuntime()
        try:
            with (
                _running_client(runtime) as client,
                client.websocket_connect("/ws/v1/control") as observing,
                client.websocket_connect("/ws/v1/control") as requesting,
            ):
                observing.receive_json()
                requesting.receive_json()
                requesting.send_json(
                    {
                        "contract_version": "1",
                        "request_id": "blocking-1",
                        "cmd": "start_subtitles",
                    }
                )
                assert runtime.command_accepted.wait(timeout=1.0)
                if not hasattr(client.app.state, "accepted_control_tasks"):
                    runtime.release_command.set()
                assert client.app.state.accepted_control_tasks

                requesting.close()
                runtime.release_command.set()
                committed = observing.receive_json()
        finally:
            runtime.release_command.set()

        assert committed["event"] == "runtime_state"
        assert committed["state"]["mode"] == "subtitles"
        assert committed["state"]["runtime_revision"] == 2
        assert not runtime.runtime_events._clients
        assert not client.app.state.accepted_control_tasks

    async def test_control_sender_is_single_writer_and_cleans_pending_gets(self) -> None:
        runtime = _FakeRuntime()
        runtime_client = runtime.runtime_events.add_client()
        responses: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=8)
        websocket = _RecordingWebSocket()
        sender = asyncio.create_task(
            server_module._send_control_messages(websocket, responses, runtime_client)
        )
        await asyncio.wait_for(websocket.wait_for_messages(1), timeout=1.0)

        responses.put_nowait({"request_id": "r1", "ok": True})
        runtime.force_state(RuntimeMode.SUBTITLES, PCMOwner.SUBTITLES, 2)
        await asyncio.wait_for(websocket.wait_for_messages(3), timeout=1.0)

        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender

        pending_response = {"request_id": "after-cancel", "ok": True}
        responses.put_nowait(pending_response)
        runtime.force_state(RuntimeMode.MEETING, PCMOwner.MEETING, 3)

        assert responses.get_nowait() == pending_response
        assert runtime_client.latest_nowait().runtime_revision == 3
        assert websocket.max_active_sends == 1

    @pytest.mark.parametrize(
        "payload",
        [
            {"cmd": "clear_context"},
            {"request_id": "   ", "cmd": "clear_context"},
            {"request_id": "x" * 65, "cmd": "clear_context"},
        ],
    )
    def test_control_socket_strictly_validates_request_id(
        self,
        payload: dict[str, Any],
    ) -> None:
        runtime = _FakeRuntime()
        with (
            _running_client(runtime) as client,
            client.websocket_connect("/ws/v1/control") as websocket,
        ):
            websocket.receive_json()
            websocket.send_json(payload)
            response = websocket.receive_json()

        assert response["ok"] is False
        assert response["error_code"] == "invalid_payload"
        assert response["error"]["code"] == "invalid_payload"
        assert response["error"]["message"] == "控制命令无效"

    @pytest.mark.parametrize(
        "origin",
        ["https://evil.example", "http://localhost:9999"],
    )
    def test_control_socket_rejects_untrusted_origin_host(self, origin: str) -> None:
        runtime = _FakeRuntime()
        with (
            _running_client(runtime) as client,
            pytest.raises(WebSocketDisconnect) as caught,
            client.websocket_connect(
                "/ws/v1/control",
                headers={"origin": origin},
            ) as websocket,
        ):
            websocket.receive_text()

        assert caught.value.code == 1008
        assert caught.value.reason == "Origin 不受信任"

    def test_accepted_task_error_log_redacts_exception_body(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        runtime = _FakeRuntime()
        secret = "external-payload-must-not-leak"
        with (
            caplog.at_level(logging.ERROR, logger="voice_realtime.ui.server"),
            patch(
                "voice_realtime.ui.server.ControlBridge.handle",
                new_callable=AsyncMock,
                side_effect=RuntimeError(secret),
            ),
            _running_client(runtime) as client,
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/ws/v1/control") as websocket,
        ):
            websocket.receive_json()
            websocket.send_json({"request_id": "error-1", "cmd": "clear_context"})
            websocket.receive_json()

        assert "RuntimeError" in caplog.text
        assert "command_failed" in caplog.text
        assert secret not in caplog.text


class TestSubtitleEligibility:
    async def test_ineligible_socket_completes_handshake_before_stable_close(self) -> None:
        runtime = _FakeRuntime(mode=RuntimeMode.ASSISTANT)
        websocket = _HandshakeRecordingWebSocket()

        await server_module._serve_subtitle_websocket(websocket, runtime)  # type: ignore[arg-type]

        assert websocket.events == [("accept", None), ("close", 4409)]
        assert not runtime.runtime_events._clients

    @pytest.mark.parametrize(
        ("mode", "owner"),
        [
            (RuntimeMode.ASSISTANT, PCMOwner.ASSISTANT),
            (RuntimeMode.IDLE, PCMOwner.NONE),
        ],
    )
    def test_subtitles_socket_rejected_when_mode_is_ineligible(
        self,
        mode: RuntimeMode,
        owner: PCMOwner,
    ) -> None:
        runtime = _FakeRuntime(mode=mode)
        runtime.force_state(mode, owner, 2)
        with (
            _running_client(runtime) as client,
            pytest.raises(WebSocketDisconnect) as caught,
            client.websocket_connect("/ws/subtitles") as websocket,
        ):
            assert not runtime.subtitle_proxy.has_clients
            websocket.receive_text()

        assert caught.value.code == 4409
        assert caught.value.reason == "字幕模式未激活"
        assert not runtime.subtitle_proxy.has_clients
        assert not runtime.runtime_events._clients

    def test_existing_subtitle_socket_is_revoked_on_idle(self) -> None:
        runtime = _FakeRuntime(mode=RuntimeMode.SUBTITLES)
        with (
            _running_client(runtime) as client,
            client.websocket_connect("/ws/subtitles") as websocket,
        ):
            assert runtime.subtitle_proxy.has_clients
            assert client.portal is not None
            client.portal.call(
                runtime.force_state,
                RuntimeMode.IDLE,
                PCMOwner.NONE,
                2,
            )
            with pytest.raises(WebSocketDisconnect) as caught:
                websocket.receive_text()

        assert caught.value.code == 4409
        assert caught.value.reason == "字幕模式未激活"
        assert not runtime.subtitle_proxy.has_clients
        assert not runtime.runtime_events._clients

    def test_subtitles_to_meeting_stays_then_meeting_to_idle_revokes(self) -> None:
        runtime = _FakeRuntime(mode=RuntimeMode.SUBTITLES)
        with (
            _running_client(runtime) as client,
            client.websocket_connect("/ws/subtitles") as websocket,
        ):
            assert client.portal is not None
            client.portal.call(
                runtime.force_state,
                RuntimeMode.MEETING,
                PCMOwner.MEETING,
                2,
            )
            websocket.send_json({"request_id": "must-not-route", "cmd": "start_subtitles"})
            client.portal.call(
                runtime.subtitle_proxy._broadcast_untracked,
                {"event": "subtitle", "text": "会议字幕"},
            )
            assert websocket.receive_json() == {"event": "subtitle", "text": "会议字幕"}
            runtime.start_subtitles.assert_not_awaited()

            client.portal.call(
                runtime.force_state,
                RuntimeMode.IDLE,
                PCMOwner.NONE,
                3,
            )
            with pytest.raises(WebSocketDisconnect) as caught:
                websocket.receive_text()

        assert caught.value.code == 4409
        assert not runtime.subtitle_proxy.has_clients
        assert not runtime.runtime_events._clients

    def test_mode_change_between_initial_and_second_check_never_registers_proxy(
        self,
    ) -> None:
        runtime = _FakeRuntime(mode=RuntimeMode.SUBTITLES)
        original_accept = WebSocket.accept
        runtime.subtitle_proxy.add_client = Mock(wraps=runtime.subtitle_proxy.add_client)

        async def accept_then_change_mode(
            websocket: WebSocket,
            subprotocol: str | None = None,
            headers: list[tuple[bytes, bytes]] | None = None,
        ) -> None:
            await original_accept(websocket, subprotocol=subprotocol, headers=headers)
            runtime.force_state(RuntimeMode.IDLE, PCMOwner.NONE, 2)

        with (
            patch.object(WebSocket, "accept", new=accept_then_change_mode),
            _running_client(runtime) as client,
            pytest.raises(WebSocketDisconnect) as caught,
            client.websocket_connect("/ws/subtitles") as websocket,
        ):
            websocket.receive_text()

        assert caught.value.code == 4409
        runtime.subtitle_proxy.add_client.assert_not_called()
        assert not runtime.subtitle_proxy.has_clients
        assert not runtime.runtime_events._clients
