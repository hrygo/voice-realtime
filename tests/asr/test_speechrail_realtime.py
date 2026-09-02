from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator

from sona.asr.contracts import ASREvent, ASRSessionContext
from sona.speechrail import (
    SpeechRailRealtimeClient,
    SpeechRailStreamingTranscriber,
)
from sona.speechrail.transcription_events import (
    SpeechRailTranscriptionError,
    TranscriptionCompleted,
    TranscriptionDelta,
    decode_transcription_event,
)
from sona.speechrail.transport import SpeechRailOpenAITransport


class FakeConnection:
    uri = "ws://speechrail.test/v1/realtime"

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self._messages = [*_session_events(),
            _transcription_delta("你好", sequence=4),
            _transcription_completed("你好世界", sequence=5),
        ]

    async def send(self, payload: str) -> None:
        event = json.loads(payload)
        self.sent.append(event)

    async def recv(self) -> str:
        return json.dumps(self._messages.pop(0))

    async def close(self) -> None:
        return None


class Websockets17StyleConnection:
    """Minimal websockets 17 connection shape: no public ``uri`` attribute."""

    async def send(self, payload: str) -> None:
        return None

    async def recv(self) -> str:
        raise AssertionError("recv should not be called by the transport URI test")

    async def close(self) -> None:
        return None


def _session_events() -> list[dict[str, object]]:
    return [
        _envelope("session.created", 1, session={"id": "sess-1"}),
        _envelope("session.updated", 2, session={"id": "sess-1"}),
        _envelope("conversation.created", 3, conversation={"id": "conv-1"}),
    ]


def _envelope(event_type: str, sequence: int, **payload: object) -> dict[str, object]:
    return {
        "type": event_type,
        "event_id": f"evt-{sequence}",
        "session_id": "sess-1",
        "sequence": sequence,
        **payload,
    }


def _transcription_delta(text: str, *, sequence: int) -> dict[str, object]:
    return _envelope(
        "conversation.item.input_audio_transcription.delta",
        sequence,
        item_id="item-1",
        content_index=0,
        delta=text,
    )


def _transcription_completed(transcript: str, *, sequence: int) -> dict[str, object]:
    return _envelope(
        "conversation.item.input_audio_transcription.completed",
        sequence,
        item_id="item-1",
        content_index=0,
        transcript=transcript,
    )


def _segment(text: str, *, speaker: str | None, sequence: int) -> dict[str, object]:
    return _envelope(
        "conversation.item.input_audio_transcription.segment",
        sequence,
        item_id="item-1",
        content_index=0,
        id=f"seg-{sequence}",
        text=text,
        speaker=speaker,
        start=0.0,
        end=1.0,
    )


def test_transport_uri_uses_configured_url_when_connection_has_no_uri() -> None:
    async def scenario() -> None:
        connection = Websockets17StyleConnection()
        transport = SpeechRailOpenAITransport(
            url="ws://speechrail.test/v1/realtime",
            connection_factory=lambda _: _immediate(connection),
        )

        await transport.connect()

        assert transport.uri == "ws://speechrail.test/v1/realtime"
        await transport.close()

    asyncio.run(scenario())


def test_streaming_adapter_maps_openai_snapshot_and_pcm_append() -> None:
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
        final = await anext(events)

        assert snapshot.kind == "snapshot"
        assert snapshot.window is not None
        assert snapshot.window.partial == "你好"
        assert final.kind == "final"
        assert final.window is not None
        assert final.window.segments[0].text == "你好世界"
        assert final.window.segments[0].speaker_key == "epoch:2:speaker:0"
        assert "input_audio_transcription" in connection.sent[0]["session"]
        assert connection.sent[1]["audio"] == base64.b64encode(b"\x00\x00").decode()
        assert connection.sent[2]["type"] == "input_audio_buffer.commit"

    asyncio.run(scenario())


def test_meeting_adapter_requests_diarization_and_preserves_anonymous_speaker_label() -> None:
    async def scenario() -> None:
        connection = FakeConnection()
        connection._messages = [*_session_events(),
            _transcription_delta("你好", sequence=4),
            _segment("你好世界", speaker="spk_02", sequence=5),
            _transcription_completed("你好世界", sequence=6),
        ]
        adapter = SpeechRailStreamingTranscriber(
            client=SpeechRailRealtimeClient(
                url=connection.uri,
                connection_factory=lambda _: _immediate(connection),
            ),
            context=ASRSessionContext(
                source_epoch=2,
                offset_ms=1_000,
                purpose="meeting",
                speaker_count_hint=2,
                diarization_group_id="a" * 64,
            ),
            language="Chinese",
        )

        await adapter.connect()
        await adapter.send_audio(b"\x00\x00")
        await adapter._client.commit()
        events = adapter.events()
        assert (await anext(events)).kind == "ready"
        await anext(events)
        final = await anext(events)

        transcription = connection.sent[0]["session"]["input_audio_transcription"]
        assert isinstance(transcription, dict)
        assert transcription["diarization"] == {
            "enabled": True,
            "finalize": True,
            "speaker_count_hint": 2,
            "group_id": "a" * 64,
        }
        assert final.window is not None
        assert final.window.segments[0].speaker_key == f"group:{'a' * 64}:speaker:spk_02"
        assert final.window.segments[0].text == "你好世界"

    asyncio.run(scenario())


def test_meeting_adapter_defaults_epoch_speaker_when_no_group_id() -> None:
    async def scenario() -> None:
        connection = FakeConnection()
        connection._messages = [*_session_events(),
            _segment("你好世界", speaker="spk_01", sequence=4),
            _transcription_completed("你好世界", sequence=5),
        ]
        adapter = SpeechRailStreamingTranscriber(
            client=SpeechRailRealtimeClient(
                url=connection.uri,
                connection_factory=lambda _: _immediate(connection),
            ),
            context=ASRSessionContext(source_epoch=2, offset_ms=0, purpose="meeting"),
            language="Chinese",
        )

        await adapter.connect()
        events = adapter.events()
        assert (await anext(events)).kind == "ready"
        final = await anext(events)

        assert final.window is not None
        assert final.window.segments[0].speaker_key == "epoch:2:speaker:spk_01"

    asyncio.run(scenario())


def test_finish_waits_for_the_confirmed_transcript_window() -> None:
    async def scenario() -> None:
        connection = FakeConnection()
        connection._messages = [*_session_events(),
            _segment("你好世界", speaker="spk_01", sequence=4),
            _transcription_completed("你好世界", sequence=5),
        ]
        client = SpeechRailRealtimeClient(
            url=connection.uri,
            connection_factory=lambda _: _immediate(connection),
        )
        adapter = SpeechRailStreamingTranscriber(
            client=client,
            context=ASRSessionContext(source_epoch=2, offset_ms=1_000, purpose="meeting"),
            language="Chinese",
        )
        await adapter.connect()
        await adapter.send_audio(b"\x00\x00")

        events = adapter.events()
        assert (await anext(events)).kind == "ready"
        final_event = asyncio.create_task(_next_final(events))
        completed = await adapter.finish()

        assert completed.segments[0].text == "你好世界"
        assert (await final_event).kind == "final"

    asyncio.run(scenario())


def test_client_reconnects_with_a_new_session_sequence() -> None:
    async def scenario() -> None:
        first = FakeConnection()
        second = FakeConnection()
        connections = iter((first, second))
        client = SpeechRailRealtimeClient(
            url=first.uri,
            connection_factory=lambda _: _immediate(next(connections)),
        )

        await client.connect(language="Chinese")
        await client.close()
        await client.connect(language="Chinese")

        assert client.uri == second.uri

    asyncio.run(scenario())


def test_client_rejects_events_from_another_session() -> None:
    async def scenario() -> None:
        connection = FakeConnection()
        connection._messages = _session_events()
        connection._messages[1]["session_id"] = "sess-other"
        client = SpeechRailRealtimeClient(
            url=connection.uri,
            connection_factory=lambda _: _immediate(connection),
        )

        await client.connect(language="Chinese")
        assert (await client.receive())["type"] == "session.created"

        try:
            await client.receive()
        except RuntimeError as error:
            assert str(error) == "SPEECHRAIL_SESSION_MISMATCH"
        else:
            raise AssertionError("mismatched session event was accepted")

    asyncio.run(scenario())


def test_finish_times_out_when_no_event_reader_receives_a_final_result() -> None:
    async def scenario() -> None:
        connection = FakeConnection()
        connection._messages = _session_events()
        client = SpeechRailRealtimeClient(
            url=connection.uri,
            connection_factory=lambda _: _immediate(connection),
        )
        adapter = SpeechRailStreamingTranscriber(
            client=client,
            context=ASRSessionContext(source_epoch=2, offset_ms=0, purpose="meeting"),
            language="Chinese",
            finish_timeout_secs=0.01,
        )
        await adapter.connect()

        try:
            await adapter.finish()
        except TimeoutError as error:
            assert str(error) == "SPEECHRAIL_FINAL_TIMEOUT: final result was not received"
        else:
            raise AssertionError("finish unexpectedly completed without a final event")

    asyncio.run(scenario())


def _collect_stream_events(
    connection: FakeConnection,
    context: ASRSessionContext,
) -> list[ASREvent]:
    async def scenario() -> list[ASREvent]:
        adapter = SpeechRailStreamingTranscriber(
            client=SpeechRailRealtimeClient(
                url=connection.uri,
                connection_factory=lambda _: _immediate(connection),
            ),
            context=context,
            language="Chinese",
        )
        await adapter.connect()
        collected: list[ASREvent] = []
        async for event in adapter.events():
            collected.append(event)
            if event.kind in {"final", "error"}:
                break
        return collected

    return asyncio.run(scenario())


def test_streaming_adapter_matches_decoder_for_delta_and_completed() -> None:
    async def scenario() -> None:
        delta_raw = {
            "type": "conversation.item.input_audio_transcription.delta",
            "delta": "你好",
        }
        completed_raw = {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "你好世界",
        }
        decoded_delta = decode_transcription_event(delta_raw)
        decoded_completed = decode_transcription_event(completed_raw)
        assert isinstance(decoded_delta, TranscriptionDelta)
        assert isinstance(decoded_completed, TranscriptionCompleted)

        connection = FakeConnection()
        connection._messages = [*_session_events(),
            _transcription_delta(decoded_delta.text, sequence=4),
            _transcription_completed(decoded_completed.transcript, sequence=5),
        ]
        adapter = SpeechRailStreamingTranscriber(
            client=SpeechRailRealtimeClient(
                url=connection.uri,
                connection_factory=lambda _: _immediate(connection),
            ),
            context=ASRSessionContext(source_epoch=2, offset_ms=0, purpose="subtitles"),
            language="Chinese",
        )
        await adapter.connect()
        events = adapter.events()
        assert (await anext(events)).kind == "ready"
        snapshot = await anext(events)
        final = await anext(events)

        assert snapshot.kind == "snapshot"
        assert snapshot.window is not None
        assert snapshot.window.partial == decoded_delta.text
        assert final.kind == "final"
        assert final.window is not None
        assert final.window.segments[0].text == decoded_completed.transcript
        assert final.window.segments[0].speaker_key == "epoch:2:speaker:0"

    asyncio.run(scenario())


def test_streaming_adapter_matches_decoder_for_error_event() -> None:
    error_raw = {"type": "error", "error": {"code": "speechrail_error", "message": "boom"}}
    decoded = decode_transcription_event(error_raw)
    assert isinstance(decoded, SpeechRailTranscriptionError)
    assert decoded.code == "speechrail_error"

    connection = FakeConnection()
    connection._messages = [*_session_events(),
        _envelope("error", 4, error={"code": "speechrail_error", "message": "boom"})
    ]
    events = _collect_stream_events(
        connection,
        ASRSessionContext(source_epoch=2, offset_ms=0, purpose="subtitles"),
    )

    assert [event.kind for event in events] == ["ready", "error"]
    assert events[1].error_code == "SPEECHRAIL_REQUEST_FAILED"
    assert events[1].error_message == "SpeechRail rejected the transcription request"


def test_streaming_adapter_reports_protocol_error_for_delta_without_text() -> None:
    connection = FakeConnection()
    connection._messages = [*_session_events(),
        _envelope("conversation.item.input_audio_transcription.delta", 4)
    ]
    events = _collect_stream_events(
        connection,
        ASRSessionContext(source_epoch=2, offset_ms=0, purpose="subtitles"),
    )

    assert events[-1].kind == "error"
    assert events[-1].error_code == "SPEECHRAIL_PROTOCOL_ERROR"
    assert events[-1].error_message == "SpeechRail returned a transcription delta without text"


def test_streaming_adapter_reports_invalid_completed_transcript() -> None:
    connection = FakeConnection()
    connection._messages = [*_session_events(),
        _envelope("conversation.item.input_audio_transcription.completed", 4, transcript="")
    ]
    events = _collect_stream_events(
        connection,
        ASRSessionContext(source_epoch=2, offset_ms=0, purpose="meeting"),
    )

    assert events[-1].kind == "error"
    assert events[-1].error_code == "SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR"
    assert events[-1].error_message == "SpeechRail ended a diarized turn without segment events"


def test_streaming_adapter_requires_speaker_for_diarized_segments() -> None:
    connection = FakeConnection()
    connection._messages = [*_session_events(),
        _segment("你好", speaker=None, sequence=4),
        _transcription_completed("你好", sequence=5),
    ]
    events = _collect_stream_events(
        connection,
        ASRSessionContext(source_epoch=2, offset_ms=0, purpose="meeting"),
    )

    assert events[-1].kind == "error"
    assert events[-1].error_code == "SPEECHRAIL_PROTOCOL_ERROR"
    assert events[-1].error_message == "SpeechRail returned an invalid transcription segment"


async def _next_final(events: AsyncIterator[ASREvent]) -> ASREvent:
    async for event in events:
        if event.kind == "final":
            return event
    raise AssertionError("adapter ended without a final event")


async def _immediate(connection: FakeConnection) -> FakeConnection:
    return connection
