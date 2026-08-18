"""Voice Studio Web 控制台（`vr-ui` CLI 入口）。

FastAPI 服务：React 静态托管 + 服务健康聚合（TTS 桥 / wlk / LM Studio）
+ WebSocket 事件网关骨架（/ws/subtitles、/ws/assistant 由 M2/M3 填充）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from voice_realtime.config import Settings, get_settings
from voice_realtime.logging import setup_logging

logger = logging.getLogger(__name__)


def _probe_url(host: str, port: int, path: str = "/health") -> str:
    return f"http://{host}:{port}{path}"


def _do_probe(name: str, url: str, timeout: float) -> dict[str, Any]:
    """同步探活：返回服务状态。"""
    try:
        resp = httpx.get(url, timeout=timeout)
        return {
            "name": name,
            "status": "ok" if resp.status_code < 400 else "error",
            "url": url,
        }
    except httpx.ConnectError:
        return {"name": name, "status": "unreachable", "url": url}
    except httpx.ReadTimeout:
        return {"name": name, "status": "timeout", "url": url}
    except httpx.TimeoutException:
        return {"name": name, "status": "timeout", "url": url}


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造 Voice Studio 应用。settings 可注入（测试）。"""
    cfg = settings or get_settings()
    app = FastAPI(title="Voice Studio", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/services")
    async def services() -> dict[str, list[dict[str, Any]]]:
        """三服务健康灯聚合（探活失败不抛错，返回每项 status）。"""
        timeout = cfg.ui.api_timeout
        wlk = cfg.subtitles
        bridge = cfg.bridge
        lm = cfg.interaction
        paths = [
            ("wlk", _probe_url(wlk.host, wlk.port)),
            ("tts", _probe_url(bridge.host, bridge.port)),
            ("lm", _lm_models_url(lm.llm_base_url)),
        ]
        results = [_do_probe(name, url, timeout) for name, url in paths]
        return {"services": results}

    _mount_websocket_skeletons(app)
    _mount_static(app, cfg.ui.static_dir)
    return app


def _lm_models_url(base_url: str) -> str:
    """LM Studio OpenAI 兼容端点 → /models 探活地址。"""
    return base_url.rstrip("/") + "/models"


def _mount_websocket_skeletons(app: FastAPI) -> None:
    """M2/M3 填充的事件网关占位：先接受连接并回显 connect 事件。"""

    @app.websocket("/ws/subtitles")
    async def ws_subtitles(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            return

    @app.websocket("/ws/assistant")
    async def ws_assistant(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            return


def _mount_static(app: FastAPI, static_dir: Path) -> None:
    """挂载 React build 产物（ui/dist）；不存在则跳过。"""
    if static_dir.is_dir() and (static_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    else:
        @app.get("/")
        async def placeholder() -> dict[str, str]:
            return {"message": "Voice Studio 未构建（npm run build 于 ui/）"}


def main() -> None:
    """`vr-ui` 控制台入口。"""
    import uvicorn

    setup_logging()
    cfg = get_settings()
    logger.info("Voice Studio 启动: http://%s:%s", cfg.ui.host, cfg.ui.port)
    uvicorn.run(create_app(), host=cfg.ui.host, port=cfg.ui.port)


if __name__ == "__main__":
    main()
