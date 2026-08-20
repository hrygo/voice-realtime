"""Voice Studio Web 控制台（`vr-ui` CLI 入口）。

FastAPI 服务：React 静态托管 + 服务健康聚合（TTS 桥 / wlk / LM Studio）
+ WebSocket 事件网关（/ws/subtitles 字幕流、/ws/assistant 助手状态流）。
组件生命周期由 `UIRuntime` 经 FastAPI lifespan 管理。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from voice_realtime.config import Settings, get_settings
from voice_realtime.logging import setup_logging
from voice_realtime.ui.control import ControlBridge
from voice_realtime.ui.protocol import RuntimeStateEvent
from voice_realtime.ui.runtime import UIRuntime

logger = logging.getLogger(__name__)


def _probe_url(host: str, port: int, path: str = "/health") -> str:
    return f"http://{host}:{port}{path}"


async def _do_probe_async(
    client: httpx.AsyncClient,
    name: str,
    url: str,
    expected_model: str | None = None,
) -> dict[str, Any]:
    """并发异步探活单个服务。"""
    try:
        resp = await client.get(url)
        result: dict[str, Any] = {
            "name": name,
            "status": "ok" if resp.status_code < 400 else "error",
            "url": url,
        }
        if expected_model is not None and resp.status_code < 400:
            model_ids: set[str] = set()
            with contextlib.suppress(ValueError, TypeError):
                body = resp.json()
                if isinstance(body, dict) and isinstance(body.get("data"), list):
                    model_ids = {
                        item["id"]
                        for item in body["data"]
                        if isinstance(item, dict) and isinstance(item.get("id"), str)
                    }
            result["target_model"] = expected_model
            result["model_present"] = expected_model in model_ids
        return result
    except httpx.ConnectError:
        return {"name": name, "status": "unreachable", "url": url}
    except (httpx.ReadTimeout, httpx.TimeoutException):
        return {"name": name, "status": "timeout", "url": url}
    except Exception:
        return {"name": name, "status": "error", "url": url}


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造 Voice Studio 应用。settings 可注入（测试）。"""
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """lifespan：装配 UIRuntime 并随服务启停。"""
        runtime = UIRuntime(cfg)
        await runtime.start()
        app.state.runtime = runtime
        yield
        await runtime.stop()

    app = FastAPI(title="Voice Studio", version="0.1.0", lifespan=lifespan)
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/services")
    async def services() -> dict[str, list[dict[str, Any]]]:
        """三服务健康灯聚合（并发异步探活，单次总延时 <= timeout）。"""
        timeout = min(cfg.ui.api_timeout, 1.0)
        wlk = cfg.subtitles
        bridge = cfg.bridge
        lm = cfg.interaction
        paths = [
            ("wlk", _probe_url(wlk.host, wlk.port), None),
            ("tts", _probe_url(bridge.host, bridge.port), None),
            ("lm", _lm_models_url(lm.llm_base_url), lm.llm_model),
        ]
        async with httpx.AsyncClient(timeout=timeout) as client:
            tasks = [
                _do_probe_async(client, name, url, expected_model)
                for name, url, expected_model in paths
            ]
            results = await asyncio.gather(*tasks)
        return {"services": list(results)}

    @app.get("/api/runtime")
    async def runtime_state() -> dict[str, Any]:
        runtime = _get_runtime(app)
        if runtime is None:
            raise HTTPException(status_code=503, detail="runtime 未就绪")
        return runtime.snapshot().model_dump(mode="json")

    @app.get("/v1/voices")
    async def voices() -> dict[str, Any]:
        """代理 TTS 桥音色列表（GET /v1/voices），供前端音色下拉。"""
        url = _probe_url(cfg.bridge.host, cfg.bridge.port, "/v1/voices")
        try:
            async with httpx.AsyncClient(timeout=cfg.ui.api_timeout) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return dict(resp.json())
        except httpx.HTTPError as exc:
            logger.warning("Voice Studio: 桥 /v1/voices 请求失败: %s", exc)
            raise HTTPException(status_code=502, detail="TTS 桥音色列表不可用") from exc

    _mount_websocket_routes(app, cfg)
    _mount_static(app, cfg.ui.static_dir)
    return app


def _get_runtime(app: FastAPI) -> UIRuntime | None:
    """取 lifespan 装配的 runtime；未装配（测试直连）返回 None。"""
    return getattr(app.state, "runtime", None)


def _lm_models_url(base_url: str) -> str:
    """LM Studio OpenAI 兼容端点 → /models 探活地址。"""
    return base_url.rstrip("/") + "/models"


def _mount_websocket_routes(app: FastAPI, cfg: Settings) -> None:
    """事件网关：/ws/subtitles + /ws/assistant + /ws/assistant/cmd（控制面）。"""

    @app.websocket("/ws/subtitles")
    async def ws_subtitles(websocket: WebSocket) -> None:
        if not await _allow_websocket(websocket, cfg):
            return
        runtime = _get_runtime(websocket.app)
        if runtime is None:
            await websocket.close(code=1011, reason="runtime 未就绪")
            return
        await websocket.accept()
        ws_send = websocket.send_text
        runtime.subtitle_proxy.add_client(ws_send)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            runtime.subtitle_proxy.remove_client(ws_send)

    @app.websocket("/ws/assistant")
    async def ws_assistant(websocket: WebSocket) -> None:
        if not await _allow_websocket(websocket, cfg):
            return
        runtime = _get_runtime(websocket.app)
        if runtime is None:
            await websocket.close(code=1011, reason="runtime 未就绪")
            return
        await websocket.accept()
        ws_send = websocket.send_text
        runtime.observer.add_client(ws_send)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            runtime.observer.remove_client(ws_send)

    @app.websocket("/ws/assistant/cmd")
    async def ws_assistant_cmd(websocket: WebSocket) -> None:
        if not await _allow_websocket(websocket, cfg):
            return
        runtime = _get_runtime(websocket.app)
        if runtime is None:
            await websocket.close(code=1011, reason="runtime 未就绪")
            return
        bridge = ControlBridge(runtime, cfg.bridge)
        await websocket.accept()
        event = RuntimeStateEvent(state=runtime.snapshot())
        await websocket.send_json(event.model_dump(mode="json"))
        try:
            while True:
                text = await websocket.receive_text()
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = {}
                await websocket.send_json(await bridge.handle(payload))
        except WebSocketDisconnect:
            return


async def _allow_websocket(websocket: WebSocket, cfg: Settings) -> bool:
    """拒绝来自非本机页面的浏览器 WebSocket；无 Origin 的本地 CLI 保持可用。"""
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    allowed = {
        f"http://127.0.0.1:{cfg.ui.port}",
        f"http://localhost:{cfg.ui.port}",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    }
    if origin in allowed:
        return True
    await websocket.close(code=1008, reason="Origin 不受信任")
    return False


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """为本地控制台统一添加最小浏览器安全边界。"""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self' ws://127.0.0.1:* "
            "ws://localhost:* http://127.0.0.1:* http://localhost:*; "
            "object-src 'none'; base-uri 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response


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
