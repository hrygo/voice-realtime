"""Voice Studio Web 控制台（`vr-ui` CLI 入口）。

FastAPI 服务：React 静态托管 + 服务健康聚合（TTS 桥 / wlk / LM Studio）
+ WebSocket 事件网关（/ws/subtitles 字幕流、/ws/assistant 助手状态流）。
组件生命周期由 `UIRuntime` 经 FastAPI lifespan 管理。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from voice_realtime.config import Settings, get_settings
from voice_realtime.logging import setup_logging
from voice_realtime.meeting.api import install_meeting_api, meeting_summary_json
from voice_realtime.meeting.events import MeetingEventBroadcaster, MeetingEventClient, make_event
from voice_realtime.meeting.migrations import run_migrations
from voice_realtime.meeting.recovery import RecoveryJournal
from voice_realtime.meeting.repository import PostgresMeetingRepository
from voice_realtime.meeting.session import MeetingSession
from voice_realtime.meeting.summary import MeetingSummaryClient, MeetingSummaryService
from voice_realtime.network import local_async_client
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


def create_app(
    settings: Settings | None = None,
    *,
    initialize_meeting: bool = True,
) -> FastAPI:
    """构造 Voice Studio 应用。settings 可注入（测试）。"""
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """lifespan：装配 UIRuntime 并随服务启停。"""
        runtime = UIRuntime(cfg)
        await runtime.start()
        app.state.runtime = runtime
        if initialize_meeting:
            await _initialize_meeting_backend(app, cfg, runtime)
        try:
            yield
        finally:
            await runtime.stop()
            summary_service = getattr(app.state, "meeting_summary_service", None)
            if summary_service is not None:
                await summary_service.stop()
            repository = getattr(app.state, "meeting_repository", None)
            if repository is not None:
                await repository.close()

    app = FastAPI(title="Voice Studio", version="0.1.0", lifespan=lifespan)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.meeting.allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Idempotency-Key", "X-Request-ID"],
    )
    app.state.meeting_events = MeetingEventBroadcaster()
    install_meeting_api(app)

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
        async with local_async_client(timeout=timeout) as client:
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
            async with local_async_client(timeout=cfg.ui.api_timeout) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return dict(resp.json())
        except httpx.HTTPError as exc:
            logger.warning("Voice Studio: 桥 /v1/voices 请求失败: %s", exc)
            raise HTTPException(status_code=502, detail="TTS 桥音色列表不可用") from exc

    @app.post("/v1/audio/speech")
    async def proxy_speech(request: Request) -> Response:
        """代理 TTS 桥音频合成（POST /v1/audio/speech），供前端音色试听。"""
        url = _probe_url(cfg.bridge.host, cfg.bridge.port, "/v1/audio/speech")
        try:
            body = await request.body()
            headers = {"Content-Type": "application/json"}
            async with local_async_client(timeout=10.0) as client:
                resp = await client.post(url, content=body, headers=headers)
                resp.raise_for_status()
                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "audio/wav"),
                )
        except httpx.HTTPError as exc:
            logger.warning("Voice Studio: 桥 /v1/audio/speech 试听请求失败: %s", exc)
            raise HTTPException(status_code=502, detail="TTS 桥语音合成不可用") from exc

    _mount_websocket_routes(app, cfg)
    _mount_static(app, cfg.ui.static_dir)
    return app


async def _initialize_meeting_backend(
    app: FastAPI,
    cfg: Settings,
    runtime: UIRuntime,
) -> bool:
    """装配会议持久化与纪要服务；失败时保留普通语音助手可用。"""
    repository: PostgresMeetingRepository | None = None
    summary_service: MeetingSummaryService | None = None
    try:
        await run_migrations(cfg.meeting.database_url, schema=cfg.meeting.schema_name)
        repository = PostgresMeetingRepository(cfg.meeting)
        await repository.start()
        journal = RecoveryJournal(cfg.meeting.recovery_dir)
        await journal.replay(repository)
        await repository.recover_stale()
        summary_client = MeetingSummaryClient(
            cfg.meeting,
            base_url=cfg.interaction.llm_base_url,
        )
        broadcaster = _meeting_broadcaster(app)
        summary_service = MeetingSummaryService(
            repository,
            summary_client,
            cfg.meeting,
            event_publisher=broadcaster.publish_event,
        )
        meeting_session = MeetingSession(
            repository,
            runtime.subtitle_proxy,
            summary_service,
            finalization_timeout_secs=cfg.meeting.finalization_timeout_secs,
            recovery_journal=journal,
            event_publisher=broadcaster.publish_event,
        )
        runtime.configure_meeting(meeting_session)
        app.state.meeting_repository = repository
        app.state.meeting_summary_service = summary_service
        app.state.meeting_session = meeting_session
        app.state.meeting_runtime = runtime
        await summary_service.start()
        logger.info("会议助手后端已就绪 (schema=%s)", cfg.meeting.schema_name)
        return True
    except Exception as exc:
        logger.warning(
            "会议助手后端不可用，普通语音助手继续运行 (%s)",
            type(exc).__name__,
        )
        if summary_service is not None:
            with contextlib.suppress(Exception):
                await summary_service.stop()
        if repository is not None:
            with contextlib.suppress(Exception):
                await repository.close()
        app.state.meeting_backend_error = type(exc).__name__
        return False


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

    @app.websocket("/ws/v1/meetings")
    async def ws_v1_meetings(websocket: WebSocket) -> None:
        """会议事件通道；浏览器断开不会释放服务端会议采集租约。"""

        if not await _allow_websocket(websocket, cfg):
            return
        broadcaster = _meeting_broadcaster(websocket.app)
        await websocket.accept()
        client = broadcaster.add_client()
        sender = asyncio.create_task(
            _forward_meeting_events(websocket, client),
            name="meeting-events-send",
        )
        try:
            snapshot = await _meeting_snapshot(websocket.app, broadcaster)
            await websocket.send_json(snapshot)
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            broadcaster.remove_client(client)
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender

    @app.websocket("/ws/v1/control")
    async def ws_v1_control(websocket: WebSocket) -> None:
        """版本化控制通道，保留旧 `/ws/assistant/cmd` 作为兼容入口。"""

        if not await _allow_websocket(websocket, cfg):
            return
        runtime = _get_runtime(websocket.app)
        bridge = ControlBridge(runtime, cfg.bridge) if runtime is not None else None
        await websocket.accept()
        await websocket.send_json(
            {
                "contract_version": "1",
                "event": "runtime_state",
                "state": _runtime_state_json(runtime),
            }
        )
        cache: dict[str, dict[str, Any]] = {}
        try:
            while True:
                text = await websocket.receive_text()
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                response = (
                    await bridge.handle(payload)
                    if bridge is not None
                    else await _handle_v1_control(runtime, cfg, payload, cache)
                )
                await websocket.send_json(response)
        except WebSocketDisconnect:
            return


async def _forward_meeting_events(websocket: WebSocket, client: MeetingEventClient) -> None:
    """从单个 bounded queue 转发事件；慢 socket 不阻塞其他订阅者。"""

    while True:
        event = await client.receive()
        await websocket.send_json(event)


def _meeting_broadcaster(app: FastAPI) -> MeetingEventBroadcaster:
    broadcaster = getattr(app.state, "meeting_events", None)
    if broadcaster is None:
        broadcaster = MeetingEventBroadcaster()
        app.state.meeting_events = broadcaster
    return broadcaster


def _runtime_state_json(runtime: Any) -> dict[str, Any]:
    if runtime is None or not hasattr(runtime, "snapshot"):
        return {}
    state = runtime.snapshot()
    if hasattr(state, "model_dump"):
        state = state.model_dump(mode="json")
    return state if isinstance(state, dict) else {}


async def _meeting_snapshot(app: FastAPI, broadcaster: MeetingEventBroadcaster) -> dict[str, Any]:
    provided = await broadcaster.snapshot_async()
    if isinstance(provided, dict) and provided.get("type") == "meeting_snapshot":
        return provided
    runtime = getattr(app.state, "meeting_runtime", None) or getattr(app.state, "runtime", None)
    runtime_state = _runtime_state_json(runtime)
    active = getattr(runtime, "active_meeting_id", None)
    if active is not None:
        active = getattr(active, "value", active)
    meeting_id = str(runtime_state.get("active_meeting_id") or active or "")
    meeting_status = str(runtime_state.get("meeting_state") or "completed")
    meeting_started_at = runtime_state.get("meeting_started_at")
    repository = getattr(app.state, "meeting_repository", None)
    if repository is not None and meeting_id:
        try:
            meeting = await repository.get_meeting(UUID(meeting_id))
            if meeting is not None:
                document = await repository.get_transcript(meeting.id)
                session = getattr(app.state, "meeting_session", None)
                last_window = getattr(session, "last_window", None)
                payload = {
                    "meeting": meeting_summary_json(meeting),
                    "health": {
                        "storage": runtime_state.get("storage", "ok"),
                        "transcription": "ok",
                        "mic_muted": bool(runtime_state.get("mic_muted", False)),
                        "recovery_journal_active": (
                            str(runtime_state.get("storage", "ok")) == "degraded"
                        ),
                    },
                    "partial": getattr(last_window, "partial", None),
                    "transcript_revision": document.transcript_revision,
                    "content_revision": document.content_revision,
                }
                return make_event("meeting_snapshot", meeting_id, payload)
        except Exception:
            logger.warning("构造会议快照失败，回退运行时快照", exc_info=True)
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    payload = {
        "meeting": {
            "id": meeting_id,
            "title": "",
            "status": meeting_status,
            "language": "Chinese",
            "started_at": meeting_started_at,
            "ended_at": None,
            "transcript_revision": 0,
            "content_revision": 0,
            "interruption_reason": None,
            "created_at": meeting_started_at or now,
        },
        "health": {
            "storage": runtime_state.get("storage", "ok"),
            "transcription": "ok",
            "mic_muted": bool(runtime_state.get("mic_muted", False)),
            "recovery_journal_active": False,
        },
        "partial": None,
        "transcript_revision": 0,
        "content_revision": 0,
    }
    return make_event("meeting_snapshot", meeting_id, payload)


async def _handle_v1_control(
    runtime: Any,
    cfg: Settings,
    payload: dict[str, Any],
    cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    request_id = payload.get("request_id") if isinstance(payload.get("request_id"), str) else ""
    cmd = payload.get("cmd") if isinstance(payload.get("cmd"), str) else ""
    if request_id and request_id in cache:
        return cache[request_id]
    base = {
        "contract_version": "1",
        "request_id": request_id,
        "cmd": cmd,
        "ok": False,
        "state": _runtime_state_json(runtime),
        "error": None,
    }
    if not request_id or not cmd:
        base["error"] = {
            "code": "invalid_request",
            "message": "控制命令无效",
        }
        return base
    try:
        if runtime is None:
            raise RuntimeError("runtime unavailable")
        if hasattr(runtime, "handle_meeting_command"):
            result = runtime.handle_meeting_command(payload)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, dict):
                base.update(result)
        elif cmd == "start_meeting" and hasattr(runtime, "start_meeting"):
            result = runtime.start_meeting(payload.get("title"))
            if inspect.isawaitable(result):
                await result
        elif cmd == "end_meeting" and hasattr(runtime, "end_meeting"):
            result = runtime.end_meeting(payload.get("meeting_id"))
            if inspect.isawaitable(result):
                await result
        elif cmd == "start_assistant" and hasattr(runtime, "start_assistant"):
            result = runtime.start_assistant()
            if inspect.isawaitable(result):
                await result
        elif cmd == "stop_active_mode" and hasattr(runtime, "stop_active_mode"):
            result = runtime.stop_active_mode()
            if inspect.isawaitable(result):
                await result
        else:
            raise ValueError("unsupported command")
        base["ok"] = True
        base["state"] = _runtime_state_json(runtime)
    except ValueError:
        base["error"] = {"code": "invalid_request", "message": "控制命令无效"}
    except Exception as exc:
        error_code = str(getattr(exc, "code", "command_failed"))
        allowed_codes = {
            "conflict",
            "mode_conflict",
            "meeting_not_active",
            "storage_unavailable",
            "transcription_unavailable",
            "summary_unavailable",
            "service_unavailable",
            "command_failed",
        }
        if error_code not in allowed_codes:
            error_code = "command_failed"
        base["error"] = {
            "code": error_code,
            "message": "命令执行失败，请检查相关服务状态",
        }
    if request_id:
        if len(cache) >= 256:
            cache.pop(next(iter(cache)))
        cache[request_id] = base
    return base


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
    meeting_cfg = getattr(cfg, "meeting", None)
    configured_origins = getattr(meeting_cfg, "allowed_origins", ()) if meeting_cfg else ()
    allowed.update(str(item) for item in configured_origins)
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
