from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status

from voice_realtime.meeting.api import MeetingAPIError

from .repository import InnerOSExchangeRepository


def _json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    return value


def _public(exchange: dict[str, Any]) -> dict[str, Any]:
    return {key: _json(value) for key, value in exchange.items()}


def install_inner_os_api(app: Any) -> APIRouter:
    router = APIRouter(prefix="/api/v1/meetings/{meeting_id}/inner-os")

    def repositories(request: Request) -> tuple[Any, Any, InnerOSExchangeRepository]:
        cfg = request.app.state.settings
        if not cfg.meeting.inner_os_enabled:
            raise MeetingAPIError(
                "inner_os_not_found", "内心 OS 未启用", status_code=404
            )
        service = getattr(request.app.state, "inner_os_service", None)
        meeting_repo = getattr(request.app.state, "meeting_repository", None)
        if service is None or meeting_repo is None:
            raise MeetingAPIError(
                "inner_os_context_unavailable",
                "内心 OS 上下文暂不可用",
                status_code=503,
            )
        exchange_repo = getattr(request.app.state, "inner_os_exchange_repository", None)
        if exchange_repo is None:
            exchange_repo = InnerOSExchangeRepository(meeting_repo)
            request.app.state.inner_os_exchange_repository = exchange_repo
        return service, meeting_repo, exchange_repo

    @router.put("/exchanges/{exchange_id}", status_code=status.HTTP_201_CREATED)
    async def save_exchange(
        meeting_id: UUID,
        exchange_id: UUID,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        service, _, repository = repositories(request)
        existing = await repository.get(meeting_id, exchange_id)
        if existing is not None:
            response.status_code = status.HTTP_200_OK
            return _public(existing)
        exchange = service.peek_completed(exchange_id)
        if exchange is None or exchange["meeting_id"] != meeting_id:
            # Do not consume a completed result before the meeting ownership
            # check; a malformed cross-meeting request must be harmless.
            raise MeetingAPIError(
                "inner_os_not_found", "内心 OS 问答不存在", status_code=404
            )
        exchange = service.take_completed(exchange_id)
        if exchange is None or exchange["meeting_id"] != meeting_id:
            raise MeetingAPIError(
                "inner_os_not_found", "内心 OS 问答不存在", status_code=404
            )
        return _public(await repository.save(exchange))

    @router.get("/exchanges")
    async def list_exchanges(
        meeting_id: UUID,
        request: Request,
        cursor: str | None = None,
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        _, _, repository = repositories(request)
        try:
            items, next_cursor = await repository.list(meeting_id, cursor, limit)
        except (ValueError, KeyError, TypeError) as exc:
            raise MeetingAPIError(
                "inner_os_invalid_cursor", "分页游标无效", status_code=400
            ) from exc
        return {"items": [_public(item) for item in items], "next_cursor": next_cursor}

    @router.get("/exchanges/{exchange_id}")
    async def get_exchange(meeting_id: UUID, exchange_id: UUID, request: Request) -> dict[str, Any]:
        _, _, repository = repositories(request)
        exchange = await repository.get(meeting_id, exchange_id)
        if exchange is None:
            raise MeetingAPIError(
                "inner_os_not_found", "内心 OS 问答不存在", status_code=404
            )
        return _public(exchange)

    @router.delete("/exchanges/{exchange_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_exchange(meeting_id: UUID, exchange_id: UUID, request: Request) -> Response:
        _, _, repository = repositories(request)
        await repository.delete(meeting_id, exchange_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    app.include_router(router)
    return router
