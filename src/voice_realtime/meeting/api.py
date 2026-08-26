"""会议助手 V1 HTTP API。

此路由层只做边界校验、稳定错误映射和公开 JSON 投影。Repository、运行模式
和纪要 worker 通过 ``app.state`` 注入，前端不依赖其内部实现或数据库字段。
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, FastAPI, Header, Query, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

from voice_realtime.meeting.models import MeetingStatus

_TITLE = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
_DISPLAY_NAME = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
_SAFE_FILENAME = re.compile(r"[^\w\-\u4e00-\u9fff .]", re.UNICODE)


class MeetingAPIError(RuntimeError):
    """映射到稳定 HTTP error envelope 的业务错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 500,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = dict(details or {})


class MeetingTitleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: _TITLE


class SpeakerNameUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: _DISPLAY_NAME


class MeetingAPIErrorEnvelope(BaseModel):
    error: dict[str, Any]


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        normalized = value.astimezone(UTC).isoformat()
        return normalized.replace("+00:00", "Z")
    return _enum_value(value)


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _uuid(value: Any) -> str:
    return str(value) if value is not None else ""


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        rendered = value.astimezone(UTC).isoformat()
        return rendered.replace("+00:00", "Z")
    return str(value)


def _speaker_json(speaker: Any, *, speaker_key: str | None = None) -> dict[str, Any]:
    key = str(_attr(speaker, "speaker_key", speaker_key or ""))
    raw = str(_attr(speaker, "original_speaker", _attr(speaker, "raw_speaker", key)))
    fallback_label = (
        f"说话人 {raw.removeprefix('s')}"
        if raw.removeprefix("s").isdigit()
        else f"说话人 {key}"
    )
    default_label = str(_attr(speaker, "default_label", fallback_label))
    return {
        "speaker_key": key,
        "original_speaker": _attr(speaker, "original_speaker", _attr(speaker, "raw_speaker")),
        "default_label": default_label,
        "display_name": str(_attr(speaker, "display_name", default_label)),
        "updated_at": _timestamp(_attr(speaker, "updated_at"))
        or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def _speaker_map(value: Any) -> dict[str, dict[str, Any]]:
    if isinstance(value, Mapping):
        return {str(key): _speaker_json(item, speaker_key=str(key)) for key, item in value.items()}
    return {
        str(_attr(item, "speaker_key")): _speaker_json(item)
        for item in (value or ())
        if _attr(item, "speaker_key") is not None
    }


def _minutes_json(minutes: Any, *, meeting: Any | None = None) -> dict[str, Any] | None:
    if minutes is None:
        return None
    content = _attr(minutes, "content_json")
    source_revision = int(_attr(minutes, "source_content_revision", 0) or 0)
    current_revision = int(_attr(meeting, "content_revision", source_revision) or source_revision)
    return {
        "id": _uuid(_attr(minutes, "id")),
        "meeting_id": _uuid(_attr(minutes, "meeting_id", _attr(meeting, "id"))),
        "version": int(_attr(minutes, "version", 1) or 1),
        "status": str(_enum_value(_attr(minutes, "status", "queued"))),
        "source_content_revision": source_revision,
        "model": str(_attr(minutes, "model", "")),
        "prompt_version": _attr(minutes, "prompt_version"),
        "content_json": _jsonable(content) if content is not None else None,
        "content_markdown": _attr(minutes, "content_markdown"),
        "raw_output": _attr(minutes, "raw_output"),
        "error_code": _attr(minutes, "error_code"),
        "error_message": _attr(minutes, "error_message"),
        "created_at": _timestamp(_attr(minutes, "created_at")),
        "is_stale": source_revision < current_revision,
    }


def meeting_summary_json(meeting: Any) -> dict[str, Any]:
    return {
        "id": _uuid(_attr(meeting, "id")),
        "title": str(_attr(meeting, "title", "")),
        "status": str(_enum_value(_attr(meeting, "status", "completed"))),
        "language": str(_attr(meeting, "language", "Chinese")),
        "started_at": _timestamp(_attr(meeting, "started_at")),
        "ended_at": _timestamp(_attr(meeting, "ended_at")),
        "transcript_revision": int(_attr(meeting, "transcript_revision", 0) or 0),
        "content_revision": int(_attr(meeting, "content_revision", 0) or 0),
        "interruption_reason": _attr(meeting, "interruption_reason"),
        "created_at": _timestamp(_attr(meeting, "created_at")),
    }


async def _meeting_detail(repository: Any, meeting: Any) -> dict[str, Any]:
    speakers = _attr(meeting, "speakers")
    if speakers is None:
        get_speakers = getattr(repository, "get_speakers", None)
        if get_speakers is not None:
            speakers = get_speakers(_attr(meeting, "id"))
            if inspect.isawaitable(speakers):
                speakers = await speakers
    if speakers is None:
        get_transcript = getattr(repository, "get_transcript", None)
        if get_transcript is not None:
            document = get_transcript(_attr(meeting, "id"))
            if inspect.isawaitable(document):
                document = await document
            speakers = _attr(document, "speakers", ())
    minutes = _attr(meeting, "latest_minutes")
    if minutes is None:
        get_latest = getattr(repository, "get_latest_minutes", None)
        if get_latest is not None:
            minutes = get_latest(_attr(meeting, "id"))
            if inspect.isawaitable(minutes):
                minutes = await minutes
    result = meeting_summary_json(meeting)
    result.update(
        {
            "audio_source": str(_attr(meeting, "audio_source", "microphone")),
            "metadata": _jsonable(_attr(meeting, "metadata", {})) or {},
            "speakers": _speaker_map(speakers or ()),
            "latest_minutes": _minutes_json(minutes, meeting=meeting),
            "updated_at": _timestamp(_attr(meeting, "updated_at")),
        }
    )
    return result


def _segment_json(segment: Any, speakers: Any) -> dict[str, Any]:
    key = str(_attr(segment, "speaker_key", ""))
    speaker_map = _speaker_map(speakers)
    speaker = speaker_map.get(key, {})
    raw_key = key.rsplit(":", 1)[-1].removeprefix("s")
    fallback_name = f"说话人 {raw_key}" if raw_key.isdigit() else key
    return {
        "id": _uuid(_attr(segment, "id")),
        "order": int(_attr(segment, "order", 0) or 0),
        "speaker_key": key,
        "speaker_name": speaker.get("display_name")
        or speaker.get("default_label")
        or fallback_name,
        "start_ms": int(_attr(segment, "start_ms", 0) or 0),
        "end_ms": int(_attr(segment, "end_ms", 0) or 0),
        "text": str(_attr(segment, "text", "")),
        "translation": _attr(segment, "translation"),
        "detected_language": _attr(segment, "detected_language"),
        "source_epoch": int(_attr(segment, "source_epoch", 0) or 0),
    }


def _transcript_json(document: Any) -> dict[str, Any]:
    segments = _attr(document, "segments", ()) or ()
    speakers = _attr(document, "speakers", ()) or ()
    return {
        "meeting_id": _uuid(_attr(document, "meeting_id")),
        "transcript_revision": int(_attr(document, "transcript_revision", 0) or 0),
        "content_revision": int(_attr(document, "content_revision", 0) or 0),
        "segments": [_segment_json(item, speakers) for item in segments],
    }


def _request_id(request: Request) -> str:
    return request.headers.get("X-Request-ID", str(uuid4()))[:128]


def _error_response(request: Request, error: MeetingAPIError) -> JSONResponse:
    body = {
        "error": {
            "code": error.code,
            "message": error.message,
            "request_id": _request_id(request),
            "details": error.details,
        }
    }
    return JSONResponse(status_code=error.status_code, content=body)


def _typed_error(exc: Exception) -> MeetingAPIError:
    if isinstance(exc, MeetingAPIError):
        return exc
    code = str(getattr(exc, "code", ""))
    class_name = type(exc).__name__.lower()
    if not code:
        if "invalid" in class_name or "cursor" in class_name or "validation" in class_name:
            code = "invalid_request"
        elif "notfound" in class_name or "not_found" in class_name:
            code = "not_found"
        elif "conflict" in class_name or "already" in class_name:
            code = "conflict"
        elif (
            "storage" in class_name
            or "database" in class_name
            or "unavailable" in class_name
        ):
            code = "storage_unavailable"
        else:
            code = "internal_error"
    status = {
        "invalid_request": 400,
        "not_found": 404,
        "conflict": 409,
        "storage_unavailable": 503,
        "transcription_unavailable": 503,
        "mode_conflict": 409,
        "meeting_not_active": 409,
        "finalization_timeout": 504,
        "summary_unavailable": 503,
        "service_unavailable": 503,
        "internal_error": 500,
    }.get(code, 500)
    public_message = {
        "not_found": "会议或资源不存在",
        "storage_unavailable": "会议存储暂不可用",
        "summary_unavailable": "AI 纪要模型服务不可用",
        "service_unavailable": "服务暂不可用",
        "conflict": "操作冲突",
    }.get(code, "请求处理失败")
    return MeetingAPIError(code, public_message, status_code=status)


def _repository(request: Request, explicit: Any) -> Any:
    repository = explicit or getattr(request.app.state, "meeting_repository", None)
    if repository is None:
        raise MeetingAPIError("storage_unavailable", "会议存储暂不可用", status_code=503)
    return repository


def _runtime(request: Request, explicit: Any) -> Any:
    return (
        explicit
        or getattr(request.app.state, "meeting_runtime", None)
        or getattr(request.app.state, "runtime", None)
    )


def _summary_service(request: Request, explicit: Any) -> Any:
    return (
        explicit
        or getattr(request.app.state, "meeting_summary_service", None)
        or getattr(request.app.state, "summary_service", None)
    )


async def _publish_event(
    request: Request,
    event_type: str,
    meeting_id: UUID,
    payload: Mapping[str, Any],
) -> None:
    broadcaster = getattr(request.app.state, "meeting_events", None)
    publish = getattr(broadcaster, "publish_event", None)
    if publish is None:
        return
    result = publish(event_type, meeting_id, payload)
    if inspect.isawaitable(result):
        await result


def create_meeting_router(
    *,
    repository: Any | None = None,
    runtime: Any | None = None,
    summary_service: Any | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["meeting-assistant-v1"])

    @router.get("/runtime")
    async def get_runtime(request: Request) -> dict[str, Any]:
        current = _runtime(request, runtime)
        if current is None or not hasattr(current, "snapshot"):
            raise MeetingAPIError("service_unavailable", "运行时尚未就绪", status_code=503)
        normalized = _jsonable(current.snapshot())
        if not isinstance(normalized, dict):
            raise MeetingAPIError("internal_error", "运行时状态格式无效", status_code=500)
        return normalized

    @router.get("/meetings")
    async def list_meetings(
        request: Request,
        cursor: str | None = Query(default=None, max_length=256),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        repo = _repository(request, repository)
        try:
            page = await repo.list_meetings(cursor=cursor, limit=limit)
            return {
                "items": [meeting_summary_json(item) for item in (_attr(page, "items", ()) or ())],
                "next_cursor": _attr(page, "next_cursor"),
            }
        except Exception as exc:
            raise _typed_error(exc) from exc

    @router.get("/meetings/{meeting_id}")
    async def get_meeting(request: Request, meeting_id: UUID) -> dict[str, Any]:
        repo = _repository(request, repository)
        try:
            meeting = await repo.get_meeting(meeting_id)
            if meeting is None:
                raise MeetingAPIError("not_found", "会议或资源不存在", status_code=404)
            return await _meeting_detail(repo, meeting)
        except Exception as exc:
            raise _typed_error(exc) from exc

    @router.get("/meetings/{meeting_id}/transcript")
    async def get_transcript(request: Request, meeting_id: UUID) -> dict[str, Any]:
        repo = _repository(request, repository)
        try:
            document = await repo.get_transcript(meeting_id)
            return _transcript_json(document)
        except Exception as exc:
            raise _typed_error(exc) from exc

    @router.patch("/meetings/{meeting_id}")
    async def update_meeting(
        request: Request,
        meeting_id: UUID,
        body: MeetingTitleUpdate,
    ) -> dict[str, Any]:
        repo = _repository(request, repository)
        method = getattr(repo, "update_title", None) or getattr(repo, "rename_meeting", None)
        if method is None:
            raise MeetingAPIError("internal_error", "会议标题更新不可用", status_code=500)
        try:
            meeting = method(meeting_id, body.title)
            if inspect.isawaitable(meeting):
                meeting = await meeting
            await _publish_event(
                request,
                "meeting_title_updated",
                meeting_id,
                {"title": str(_attr(meeting, "title", body.title))},
            )
            return await _meeting_detail(repo, meeting)
        except Exception as exc:
            raise _typed_error(exc) from exc

    @router.api_route("/meetings/{meeting_id}/generate-title", methods=["POST", "PATCH"])
    async def generate_meeting_title(
        request: Request,
        meeting_id: UUID,
    ) -> dict[str, Any]:
        repo = _repository(request, repository)
        summary_srv = _summary_service(request, summary_service)
        client = getattr(summary_srv, "client", None)
        try:
            document = await repo.get_transcript(meeting_id)
            segments = tuple(_attr(document, "segments", ()) or ())
            if not segments:
                raise MeetingAPIError(
                    "invalid_request", "会议没有可生成标题的转录内容", status_code=400
                )
            speakers = _attr(document, "speakers", ())
            if client is not None and hasattr(client, "generate_title"):
                title = await client.generate_title(document, speakers)
            else:
                first_text = str(_attr(segments[0], "text", "")).strip()
                title = first_text[:20] or "AI 会议纪要"
            method = getattr(repo, "update_title", None) or getattr(repo, "rename_meeting", None)
            if method is None:
                raise MeetingAPIError("internal_error", "会议标题更新不可用", status_code=500)
            meeting = method(meeting_id, title)
            if inspect.isawaitable(meeting):
                meeting = await meeting
            await _publish_event(
                request,
                "meeting_title_updated",
                meeting_id,
                {"title": str(_attr(meeting, "title", title))},
            )
            return await _meeting_detail(repo, meeting)
        except MeetingAPIError:
            raise
        except Exception as exc:
            raise _typed_error(exc) from exc


    @router.patch("/meetings/{meeting_id}/speakers/{speaker_key}")
    async def update_speaker(
        request: Request,
        meeting_id: UUID,
        speaker_key: str,
        body: SpeakerNameUpdate,
    ) -> dict[str, Any]:
        if len(speaker_key) > 200 or any(ord(char) < 0x20 for char in speaker_key):
            raise MeetingAPIError("invalid_request", "speaker_key 无效", status_code=400)
        repo = _repository(request, repository)
        try:
            meeting_or_speaker = await repo.rename_speaker(
                meeting_id,
                speaker_key,
                body.display_name,
            )
            if _attr(meeting_or_speaker, "speaker_key") == speaker_key:
                return _speaker_json(meeting_or_speaker, speaker_key=speaker_key)
            speakers = _attr(meeting_or_speaker, "speakers")
            if speakers is None:
                get_speakers = getattr(repo, "get_speakers", None)
                if get_speakers is not None:
                    speakers = get_speakers(meeting_id)
                    if inspect.isawaitable(speakers):
                        speakers = await speakers
            if speakers is None:
                document = await repo.get_transcript(meeting_id)
                speakers = _attr(document, "speakers", ())
            speaker = (
                speakers.get(speaker_key)
                if isinstance(speakers, Mapping)
                else next(
                    (item for item in speakers or () if _attr(item, "speaker_key") == speaker_key),
                    None,
                )
            )
            if speaker is None:
                raise MeetingAPIError("not_found", "说话人不存在", status_code=404)
            response = _speaker_json(speaker, speaker_key=speaker_key)
            await _publish_event(
                request,
                "speaker_updated",
                meeting_id,
                {
                    "speaker_key": speaker_key,
                    "display_name": response["display_name"],
                    "content_revision": int(
                        _attr(meeting_or_speaker, "content_revision", 0) or 0
                    ),
                },
            )
            return response
        except Exception as exc:
            raise _typed_error(exc) from exc

    @router.post("/meetings/{meeting_id}/minutes")
    async def create_minutes(
        request: Request,
        meeting_id: UUID,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
    ) -> dict[str, Any]:
        repo = _repository(request, repository)
        try:
            minutes = await repo.create_minutes(meeting_id, idempotency_key=idempotency_key)
            result = _minutes_json(minutes)
            if result is None:
                raise MeetingAPIError("internal_error", "纪要响应格式无效", status_code=500)
            await _publish_event(
                request,
                "minutes_state_changed",
                meeting_id,
                {
                    "minutes_id": result["id"],
                    "version": result["version"],
                    "status": result["status"],
                    "error_code": result["error_code"],
                    "error_message": result["error_message"],
                    "minutes": result,
                },
            )
            return result
        except Exception as exc:
            raise _typed_error(exc) from exc

    @router.get("/meetings/{meeting_id}/minutes/{version}")
    async def get_minutes(request: Request, meeting_id: UUID, version: int) -> dict[str, Any]:
        if version < 1:
            raise MeetingAPIError("invalid_request", "version 必须为正整数", status_code=400)
        repo = _repository(request, repository)
        method = getattr(repo, "get_minutes", None) or getattr(repo, "get_minutes_version", None)
        if method is None:
            raise MeetingAPIError("not_found", "纪要版本不存在", status_code=404)
        try:
            minutes = method(meeting_id, version)
            if inspect.isawaitable(minutes):
                minutes = await minutes
            if minutes is None:
                raise MeetingAPIError("not_found", "纪要版本不存在", status_code=404)
            meeting = await repo.get_meeting(meeting_id)
            return _minutes_json(minutes, meeting=meeting) or {}
        except Exception as exc:
            raise _typed_error(exc) from exc

    @router.get("/meetings/{meeting_id}/export")
    async def export_meeting(
        request: Request,
        meeting_id: UUID,
        export_format: Literal["md", "txt", "srt", "json"] = Query(
            default="md", alias="format"
        ),
    ) -> Response:
        repo = _repository(request, repository)
        try:
            meeting = await repo.get_meeting(meeting_id)
            if meeting is None:
                raise MeetingAPIError("not_found", "会议或资源不存在", status_code=404)
            document = await repo.get_transcript(meeting_id)
            transcript = _transcript_json(document)
            if export_format == "json":
                body = json.dumps(
                    {"meeting": await _meeting_detail(repo, meeting), "transcript": transcript},
                    ensure_ascii=False,
                    indent=2,
                )
                media_type = "application/json"
            elif export_format == "srt":
                body = _render_srt(transcript)
                media_type = "application/x-subrip"
            else:
                body = _render_text_export(
                    meeting,
                    transcript,
                    markdown=export_format == "md",
                )
                media_type = (
                    "text/markdown; charset=utf-8"
                    if export_format == "md"
                    else "text/plain; charset=utf-8"
                )
            filename = _safe_filename(str(_attr(meeting, "title", "meeting"))) or "meeting"
            return Response(
                content=body,
                media_type=media_type,
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="{filename}.{export_format}"'
                    )
                },
            )
        except Exception as exc:
            if isinstance(exc, MeetingAPIError):
                raise
            raise _typed_error(exc) from exc

    @router.delete("/meetings/{meeting_id}", status_code=204)
    async def delete_meeting(request: Request, meeting_id: UUID) -> Response:
        repo = _repository(request, repository)
        try:
            meeting = await repo.get_meeting(meeting_id)
            if meeting is None:
                raise MeetingAPIError("not_found", "会议或资源不存在", status_code=404)
            status = str(_enum_value(_attr(meeting, "status", "")))
            if status in {MeetingStatus.RECORDING.value, MeetingStatus.FINALIZING.value}:
                raise MeetingAPIError("conflict", "录制中的会议不能删除", status_code=409)
            await repo.delete_meeting(meeting_id)
            return Response(status_code=204)
        except Exception as exc:
            raise _typed_error(exc) from exc

    return router


def _render_text_export(meeting: Any, transcript: Mapping[str, Any], *, markdown: bool) -> str:
    title = str(_attr(meeting, "title", "会议"))
    lines = [f"# {title}" if markdown else title, ""]
    for segment in transcript.get("segments", []):
        start = int(segment["start_ms"])
        seconds, millis = divmod(start, 1000)
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        stamp = f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"
        prefix = f"- [{stamp}]" if markdown else f"[{stamp}]"
        lines.append(f"{prefix} {segment['speaker_name']}: {segment['text']}")
    return "\n".join(lines).rstrip() + "\n"


def _render_srt(transcript: Mapping[str, Any]) -> str:
    def stamp(ms: int) -> str:
        seconds, millis = divmod(max(0, ms), 1000)
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"

    blocks: list[str] = []
    for index, segment in enumerate(transcript.get("segments", []), start=1):
        blocks.extend(
            [
                str(index),
                f"{stamp(int(segment['start_ms']))} --> {stamp(int(segment['end_ms']))}",
                f"{segment['speaker_name']}: {segment['text']}",
                "",
            ]
        )
    return "\n".join(blocks)


def _safe_filename(title: str) -> str:
    title = _SAFE_FILENAME.sub("_", title).strip(" .")
    return title[:120]


async def _meeting_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return _error_response(request, _typed_error(exc))


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not request.url.path.startswith("/api/v1/"):
        return await request_validation_exception_handler(
            request,
            cast(RequestValidationError, exc),
        )
    return _error_response(
        request,
        MeetingAPIError("invalid_request", "请求参数不符合规范", status_code=422),
    )


def install_meeting_api(
    app: FastAPI,
    *,
    repository: Any | None = None,
    runtime: Any | None = None,
    summary_service: Any | None = None,
) -> APIRouter:
    """将 canonical v1 HTTP 路由和统一错误处理安装到 FastAPI 应用。"""

    if repository is not None:
        app.state.meeting_repository = repository
    if runtime is not None:
        app.state.meeting_runtime = runtime
    if summary_service is not None:
        app.state.meeting_summary_service = summary_service
    if getattr(app.state, "meeting_api_installed", False):
        return cast(APIRouter, app.state.meeting_api_router)
    router = create_meeting_router(
        repository=repository, runtime=runtime, summary_service=summary_service
    )
    app.include_router(router)
    app.add_exception_handler(MeetingAPIError, _meeting_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.state.meeting_api_installed = True
    app.state.meeting_api_router = router
    return router


__all__ = [
    "MeetingAPIError",
    "MeetingAPIErrorEnvelope",
    "MeetingTitleUpdate",
    "SpeakerNameUpdate",
    "create_meeting_router",
    "install_meeting_api",
    "meeting_summary_json",
]
