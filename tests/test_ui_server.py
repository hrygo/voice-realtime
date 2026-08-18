"""Voice Studio UI 服务基础测试。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from voice_realtime.config import Settings
from voice_realtime.ui.server import create_app


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
        ):
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
        with TestClient(app) as client:
            resp = client.get("/")
            assert resp.status_code == 200
            assert "Voice Studio" in resp.text
