"""Neutral Realtime v2 transport validation shared by ASR and TTS adapters."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Protocol, cast

import websockets


class SpeechRailProtocolError(RuntimeError):
    """A remote Realtime v2 event violated the public protocol."""


class SpeechRailConnection(Protocol):
    """The minimal WebSocket surface used by outbound SpeechRail adapters."""

    uri: str

    async def send(self, payload: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self) -> None: ...


ConnectionFactory = Callable[[str], Awaitable[SpeechRailConnection]]


def _connect(url: str) -> Awaitable[SpeechRailConnection]:
    return cast(Awaitable[SpeechRailConnection], websockets.connect(url))


class SpeechRailV2Transport:
    """Validate shared v2 envelopes without coupling to an ASR or TTS lifecycle."""

    def __init__(
        self, *, url: str, connection_factory: ConnectionFactory | None = None
    ) -> None:
        self._url = url
        self._connection_factory = connection_factory or _connect
        self._connection: SpeechRailConnection | None = None
        self._sequence = 0
        self._session_id: str | None = None
        self._request_id: str | None = None

    @property
    def uri(self) -> str:
        return self._connection.uri if self._connection is not None else self._url

    async def connect(self) -> None:
        if self._connection is not None:
            raise RuntimeError("SPEECHRAIL_ALREADY_CONNECTED")
        self._sequence = 0
        self._session_id = None
        self._request_id = None
        self._connection = await self._connection_factory(self._url)

    async def send_event(self, payload: dict[str, object]) -> None:
        if self._connection is None:
            raise RuntimeError("SPEECHRAIL_NOT_CONNECTED")
        await self._connection.send(json.dumps(payload, separators=(",", ":")))

    async def receive(self) -> dict[str, object]:
        if self._connection is None:
            raise RuntimeError("SPEECHRAIL_NOT_CONNECTED")
        try:
            payload = json.loads(await self._connection.recv())
        except (json.JSONDecodeError, TypeError) as exc:
            raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR") from exc
        if not isinstance(payload, dict):
            raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR")
        event_type = payload.get("type")
        session_id = payload.get("session_id")
        request_id = payload.get("request_id")
        sequence = payload.get("sequence")
        if (
            not isinstance(event_type, str)
            or not event_type
            or not isinstance(session_id, str)
            or not session_id
            or not isinstance(request_id, str)
            or not request_id
            or not isinstance(sequence, int)
        ):
            raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR")
        if sequence <= self._sequence:
            raise SpeechRailProtocolError("SPEECHRAIL_SEQUENCE_ERROR")
        if self._session_id is None:
            self._session_id = session_id
            self._request_id = request_id
        elif session_id != self._session_id:
            raise SpeechRailProtocolError("SPEECHRAIL_SESSION_MISMATCH")
        elif request_id != self._request_id:
            raise SpeechRailProtocolError("SPEECHRAIL_REQUEST_MISMATCH")
        self._sequence = sequence
        return {str(key): value for key, value in payload.items()}

    async def close(self) -> None:
        if self._connection is not None:
            try:
                await self._connection.close()
            finally:
                self._connection = None
                self._session_id = None
                self._request_id = None
