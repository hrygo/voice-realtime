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
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import is_dataclass
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from voice_realtime.config import Settings, get_settings
from voice_realtime.logging import setup_logging
from voice_realtime.meeting.api import install_meeting_api, meeting_summary_json
from voice_realtime.meeting.diarization_smoother import DiarizationSmoother
from voice_realtime.meeting.events import MeetingEventBroadcaster, MeetingEventClient, make_event
from voice_realtime.meeting.migrations import run_migrations
from voice_realtime.meeting.models import RuntimeMode
from voice_realtime.meeting.recovery import RecoveryJournal
from voice_realtime.meeting.repository import PostgresMeetingRepository
from voice_realtime.meeting.session import MeetingSession
from voice_realtime.meeting.summary import MeetingSummaryClient, MeetingSummaryService
from voice_realtime.network import local_async_client
from voice_realtime.ui.control import ControlBridge
from voice_realtime.ui.protocol import ErrorCode, RuntimeStateEvent, RuntimeStateSnapshot
from voice_realtime.ui.runtime import UIRuntime
from voice_realtime.ui.runtime_events import RuntimeStateClient

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
_WLK_WORKLOAD_KEYS = (
    "workload",
    "ws_state",
    "reconnect_count",
    "last_event_age_ms",
)


def _probe_url(host: str, port: int, path: str = "/health") -> str:
    target_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return f"http://{target_host}:{port}{path}"


def _network_scope(host: str) -> NetworkScope:
    normalized = host.strip().lower()
    return "local" if normalized in {"127.0.0.1", "localhost", "::1", "[::1]"} else "network"


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


def _empty_runtime_diagnostics() -> dict[str, Any]:
    return {
        "audio_hub": {},
        "interaction": {},
        "subtitles": {},
        "tts": {},
        "last_transition": None,
    }


def _redact_diagnostic_binary(_value: object) -> None:
    """诊断接口只公开指标，不复制 PCM 或其他二进制载荷。"""


def _json_safe_mapping(value: object) -> dict[str, Any] | None:
    """把 mapping/frozen dataclass 转成已验证可 JSON 序列化的深副本。"""
    if not isinstance(value, Mapping) and not (
        is_dataclass(value) and not isinstance(value, type)
    ):
        return None
    encoded = jsonable_encoder(
        value,
        custom_encoder={
            bytes: _redact_diagnostic_binary,
            bytearray: _redact_diagnostic_binary,
            memoryview: _redact_diagnostic_binary,
            asyncio.Queue: _redact_diagnostic_binary,
        },
    )
    copied = cast(object, json.loads(json.dumps(encoded, allow_nan=False)))
    if not isinstance(copied, dict):
        return None
    return cast(dict[str, Any], copied)


def _runtime_diagnostics(runtime: Any) -> dict[str, Any]:
    fallback = _empty_runtime_diagnostics()
    if runtime is None:
        return fallback
    diagnostics = getattr(runtime, "diagnostics", None)
    if not callable(diagnostics):
        return fallback
    try:
        raw = _json_safe_mapping(diagnostics())
        if raw is None:
            return fallback
        return {
            key: raw.get(key, fallback[key]) for key in _RUNTIME_DIAGNOSTIC_KEYS
        }
    except Exception as exc:
        logger.warning(
            "Voice Studio: runtime diagnostics unavailable: %s",
            type(exc).__name__,
        )
        return fallback


def _wlk_workload_diagnostics(runtime: Any) -> dict[str, Any]:
    if runtime is None:
        return {}
    try:
        snapshot = getattr(runtime, "snapshot", None)
        subtitle_proxy = getattr(runtime, "subtitle_proxy", None)
        diagnostics = getattr(subtitle_proxy, "diagnostics", None)
        if not callable(snapshot) or not callable(diagnostics):
            return {}
        state = snapshot()
        raw = _json_safe_mapping(diagnostics(state.pcm_owner))
        if raw is None:
            return {}
        return {key: raw[key] for key in _WLK_WORKLOAD_KEYS if key in raw}
    except Exception as exc:
        logger.warning(
            "Voice Studio: WLK workload diagnostics unavailable: %s",
            type(exc).__name__,
        )
        return {}


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

    app = FastAPI(title="Voice Studio", version="1.1.0", lifespan=lifespan)
    app.state.accepted_control_tasks = set()
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.meeting.allowed_origins,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2\d|3[0-1])\.\d+\.\d+)(:\d+)?$",
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
    async def services() -> dict[str, Any]:
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
        service_results = list(results)
        runtime = _get_runtime(app)
        workload_diagnostics = _wlk_workload_diagnostics(runtime)
        if workload_diagnostics:
            wlk_service = next(
                (item for item in service_results if item["name"] == "wlk"),
                None,
            )
            if wlk_service is not None:
                wlk_service.update(workload_diagnostics)
        return {
            "network_scope": _network_scope(cfg.ui.host),
            "services": service_results,
            "diagnostics": _runtime_diagnostics(runtime),
        }

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
        smoother = DiarizationSmoother(
            enabled=cfg.meeting.diarization_smoothing_enabled,
            min_duration_ms=cfg.meeting.diarization_min_duration_ms,
            hangover_gap_ms=cfg.meeting.diarization_hangover_gap_ms,
        )
        meeting_session = MeetingSession(
            repository,
            runtime.subtitle_proxy,
            summary_service,
            finalization_timeout_secs=cfg.meeting.finalization_timeout_secs,
            recovery_journal=journal,
            event_publisher=broadcaster.publish_event,
            diarization_smoother=smoother,
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
        await _serve_subtitle_websocket(websocket, runtime)

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
        await _serve_control_websocket(websocket, runtime, cfg)

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
        if runtime is None:
            await websocket.close(code=1011, reason="runtime 未就绪")
            return
        await _serve_control_websocket(websocket, runtime, cfg)


async def _serve_control_websocket(
    websocket: WebSocket,
    runtime: UIRuntime,
    cfg: Settings,
) -> None:
    """用单 writer 合并命令响应与 latest-only 运行时广播。"""

    bridge = ControlBridge(runtime, cfg.bridge)
    runtime_client = runtime.runtime_events.add_client()
    responses: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_CONTROL_RESPONSE_QUEUE_SIZE)
    sender: asyncio.Task[None] | None = None
    try:
        await websocket.accept()
        sender = asyncio.create_task(
            _send_control_messages(websocket, responses, runtime_client),
            name="control-websocket-sender",
        )
        while True:
            payload = _decode_control_payload(await websocket.receive_text())
            command_task = asyncio.create_task(
                bridge.handle(payload),
                name="accepted-control-command",
            )
            _track_accepted_control_task(websocket.app, command_task)
            try:
                response = await asyncio.shield(command_task)
            except Exception:
                if sender is not None:
                    await _cancel_background_task(sender)
                    sender = None
                await websocket.close(code=1011, reason="控制命令执行失败")
                return
            await responses.put(response)
    except WebSocketDisconnect:
        pass
    finally:
        if sender is not None:
            await _cancel_background_task(sender)
        runtime.runtime_events.remove_client(runtime_client)


async def _send_control_messages(
    websocket: WebSocket,
    responses: asyncio.Queue[dict[str, Any]],
    runtime_client: RuntimeStateClient,
) -> None:
    """唯一写协程；每轮都回收未完成的临时 queue.get task。"""

    while True:
        response_get = asyncio.create_task(
            responses.get(),
            name="control-response-get",
        )
        runtime_get = asyncio.create_task(
            runtime_client.receive(),
            name="control-runtime-state-get",
        )
        get_tasks = {response_get, runtime_get}
        try:
            done, pending = await asyncio.wait(
                get_tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except BaseException:
            for task in get_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*get_tasks, return_exceptions=True)
            raise

        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        if runtime_get in done:
            event = RuntimeStateEvent(
                contract_version="1",
                state=runtime_get.result(),
            )
            await websocket.send_json(event.model_dump(mode="json"))
        if response_get in done:
            await websocket.send_json(response_get.result())


async def _serve_subtitle_websocket(websocket: WebSocket, runtime: UIRuntime) -> None:
    """仅在字幕/会议模式维持只读字幕订阅，并随模式变化撤销。"""

    runtime_client = runtime.runtime_events.add_client()
    initial = runtime_client.latest_nowait()
    ws_send = websocket.send_text
    proxy_registered = False
    revoker: asyncio.Task[None] | None = None
    try:
        await websocket.accept()
        if not _subtitle_mode_is_eligible(initial):
            await websocket.close(
                code=_SUBTITLE_INACTIVE_CODE,
                reason=_SUBTITLE_INACTIVE_REASON,
            )
            return

        latest = initial
        with contextlib.suppress(asyncio.QueueEmpty):
            latest = runtime_client.latest_nowait()
        if not _subtitle_mode_is_eligible(latest):
            await websocket.close(
                code=_SUBTITLE_INACTIVE_CODE,
                reason=_SUBTITLE_INACTIVE_REASON,
            )
            return

        runtime.subtitle_proxy.add_client(ws_send)
        proxy_registered = True
        revoker = asyncio.create_task(
            _revoke_ineligible_subtitle(websocket, runtime_client),
            name="subtitle-mode-revoker",
        )
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if revoker is not None:
            await _cancel_background_task(revoker)
        if proxy_registered:
            runtime.subtitle_proxy.remove_client(ws_send)
        runtime.runtime_events.remove_client(runtime_client)


async def _revoke_ineligible_subtitle(
    websocket: WebSocket,
    runtime_client: RuntimeStateClient,
) -> None:
    while True:
        state = await runtime_client.receive()
        if not _subtitle_mode_is_eligible(state):
            await websocket.close(
                code=_SUBTITLE_INACTIVE_CODE,
                reason=_SUBTITLE_INACTIVE_REASON,
            )
            return


def _subtitle_mode_is_eligible(state: RuntimeStateSnapshot) -> bool:
    return state.mode in _SUBTITLE_ELIGIBLE_MODES


def _decode_control_payload(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _track_accepted_control_task(
    app: FastAPI,
    task: asyncio.Task[dict[str, Any]],
) -> None:
    tasks = _accepted_control_tasks(app)
    tasks.add(task)
    task.add_done_callback(lambda completed: _accepted_control_task_done(app, completed))


def _accepted_control_tasks(app: FastAPI) -> set[asyncio.Task[dict[str, Any]]]:
    return cast(set[asyncio.Task[dict[str, Any]]], app.state.accepted_control_tasks)


def _accepted_control_task_done(
    app: FastAPI,
    task: asyncio.Task[dict[str, Any]],
) -> None:
    _accepted_control_tasks(app).discard(task)
    if task.cancelled():
        logger.error(
            "已接受控制事务异常结束 (type=CancelledError code=%s)",
            ErrorCode.COMMAND_FAILED.value,
        )
        return
    try:
        task.result()
    except Exception as exc:
        error_code = _stable_control_error_code(exc)
        logger.error(
            "已接受控制事务异常结束 (type=%s code=%s)",
            type(exc).__name__,
            error_code,
        )


def _stable_control_error_code(exc: Exception) -> str:
    raw_code = getattr(exc, "code", ErrorCode.COMMAND_FAILED)
    try:
        return ErrorCode(raw_code).value
    except (TypeError, ValueError):
        return ErrorCode.COMMAND_FAILED.value


async def _cancel_background_task(task: asyncio.Task[Any]) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.debug("WebSocket 后台任务已结束 (type=%s)", type(exc).__name__)


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


async def _allow_websocket(websocket: WebSocket, cfg: Settings) -> bool:
    """拒绝非信任来源的浏览器 WebSocket；无 Origin 的本地 CLI 保持可用。"""
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    loopback_origins = {
        f"http://127.0.0.1:{cfg.ui.port}",
        f"http://localhost:{cfg.ui.port}",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        f"http://[::1]:{cfg.ui.port}",
        "http://[::1]:5173",
    }
    meeting_cfg = getattr(cfg, "meeting", None)
    configured_origins = (
        getattr(meeting_cfg, "allowed_origins", ())
        if meeting_cfg is not None
        and "allowed_origins" in meeting_cfg.model_fields_set
        else ()
    )
    if (
        origin in {str(item) for item in configured_origins}
        or _origin_matches_websocket_host(websocket, origin)
        or (origin in loopback_origins and _websocket_host_is_loopback(websocket))
    ):
        return True

    await websocket.close(code=1008, reason="Origin 不受信任")
    return False


def _origin_matches_websocket_host(websocket: WebSocket, origin: str) -> bool:
    """仅接受与实际 WebSocket 请求目标同源的浏览器 Origin。"""
    try:
        parsed = urlsplit(origin)
        expected_scheme = {"ws": "http", "wss": "https"}.get(
            websocket.url.scheme.lower()
        )
        if (
            expected_scheme is None
            or parsed.scheme.lower() != expected_scheme
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return False
        origin_port = parsed.port or (443 if expected_scheme == "https" else 80)

        request_host = websocket.headers.get("host")
        if request_host is None:
            return False
        request_authority = urlsplit(f"//{request_host}")
        if (
            request_authority.hostname is None
            or request_authority.username is not None
            or request_authority.password is not None
            or request_authority.path
            or request_authority.query
            or request_authority.fragment
        ):
            return False
        request_port = request_authority.port or (
            443 if expected_scheme == "https" else 80
        )
    except ValueError:
        return False

    return (
        _canonical_hostname(parsed.hostname)
        == _canonical_hostname(request_authority.hostname)
        and origin_port == request_port
    )


def _websocket_host_is_loopback(websocket: WebSocket) -> bool:
    """仅在实际请求 Host 为 localhost/IP loopback 时启用开发白名单。"""
    request_host = websocket.headers.get("host")
    if request_host is None:
        return False
    try:
        request_authority = urlsplit(f"//{request_host}")
        hostname = request_authority.hostname
        if (
            hostname is None
            or request_authority.username is not None
            or request_authority.password is not None
            or request_authority.path
            or request_authority.query
            or request_authority.fragment
        ):
            return False
        _ = request_authority.port
    except ValueError:
        return False

    if hostname.casefold() == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def _canonical_hostname(hostname: str) -> str:
    """按 URL 主机语义规范化大小写与 IPv4/IPv6 文本形式。"""
    try:
        return ip_address(hostname).compressed
    except ValueError:
        return hostname.casefold()


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

    setup_logging()
    cfg = get_settings()
    logger.info("Voice Studio 启动: http://%s:%s", cfg.ui.host, cfg.ui.port)
    uvicorn.run(create_app(), host=cfg.ui.host, port=cfg.ui.port)


if __name__ == "__main__":
    main()
