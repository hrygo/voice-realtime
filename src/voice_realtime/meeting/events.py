"""会议助手 V1 WebSocket 事件广播与慢客户端恢复。

广播器不把浏览器连接当作事实源。每个客户端拥有独立的有界队列：partial
可以丢弃，任何 durable 事件在队列满时都会被替换成 ``resync_required``，
前端随后通过 HTTP 重新读取完整 transcript。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel

DURABLE_EVENT_TYPES = frozenset(
    {
        "meeting_state_changed",
        "transcript_reconciled",
        "speaker_updated",
        "meeting_title_updated",
        "minutes_state_changed",
        "health_changed",
        "transcription_gap",
        "resync_required",
    }
)

_REVISION_FIELDS = {
    "transcript_reconciled": ("transcript_revision", "content_revision"),
    "speaker_updated": ("content_revision",),
}


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        rendered = value.astimezone(UTC).isoformat()
        return rendered.replace("+00:00", "Z")
    return value


def make_event(event_type: str, meeting_id: str | UUID, payload: Any) -> dict[str, Any]:
    """创建统一 V1 envelope；公开 JSON 只使用字符串 UUID 和 UTC 时间。"""

    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {
        "contract_version": "1",
        "type": event_type,
        "event_id": str(uuid4()),
        "meeting_id": str(meeting_id),
        "occurred_at": now,
        "payload": _json_value(payload),
    }


class MeetingEventClient:
    """单个会议事件订阅者的有界队列。"""

    def __init__(self, queue_size: int = 64) -> None:
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max(1, queue_size))
        self.closed = False

    async def receive(self) -> dict[str, Any]:
        if self.closed and self.queue.empty():
            raise asyncio.CancelledError
        return await self.queue.get()

    def close(self) -> None:
        self.closed = True
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break


class MeetingEventBroadcaster:
    """面向会议事件的 bounded fan-out broadcaster。"""

    def __init__(
        self,
        *,
        queue_size: int = 64,
        snapshot_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.queue_size = max(1, queue_size)
        self.snapshot_factory = snapshot_factory
        self._clients: set[MeetingEventClient] = set()
        self._lock = asyncio.Lock()
        self._latest: dict[str, Any] | None = None
        self._revision_cursors: dict[str, dict[str, int]] = {}

    @property
    def clients(self) -> tuple[MeetingEventClient, ...]:
        return tuple(self._clients)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def add_test_client(self, *, queue_size: int | None = None) -> MeetingEventClient:
        """供 API/事件单元测试使用的内存订阅者。"""

        return self.add_client(queue_size=queue_size)

    def add_client(
        self,
        sender: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        *,
        queue_size: int | None = None,
    ) -> MeetingEventClient:
        # sender 参数保留给旧的广播适配器；HTTP WS 使用 receive()，避免
        # 广播任务被慢 socket 反压。
        del sender
        client = MeetingEventClient(queue_size or self.queue_size)
        self._clients.add(client)
        return client

    def remove_client(self, client: MeetingEventClient) -> None:
        self._clients.discard(client)
        client.close()

    def snapshot(self) -> dict[str, Any] | None:
        if self.snapshot_factory is not None:
            value = self.snapshot_factory()
            if inspect.isawaitable(value):
                # snapshot() 是同步握手 API；异步 provider 由 websocket
                # 调用 snapshot_async()，避免在这里偷偷创建未 await 的任务。
                close = getattr(value, "close", None)
                if close is not None:
                    close()
                return None
            normalized = _json_value(value)
            return normalized if isinstance(normalized, dict) else None
        normalized = _json_value(self._latest)
        return normalized if isinstance(normalized, dict) else None

    async def snapshot_async(self) -> dict[str, Any] | None:
        if self.snapshot_factory is not None:
            value = self.snapshot_factory()
            if inspect.isawaitable(value):
                value = await value
            normalized = _json_value(value)
            return normalized if isinstance(normalized, dict) else None
        normalized = _json_value(self._latest)
        return normalized if isinstance(normalized, dict) else None

    async def publish(self, event: Mapping[str, Any] | BaseModel) -> None:
        normalized = _json_value(event)
        if not isinstance(normalized, dict):
            raise TypeError("meeting event must be an object")
        async with self._lock:
            accepted, replacement = self._accept_revision_event(normalized)
            if not accepted:
                return
            event = replacement or normalized
            event_type = str(event.get("type", ""))
            if event_type != "resync_required":
                self._latest = event
            durable = event_type in DURABLE_EVENT_TYPES
            for client in tuple(self._clients):
                if client.closed:
                    self._clients.discard(client)
                    continue
                self._enqueue(client, event, durable=durable)

    async def publish_event(self, event_type: str, meeting_id: str | UUID, payload: Any) -> None:
        await self.publish(make_event(event_type, meeting_id, payload))

    async def observe_snapshot(self, event: Mapping[str, Any] | BaseModel) -> None:
        """Seed revision cursors from a snapshot sent outside the event queue."""

        normalized = _json_value(event)
        if not isinstance(normalized, dict):
            raise TypeError("meeting snapshot must be an object")
        if normalized.get("type") != "meeting_snapshot":
            raise ValueError("revision cursor can only be seeded by meeting_snapshot")
        async with self._lock:
            self._seed_revision_cursor(
                str(normalized.get("meeting_id", "")),
                normalized.get("payload"),
            )

    def _enqueue(self, client: MeetingEventClient, event: dict[str, Any], *, durable: bool) -> None:
        try:
            client.queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            if not durable:
                # partial 是易失数据，慢客户端可安全丢弃。
                return

        # A resync request is already the recovery signal.  Preserve its
        # reason when a client is full instead of replacing it with a generic
        # client_queue_overflow reason.
        if event.get("type") == "resync_required":
            while True:
                try:
                    client.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            with contextlib.suppress(asyncio.QueueFull):
                client.queue.put_nowait(event)
            return

        expected_revision = None
        payload = event.get("payload")
        if isinstance(payload, Mapping):
            expected_revision = payload.get("transcript_revision") or payload.get(
                "content_revision"
            )
        while True:
            try:
                client.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        resync = make_event(
            "resync_required",
            event.get("meeting_id", "unknown"),
            {
                "expected_revision": expected_revision,
                "reason": "client_queue_overflow",
            },
        )
        with contextlib.suppress(asyncio.QueueFull):
            client.queue.put_nowait(resync)

    def _accept_revision_event(
        self, event: dict[str, Any]
    ) -> tuple[bool, dict[str, Any] | None]:
        """Apply the producer-side revision gate for durable content events.

        The broadcaster can observe events from several async producers.  A
        revision jump means at least one durable event is missing, so sending
        the newer event would make a consumer build an incomplete transcript.
        In that case emit one recovery event and retain the previous latest
        snapshot.  Stale events are harmlessly ignored; the HTTP baseline is
        still the authority for recovery.
        """

        event_type = str(event.get("type", ""))
        meeting_id = str(event.get("meeting_id", ""))
        payload = event.get("payload")

        if event_type == "meeting_snapshot":
            self._seed_revision_cursor(meeting_id, payload)
            return True, None

        fields = _REVISION_FIELDS.get(event_type)
        if fields is None or not isinstance(payload, Mapping):
            return True, None

        incoming = {
            field: value
            for field in fields
            if isinstance(value := payload.get(field), int) and not isinstance(value, bool)
        }
        if not incoming:
            # Leave legacy/internal malformed payloads observable; the
            # contract test gate reports their schema violation with context.
            return True, None

        cursor = self._revision_cursors.setdefault(meeting_id, {})
        expected_revision: int | None = None
        stale = False
        for field, value in incoming.items():
            previous = cursor.get(field)
            if previous is None:
                continue
            if value > previous + 1:
                expected_revision = previous + 1
                break
            if value < previous:
                stale = True

        if expected_revision is not None:
            return True, make_event(
                "resync_required",
                meeting_id or "unknown",
                {"expected_revision": expected_revision, "reason": "revision_gap"},
            )
        if stale:
            return False, None

        cursor.update(incoming)
        return True, None

    def _seed_revision_cursor(self, meeting_id: str, payload: Any) -> None:
        if not isinstance(payload, Mapping):
            return
        meeting_payload = payload.get("meeting")
        if not meeting_id and isinstance(meeting_payload, Mapping):
            meeting_id = str(meeting_payload.get("id", ""))
        if not meeting_id:
            return
        cursor = self._revision_cursors.setdefault(meeting_id, {})
        for field in ("transcript_revision", "content_revision"):
            value = payload.get(field)
            if not isinstance(value, int) or isinstance(value, bool):
                continue
            cursor[field] = value


__all__ = [
    "DURABLE_EVENT_TYPES",
    "MeetingEventBroadcaster",
    "MeetingEventClient",
    "make_event",
]
