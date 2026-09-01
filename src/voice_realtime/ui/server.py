"""Voice Studio Web 控制台（`vr-ui` CLI 入口）。

FastAPI 服务：React 静态托管 + 服务健康聚合（SpeechRail / LM Studio）
+ WebSocket 事件网关（/ws/subtitles 字幕流、/ws/assistant 助手状态流）。
组件生命周期由 `UIRuntime` 经 FastAPI lifespan 管理。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from voice_realtime.config import Settings, get_settings
from voice_realtime.logging import setup_logging
from voice_realtime.meeting.api import install_meeting_api
from voice_realtime.meeting.events import MeetingEventBroadcaster
from voice_realtime.meeting.inner_os.api import install_inner_os_api
from voice_realtime.meeting.models import RuntimeMode
from voice_realtime.ui.app_context import (
    UIAppContext,
    attach_app_context,
    get_app_context,
    initialize_meeting_backend,
)
from voice_realtime.ui.http_routes import create_http_router
from voice_realtime.ui.runtime import UIRuntime
from voice_realtime.ui.websocket_routes import create_websocket_router

logger = logging.getLogger(__name__)

NetworkScope = Literal["local", "network"]

_CONTROL_RESPONSE_QUEUE_SIZE = 8
_SUBTITLE_INACTIVE_CODE = 4409
_SUBTITLE_INACTIVE_REASON = "字幕模式未激活"
_SUBTITLE_ELIGIBLE_MODES = frozenset({RuntimeMode.SUBTITLES, RuntimeMode.MEETING})
_RUNTIME_DIAGNOSTIC_KEYS = (
    "audio_hub",
    "interaction",
    "subtitles",
    "tts",
    "last_transition",
)
_ASR_WORKLOAD_KEYS = (
    "workload",
    "ws_state",
    "reconnect_count",
    "last_event_age_ms",
)


def create_app(
    settings: Settings | None = None,
    *,
    initialize_meeting: bool = True,
) -> FastAPI:
    """构造 Voice Studio 应用。settings 可注入（测试）。"""
    cfg = settings or get_settings()

    context = UIAppContext(
        settings=cfg,
        meeting_events=MeetingEventBroadcaster(),
        accepted_control_tasks=set(),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        context.runtime = UIRuntime(cfg)
        await context.runtime.start()
        if initialize_meeting:
            await initialize_meeting_backend(context)
        try:
            yield
        finally:
            await context.close()

    app = FastAPI(title="Voice Studio", version="1.4.0", lifespan=lifespan)
    attach_app_context(app, context)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.meeting.allowed_origins,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2\d|3[0-1])\.\d+\.\d+)(:\d+)?$",
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Idempotency-Key", "X-Request-ID"],
    )
    install_meeting_api(app)
    # Register before the catch-all static mount; dependencies resolve lazily.
    install_inner_os_api(app)
    app.include_router(create_http_router(context))
    app.include_router(create_websocket_router(context))
    _mount_static(app, cfg.ui.static_dir)
    return app


def _get_runtime(app: FastAPI) -> UIRuntime | None:
    """取 lifespan 装配的 runtime；未装配（测试直连）返回 None。"""
    return get_app_context(app).runtime


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
            "img-src 'self' data: blob:; media-src 'self' blob: data:; "
            "connect-src 'self' ws: wss: http: https:; "
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

    setup_logging("ui")
    cfg = get_settings()
    logger.info("Voice Studio 启动: http://%s:%s", cfg.ui.host, cfg.ui.port)
    uvicorn.run(create_app(), host=cfg.ui.host, port=cfg.ui.port)


if __name__ == "__main__":
    main()
