from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator

import pytest

from voice_realtime.speechrail.transport import SpeechRailProtocolError, SpeechRailV2Transport
from voice_realtime.speechrail.tts import SpeechRailTTSClient


class FakeConnection:
    uri = "ws://speechrail.test/v2/realtime"

    def __init__(self, messages: list[dict[str, object]]) -> None:
        self._messages = messages
        self.sent: list[dict[str, object]] = []
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        return json.dumps(self._messages.pop(0))

    async def close(self) -> None:
        self.closed = True


def _events(*events: dict[str, object]) -> list[dict[str, object]]:
    return [
        {
            "event_id": f"evt-{index}",
            "session_id": "sess-1",
            "request_id": "req-1",
            "sequence": index,
            **event,
        }
        for index, event in enumerate(events, start=1)
    ]


async def _connection(connection: FakeConnection) -> FakeConnection:
    return connection


async def _collect(stream: AsyncIterator[bytes]) -> list[bytes]:
    return [chunk async for chunk in stream]


def test_transport_rejects_non_monotonic_server_sequence() -> None:
    async def scenario() -> None:
        connection = FakeConnection(
            _events(
                {"type": "session.created", "session": {"type": "speech"}},
                {"type": "session.completed", "sequence": 1},
            )
        )
        transport = SpeechRailV2Transport(
            url=connection.uri, connection_factory=lambda _: _connection(connection)
        )
        await transport.connect()
        await transport.receive()

        with pytest.raises(SpeechRailProtocolError, match="SPEECHRAIL_SEQUENCE_ERROR"):
            await transport.receive()

    asyncio.run(scenario())


def test_tts_client_sends_speech_session_and_yields_ordered_pcm() -> None:
    async def scenario() -> None:
        connection = FakeConnection(
            _events(
                {"type": "session.created", "session": {"type": "speech"}},
                {
                    "type": "response.created",
                    "response_id": "resp-1",
                    "audio_format": {
                        "type": "audio/pcm",
                        "rate": 24000,
                        "channels": 1,
                        "sample_width": 2,
                    },
                },
                {
                    "type": "response.audio.delta",
                    "response_id": "resp-1",
                    "chunk_index": 0,
                    "audio": base64.b64encode(b"\x01\x00").decode(),
                },
                {"type": "response.audio.completed", "response_id": "resp-1"},
                {"type": "session.completed"},
            )
        )
        client = SpeechRailTTSClient(
            url=connection.uri,
            model="speechrail/qwen3-tts",
            voice="warm",
            language="zh",
            connection_factory=lambda _: _connection(connection),
        )

        assert await _collect(client.synthesize("你好", speed=1.2)) == [b"\x01\x00"]
        assert connection.sent == [
            {
                "type": "session.update",
                "session": {
                    "type": "speech",
                    "model": "speechrail/qwen3-tts",
                    "voice": "warm",
                    "language": "zh",
                    "audio_format": {
                        "type": "audio/pcm",
                        "rate": 24000,
                        "channels": 1,
                        "sample_width": 2,
                    },
                },
            },
            {"type": "speech_input.append", "text": "你好", "speed": 1.2},
            {"type": "speech_input.commit"},
        ]
        assert connection.closed

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("event", "message"),
    [
        (
            {
                "type": "response.audio.delta",
                "response_id": "resp-unknown",
                "chunk_index": 0,
                "audio": base64.b64encode(b"\x01\x00").decode(),
            },
            "unknown response",
        ),
        (
            {
                "type": "response.audio.delta",
                "response_id": "resp-1",
                "chunk_index": 0,
                "audio": "not-base64",
            },
            "invalid PCM",
        ),
    ],
)
def test_tts_client_rejects_invalid_remote_audio_events(
    event: dict[str, object], message: str
) -> None:
    async def scenario() -> None:
        connection = FakeConnection(
            _events(
                {"type": "session.created", "session": {"type": "speech"}},
                {
                    "type": "response.created",
                    "response_id": "resp-1",
                    "audio_format": {
                        "type": "audio/pcm",
                        "rate": 24000,
                        "channels": 1,
                        "sample_width": 2,
                    },
                },
                event,
            )
        )
        client = SpeechRailTTSClient(
            url=connection.uri,
            model="speechrail/qwen3-tts",
            voice="default",
            connection_factory=lambda _: _connection(connection),
        )

        with pytest.raises(SpeechRailProtocolError, match=message):
            await _collect(client.synthesize("测试"))
        assert connection.closed

    asyncio.run(scenario())


def test_tts_client_cancels_the_active_response() -> None:
    async def scenario() -> None:
        connection = FakeConnection(
            _events(
                {"type": "session.created", "session": {"type": "speech"}},
                {
                    "type": "response.created",
                    "response_id": "resp-1",
                    "audio_format": {
                        "type": "audio/pcm",
                        "rate": 24000,
                        "channels": 1,
                        "sample_width": 2,
                    },
                },
                {
                    "type": "response.audio.delta",
                    "response_id": "resp-1",
                    "chunk_index": 0,
                    "audio": base64.b64encode(b"\x01\x00").decode(),
                },
                {"type": "response.audio.cancelled", "response_id": "resp-1"},
            )
        )
        client = SpeechRailTTSClient(
            url=connection.uri,
            model="speechrail/qwen3-tts",
            voice="default",
            connection_factory=lambda _: _connection(connection),
        )
        stream = client.synthesize("测试")

        assert await anext(stream) == b"\x01\x00"
        await client.cancel("resp-1")
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert connection.sent[-1] == {"type": "response.cancel", "response_id": "resp-1"}
        assert connection.closed

    asyncio.run(scenario())
