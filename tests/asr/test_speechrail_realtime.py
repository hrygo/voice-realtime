from __future__ import annotations

import asyncio
import base64
import json

from voice_realtime.asr.adapters.speechrail_realtime import (
    SpeechRailRealtimeClient,
    SpeechRailStreamingTranscriber,
)
from voice_realtime.asr.contracts import ASRSessionContext


class FakeConnection:
    uri = "ws://speechrail.test/v2/realtime"

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self._messages = [
            {
                "type": "session.created",
                "event_id": "evt-1",
                "session_id": "sess-1",
                "request_id": "req-1",
                "sequence": 1,
            },
            {
                "type": "transcription.delta",
                "event_id": "evt-2",
                "session_id": "sess-1",
                "request_id": "req-1",
                "sequence": 2,
                "item_id": "item-1",
                "revision": 1,
                "text": "你好",
                "audio_start_ms": 0,
                "audio_end_ms": 100,
            },
        ]

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        return json.dumps(self._messages.pop(0))

    async def close(self) -> None:
        return None


def test_streaming_adapter_maps_v2_snapshot_and_pcm_append() -> None:
    async def scenario() -> None:
        connection = FakeConnection()
        client = SpeechRailRealtimeClient(
            url=connection.uri,
            connection_factory=lambda _: _immediate(connection),
        )
        adapter = SpeechRailStreamingTranscriber(
            client=client,
            context=ASRSessionContext(source_epoch=2, offset_ms=1_000, purpose="subtitles"),
            language="Chinese",
        )

        await adapter.connect()
        await adapter.send_audio(b"\x00\x00")
        await client.commit()
        events = adapter.events()
        assert (await anext(events)).kind == "ready"
        snapshot = await anext(events)

        assert snapshot.kind == "snapshot"
        assert snapshot.window is not None
        assert snapshot.window.partial == "你好"
        assert connection.sent[1]["audio"] == base64.b64encode(b"\x00\x00").decode()
        assert connection.sent[2]["type"] == "input_audio_buffer.commit"

    asyncio.run(scenario())


async def _immediate(connection: FakeConnection) -> FakeConnection:
    return connection
