from __future__ import annotations

import asyncio
import base64
import json

import pytest

from voice_realtime.speechrail.tts import SpeechRailTTSClient


class FakeSpeechConnection:
    uri = "ws://speechrail.test/v2/realtime"

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.messages = [
            self._event("session.created", 1, session={"type": "speech"}),
            self._event("response.created", 2, response_id="resp-1"),
            self._event(
                "response.audio.delta",
                3,
                response_id="resp-1",
                chunk_index=0,
                audio=base64.b64encode(b"\x00\x00").decode("ascii"),
            ),
            self._event("response.audio.completed", 4, response_id="resp-1", total_chunks=1),
            self._event("session.completed", 5),
        ]

    @staticmethod
    def _event(event_type: str, sequence: int, **payload: object) -> dict[str, object]:
        return {
            "type": event_type,
            "event_id": f"evt-{sequence}",
            "session_id": "sess-1",
            "request_id": "req-1",
            "sequence": sequence,
            **payload,
        }

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        return json.dumps(self.messages.pop(0))

    async def close(self) -> None:
        return None


def test_tts_client_sends_v2_speech_session_and_yields_ordered_pcm() -> None:
    async def scenario() -> None:
        connection = FakeSpeechConnection()
        client = SpeechRailTTSClient(
            url=connection.uri,
            model="speechrail/qwen3-tts",
            voice="warm",
            language="zh",
            connection_factory=lambda _: _immediate(connection),
        )

        chunks = [chunk async for chunk in client.synthesize("你好", speed=1.1)]

        assert chunks == [b"\x00\x00"]
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
                        "rate": 24_000,
                        "channels": 1,
                        "sample_width": 2,
                    },
                },
            },
            {"type": "speech_input.append", "text": "你好"},
            {"type": "speech_input.commit", "speed": 1.1},
        ]

    asyncio.run(scenario())


def test_tts_client_cancels_the_active_response_when_consumer_stops() -> None:
    async def scenario() -> None:
        connection = BlockingSpeechConnection()
        client = SpeechRailTTSClient(
            url=connection.uri,
            model="speechrail/qwen3-tts",
            voice="default",
            connection_factory=lambda _: _immediate(connection),
        )

        async def consume() -> None:
            async for _chunk in client.synthesize("请取消", speed=1.0):
                pass

        task = asyncio.create_task(consume())
        await connection.response_created.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert {"type": "response.cancel", "response_id": "resp-cancel"} in connection.sent
        assert connection.closed is True

    asyncio.run(scenario())


def test_tts_client_exposes_and_cancels_active_response_for_pipecat() -> None:
    async def scenario() -> None:
        connection = BlockingSpeechConnection()
        client = SpeechRailTTSClient(
            url=connection.uri,
            model="speechrail/qwen3-tts",
            voice="default",
            connection_factory=lambda _: _immediate(connection),
        )

        async def consume() -> None:
            async for _chunk in client.synthesize("请取消", speed=1.0):
                pass

        task = asyncio.create_task(consume())
        await connection.response_created.wait()
        assert client.active_response_id == "resp-cancel"
        await client.cancel("resp-cancel")
        assert client.active_response_id is None
        assert {"type": "response.cancel", "response_id": "resp-cancel"} in connection.sent
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


class BlockingSpeechConnection(FakeSpeechConnection):
    def __init__(self) -> None:
        super().__init__()
        self.sent = []
        self.messages = [self._event("session.created", 1, session={"type": "speech"})]
        self.response_created = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def send(self, payload: str) -> None:
        event = json.loads(payload)
        self.sent.append(event)
        if event.get("type") == "speech_input.commit":
            self.messages.append(self._event("response.created", 2, response_id="resp-cancel"))
            self.response_created.set()

    async def recv(self) -> str:
        while not self.messages:
            await self.release.wait()
        return json.dumps(self.messages.pop(0))

    async def close(self) -> None:
        self.closed = True


async def _immediate(connection: FakeSpeechConnection) -> FakeSpeechConnection:
    return connection
