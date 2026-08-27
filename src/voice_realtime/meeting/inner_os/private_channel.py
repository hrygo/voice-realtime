"""Strict command and connection state for the loopback-only Inner OS channel."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

InnerOSIntent = Literal["fact", "analysis", "draft", "mixed"]
InnerOSEventType = Literal[
    "inner_os_query_accepted",
    "inner_os_answer_started",
    "inner_os_answer_completed",
    "inner_os_answer_failed",
    "inner_os_answer_cancelled",
]
_TERMINAL_EVENTS = frozenset(
    {
        "inner_os_answer_completed",
        "inner_os_answer_failed",
        "inner_os_answer_cancelled",
    }
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InnerOSEphemeralContext(_StrictModel):
    goal: str | None = Field(default=None, max_length=1_000)
    agenda: str | None = Field(default=None, max_length=1_000)
    background: str | None = Field(default=None, max_length=2_000)

    @field_validator("goal", "agenda", "background")
    @classmethod
    def _strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class InnerOSQueryCommand(_StrictModel):
    contract_version: Literal["1"]
    request_id: str = Field(min_length=1, max_length=64)
    cmd: Literal["query"]
    query_id: UUID | None = None
    meeting_id: UUID
    question: str = Field(min_length=1, max_length=2_000)
    intent: InnerOSIntent
    context_version: int = Field(ge=0)
    ephemeral_context: InnerOSEphemeralContext | None = None
    focus_segment_ids: tuple[UUID, ...] = Field(default=(), max_length=32)

    @field_validator("request_id", "question")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @model_validator(mode="after")
    def _unique_focus_segments(self) -> InnerOSQueryCommand:
        if len(set(self.focus_segment_ids)) != len(self.focus_segment_ids):
            raise ValueError("focus_segment_ids must be unique")
        return self


class InnerOSCancelCommand(_StrictModel):
    contract_version: Literal["1"]
    request_id: str = Field(min_length=1, max_length=64)
    cmd: Literal["cancel"]
    query_id: UUID

    @field_validator("request_id")
    @classmethod
    def _strip_request_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("request_id must not be blank")
        return normalized


InnerOSCommand = Annotated[
    InnerOSQueryCommand | InnerOSCancelCommand,
    Field(discriminator="cmd"),
]
_COMMAND_ADAPTER: TypeAdapter[InnerOSQueryCommand | InnerOSCancelCommand] = TypeAdapter(
    InnerOSCommand
)


class InnerOSChannelError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def parse_inner_os_command(raw: str | bytes) -> InnerOSQueryCommand | InnerOSCancelCommand:
    try:
        return _COMMAND_ADAPTER.validate_json(raw)
    except (ValidationError, ValueError, TypeError) as exc:
        raise InnerOSChannelError("inner_os_invalid_request") from exc


class InnerOSQueryServicePort(Protocol):
    async def start_query(self, **kwargs: Any) -> UUID: ...

    async def cancel(self, query_id: UUID, **kwargs: Any) -> bool: ...


EventSender = Callable[[dict[str, Any]], Awaitable[None] | None]


class InnerOSConnectionSession:
    """One connection, one active query, with stable request/query correlation."""

    def __init__(
        self,
        *,
        meeting_id: UUID,
        service: InnerOSQueryServicePort,
        send: EventSender,
        analysis_enabled: bool,
        cancel_timeout_secs: float,
    ) -> None:
        self.meeting_id = meeting_id
        self._service = service
        self._send = send
        self._analysis_enabled = analysis_enabled
        self._cancel_timeout_secs = cancel_timeout_secs
        self.active_query: UUID | None = None
        self._active_request_id: str | None = None

    async def handle_text(self, raw: str | bytes) -> None:
        command = parse_inner_os_command(raw)
        if isinstance(command, InnerOSCancelCommand):
            if self.active_query is None or command.query_id != self.active_query:
                raise InnerOSChannelError("inner_os_invalid_request")
            await self._service.cancel(
                command.query_id,
                timeout_secs=self._cancel_timeout_secs,
            )
            return
        if self.active_query is not None:
            raise InnerOSChannelError("inner_os_busy")
        if command.meeting_id != self.meeting_id:
            raise InnerOSChannelError("inner_os_context_unavailable")
        if command.intent in {"analysis", "mixed"} and not self._analysis_enabled:
            raise InnerOSChannelError("inner_os_intent_disabled")
        query_id = command.query_id or uuid4()
        self.active_query = query_id
        self._active_request_id = command.request_id
        try:
            await self._service.start_query(
                meeting_id=self.meeting_id,
                query_id=query_id,
                question=command.question,
                intent=command.intent,
                focus_segment_ids=command.focus_segment_ids,
                ephemeral_context=(
                    command.ephemeral_context.model_dump(exclude_none=True)
                    if command.ephemeral_context is not None
                    else None
                ),
                emit=self.emit,
            )
        except BaseException:
            self.active_query = None
            self._active_request_id = None
            raise

    async def emit(
        self,
        event_type: InnerOSEventType | str,
        query_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        if query_id != self.active_query:
            return
        envelope: dict[str, Any] = {
            "contract_version": "1",
            "type": event_type,
            "event_id": str(uuid4()),
            "meeting_id": str(self.meeting_id),
            "query_id": str(query_id),
            "request_id": self._active_request_id,
            "occurred_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "payload": payload,
        }
        result = self._send(envelope)
        if inspect.isawaitable(result):
            await result
        if event_type in _TERMINAL_EVENTS:
            self.active_query = None
            self._active_request_id = None

    async def close(self) -> None:
        if self.active_query is None:
            return
        query_id = self.active_query
        self.active_query = None
        self._active_request_id = None
        await self._service.cancel(
            query_id,
            reason="connection_closed",
            timeout_secs=self._cancel_timeout_secs,
        )


__all__ = [
    "InnerOSCancelCommand",
    "InnerOSChannelError",
    "InnerOSConnectionSession",
    "InnerOSQueryCommand",
    "parse_inner_os_command",
]
