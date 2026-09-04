"""UI WebSocket 路由：字幕、助手、会议、inner-OS 与 v1 control 通道。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from sona.config import Settings
from sona.meeting.api import meeting_summary_json
from sona.meeting.events import MeetingEventBroadcaster, MeetingEventClient, make_event
from sona.meeting.inner_os.private_channel import (
    InnerOSChannelError,
    InnerOSConnectionSession,
    InnerOSQueryServicePort,
)
from sona.meeting.models import RuntimeMode
from sona.ui.app_context import UIAppContext
from sona.ui.control import ControlBridge
from sona.ui.protocol import ErrorCode, RuntimeStateEvent, RuntimeStateSnapshot
from sona.ui.runtime import UIRuntime
from sona.ui.runtime_events import RuntimeStateClient

logger = logging.getLogger(__name__)

_CONTROL_RESPONSE_QUEUE_SIZE = 8
_SUBTITLE_INACTIVE_CODE = 4409
_SUBTITLE_INACTIVE_REASON = "字幕模式未激活"
_SUBTITLE_ELIGIBLE_MODES = frozenset({RuntimeMode.SUBTITLES, RuntimeMode.MEETING})


def create_websocket_router(context: UIAppContext) -> APIRouter:
    """Build subtitle, assistant, meeting, inner-OS and v1 control sockets."""
    router = APIRouter()

    @router.websocket("/ws/subtitles")
    async def ws_subtitles(websocket: WebSocket) -> None:
        if not await _allow_websocket(websocket, context.settings):
            return
        runtime = context.runtime
        if runtime is None:
            await websocket.close(code=1011, reason="runtime 未就绪")
            return
        await _serve_subtitle_websocket(websocket, runtime)

    @router.websocket("/ws/assistant")
    async def ws_assistant(websocket: WebSocket) -> None:
        if not await _allow_websocket(websocket, context.settings):
            return
        runtime = context.runtime
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

    @router.websocket("/ws/assistant/cmd")
    async def ws_assistant_cmd(websocket: WebSocket) -> None:
        if not await _allow_websocket(websocket, context.settings):
            return
        runtime = context.runtime
        if runtime is None:
            await websocket.close(code=1011, reason="runtime 未就绪")
            return
        await _serve_control_websocket(websocket, runtime, context)

    @router.websocket("/ws/v1/meetings")
    async def ws_v1_meetings(websocket: WebSocket) -> None:
        """会议事件通道；浏览器断开不会释放服务端会议采集租约。"""

        if not await _allow_websocket(websocket, context.settings):
            return
        broadcaster = context.meeting_events
        await websocket.accept()
        client = broadcaster.add_client()
        sender = asyncio.create_task(
            _forward_meeting_events(websocket, client),
            name="meeting-events-send",
        )
        try:
            snapshot = await _meeting_snapshot(context, broadcaster)
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

    @router.websocket("/ws/v1/meetings/{meeting_id}/inner-os")
    async def ws_inner_os(websocket: WebSocket, meeting_id: str) -> None:
        if (
            not context.settings.meeting.inner_os_enabled
            or not _websocket_host_is_loopback(websocket)
            or not await _allow_websocket(websocket, context.settings)
        ):
            await websocket.close(code=1008, reason="inner_os_private_channel_required")
            return
        service = context.inner_os_service
        if service is None:
            await websocket.close(code=1011, reason="inner_os_context_unavailable")
            return
        try:
            parsed_meeting_id = UUID(meeting_id)
        except ValueError:
            await websocket.close(code=1008, reason="inner_os_invalid_request")
            return
        await websocket.accept()
        session = InnerOSConnectionSession(
            meeting_id=parsed_meeting_id,
            service=cast(InnerOSQueryServicePort, service),
            send=websocket.send_json,
            analysis_enabled=context.settings.meeting.inner_os_analysis_enabled,
            cancel_timeout_secs=context.settings.meeting.inner_os_cancel_timeout_secs,
        )

        try:
            while True:
                await session.handle_text(await websocket.receive_text())
        except InnerOSChannelError as exc:
            await websocket.close(code=1008, reason=exc.code)
        except WebSocketDisconnect:
            pass
        finally:
            await session.close()

    @router.websocket("/ws/v1/control")
    async def ws_v1_control(websocket: WebSocket) -> None:
        """版本化控制通道，保留旧 `/ws/assistant/cmd` 作为兼容入口。"""

        if not await _allow_websocket(websocket, context.settings):
            return
        runtime = context.runtime
        if runtime is None:
            await websocket.close(code=1011, reason="runtime 未就绪")
            return
        await _serve_control_websocket(websocket, runtime, context)

    return router


async def _serve_control_websocket(
    websocket: WebSocket,
    runtime: UIRuntime,
    context: UIAppContext,
) -> None:
    """用单 writer 合并命令响应与 latest-only 运行时广播。"""

    bridge = ControlBridge(runtime)
    runtime_client = runtime.runtime_events.add_client()
    responses: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
        maxsize=_CONTROL_RESPONSE_QUEUE_SIZE
    )
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
            _track_accepted_control_task(context, command_task)
            try:
                response = await asyncio.shield(command_task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 单条命令异常（如快照构建失败）不关闭整条控制通道：
                # 返回无 state 的降级错误响应，前端按 request_id 拒绝该次请求后可重试。
                # 只记录异常类型名，不落异常正文（payload 可能含敏感信息）。
                logger.warning(
                    "控制命令执行异常 (%s)，返回降级错误响应", type(exc).__name__
                )
                request_id = payload.get("request_id")
                cmd = payload.get("cmd")
                await responses.put(
                    {
                        "request_id": request_id if isinstance(request_id, str) else "",
                        "cmd": cmd if isinstance(cmd, str) else "",
                        "ok": False,
                        "error_code": ErrorCode.COMMAND_FAILED.value,
                        "message": "控制命令执行失败，请查看服务日志",
                    }
                )
                continue
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
    context: UIAppContext,
    task: asyncio.Task[dict[str, Any]],
) -> None:
    tasks = context.accepted_control_tasks
    tasks.add(task)
    task.add_done_callback(lambda completed: _accepted_control_task_done(tasks, completed))


def _accepted_control_task_done(
    tasks: set[asyncio.Task[dict[str, Any]]],
    task: asyncio.Task[dict[str, Any]],
) -> None:
    tasks.discard(task)
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


def _runtime_state_json(runtime: Any) -> dict[str, Any]:
    if runtime is None or not hasattr(runtime, "snapshot"):
        return {}
    state = runtime.snapshot()
    if hasattr(state, "model_dump"):
        state = state.model_dump(mode="json")
    return state if isinstance(state, dict) else {}


async def _meeting_snapshot(
    context: UIAppContext, broadcaster: MeetingEventBroadcaster
) -> dict[str, Any]:
    provided = await broadcaster.snapshot_async()
    if isinstance(provided, dict) and provided.get("type") == "meeting_snapshot":
        await broadcaster.observe_snapshot(provided)
        return provided
    runtime = context.runtime
    runtime_state = _runtime_state_json(runtime)
    active = getattr(runtime, "active_meeting_id", None)
    if active is not None:
        active = getattr(active, "value", active)
    meeting_id = str(runtime_state.get("active_meeting_id") or active or "")
    meeting_status = str(runtime_state.get("meeting_state") or "completed")
    meeting_started_at = runtime_state.get("meeting_started_at")
    repository = context.meeting_repository
    if repository is not None and meeting_id:
        try:
            meeting = await repository.get_meeting(UUID(meeting_id))
            if meeting is not None:
                document = await repository.get_transcript(meeting.id)
                session = context.meeting_session
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
                    "partial": _partial_snapshot_json(last_window),
                    "transcript_revision": document.transcript_revision,
                    "content_revision": document.content_revision,
                }
                snapshot = make_event("meeting_snapshot", meeting_id, payload)
                await broadcaster.observe_snapshot(snapshot)
                return snapshot
        except Exception:
            logger.warning("构造会议快照失败，回退运行时快照", exc_info=True)
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    payload = {
        "meeting": {
            "id": meeting_id or "00000000-0000-4000-8000-000000000000",
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
    snapshot = make_event("meeting_snapshot", meeting_id, payload)
    await broadcaster.observe_snapshot(snapshot)
    return snapshot


def _partial_snapshot_json(window: Any) -> dict[str, str | None] | None:
    """Serialize the volatile partial without exposing the domain model."""

    text = str(getattr(window, "partial", "") or "").strip()
    if not text:
        return None
    speaker_key = getattr(window, "partial_speaker_key", None)
    if speaker_key is not None:
        speaker_key = str(speaker_key)
    speaker_name = getattr(window, "partial_speaker_name", None)
    if speaker_name is not None:
        speaker_name = str(speaker_name)
    if speaker_key and not speaker_name:
        raw = speaker_key.rsplit(":", 1)[-1].removeprefix("s")
        speaker_name = f"说话人 {raw}" if raw.isdigit() else None
    return {
        "text": text,
        "speaker_key": speaker_key,
        "speaker_name": speaker_name,
    }


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
