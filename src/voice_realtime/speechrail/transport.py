"""Shared, vendor-neutral SpeechRail Realtime v2 transport adapter."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol, cast

import websockets


class SpeechRailConnection(Protocol):
    uri: str

    async def send(self, payload: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self) -> None: ...


ConnectionFactory = Callable[[str], Awaitable[SpeechRailConnection]]


def _connect(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> Awaitable[SpeechRailConnection]:
    return cast(
        Awaitable[SpeechRailConnection],
        websockets.connect(url, additional_headers=headers or None),
    )


class SpeechRailProtocolError(RuntimeError):
    """Stable local error for malformed or rejected SpeechRail events."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


class SpeechRailV2Transport:
    """Validate common v2 envelopes while leaving ASR/TTS semantics to adapters."""

    def __init__(
        self,
        *,
        url: str,
        api_key: str | None = None,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self._url = url
        normalized_key = api_key.strip() if isinstance(api_key, str) else None
        self._connection_factory = connection_factory or (
            lambda target: _connect(
                target,
                headers=(
                    {"Authorization": f"Bearer {normalized_key}"}
                    if normalized_key
                    else None
                ),
            )
        )
        self._connection: SpeechRailConnection | None = None
        self._sequence = 0
        self._session_id: str | None = None
        self._request_id: str | None = None

    @property
    def uri(self) -> str:
        return self._connection.uri if self._connection is not None else self._url

    async def connect(self) -> None:
        """Open the socket without sending a protocol-specific session update."""
        await self._open_connection()

    async def _open_connection(self) -> None:
        if self._connection is not None:
            raise RuntimeError("SPEECHRAIL_ALREADY_CONNECTED")
        self._sequence = 0
        self._session_id = None
        self._request_id = None
        self._connection = await self._connection_factory(self._url)

    async def connect_session(self, session: Mapping[str, object]) -> dict[str, object]:
        await self._open_connection()
        try:
            await self.send_event({"type": "session.update", "session": dict(session)})
            return await self.receive()
        except BaseException:
            await self.close()
            raise

    async def send_event(self, payload: Mapping[str, object]) -> None:
        if self._connection is None:
            raise RuntimeError("SPEECHRAIL_NOT_CONNECTED")
        await self._connection.send(json.dumps(dict(payload), separators=(",", ":")))

    async def receive(self) -> dict[str, object]:
        if self._connection is None:
            raise RuntimeError("SPEECHRAIL_NOT_CONNECTED")
        try:
            payload = json.loads(await self._connection.recv())
        except (json.JSONDecodeError, TypeError) as exc:
            raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR") from exc
        if not isinstance(payload, dict):
            raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR")
        sequence = payload.get("sequence")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence <= self._sequence
        ):
            raise SpeechRailProtocolError("SPEECHRAIL_SEQUENCE_ERROR")
        event_type = payload.get("type")
        session_id = payload.get("session_id")
        request_id = payload.get("request_id")
        if (
            not isinstance(event_type, str)
            or not isinstance(session_id, str)
            or not session_id
            or not isinstance(request_id, str)
            or not request_id
        ):
            raise SpeechRailProtocolError("SPEECHRAIL_PROTOCOL_ERROR")
        if self._session_id is None:
            if event_type != "session.created":
                raise SpeechRailProtocolError("SPEECHRAIL_SESSION_CREATE_FAILED")
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
                self._sequence = 0


class SpeechRailRealtimeClient:
    """Backward-compatible transcription client built on the neutral transport."""

    def __init__(
        self,
        *,
        url: str,
        api_key: str | None = None,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self._transport = SpeechRailV2Transport(
            url=url,
            api_key=api_key,
            connection_factory=connection_factory,
        )

    async def connect(
        self,
        *,
        language: str,
        diarization: bool = False,
        speaker_count_hint: int | None = None,
        diarization_group_id: str | None = None,
    ) -> None:
        session: dict[str, object] = {
            "type": "transcription",
            "language": language,
            "audio_format": {
                "type": "audio/pcm",
                "rate": 16_000,
                "channels": 1,
                "sample_width": 2,
            },
            "endpointing": {"mode": "manual"},
        }
        if diarization:
            diarization_config: dict[str, object] = {"enabled": True, "finalize": True}
            if speaker_count_hint is not None:
                diarization_config["speaker_count_hint"] = speaker_count_hint
            if diarization_group_id is not None:
                diarization_config["group_id"] = diarization_group_id
            session["diarization"] = diarization_config
        await self._transport.connect_session(session)

    @property
    def uri(self) -> str:
        return self._transport.uri

    async def append_pcm(self, chunk: bytes) -> None:
        if not chunk or len(chunk) % 2:
            raise ValueError("PCM must be non-empty int16")
        await self._transport.send_event(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(chunk).decode("ascii"),
            }
        )

    async def flush(self) -> None:
        await self._transport.send_event({"type": "input_audio_buffer.flush"})

    async def commit(self) -> None:
        await self._transport.send_event({"type": "input_audio_buffer.commit"})

    async def receive(self) -> dict[str, object]:
        return await self._transport.receive()

    async def cancel(self) -> None:
        await self._transport.send_event({"type": "session.cancel"})

    async def close(self) -> None:
        await self._transport.close()


def decode_pcm16(value: object) -> bytes:
    """Decode a v2 audio field at the outbound boundary."""

    if not isinstance(value, str) or not value:
        raise SpeechRailProtocolError("SPEECHRAIL_AUDIO_ERROR")
    try:
        audio = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise SpeechRailProtocolError("SPEECHRAIL_AUDIO_ERROR") from exc
    if not audio or len(audio) % 2:
        raise SpeechRailProtocolError("SPEECHRAIL_AUDIO_ERROR")
    return audio


__all__ = [
    "ConnectionFactory",
    "SpeechRailConnection",
    "SpeechRailProtocolError",
    "SpeechRailRealtimeClient",
    "SpeechRailV2Transport",
    "decode_pcm16",
]
