from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator

from voice_realtime.asr.adapters.speechrail_realtime import (
    SpeechRailRealtimeClient,
    SpeechRailStreamingTranscriber,
)
from voice_realtime.asr.contracts import ASREvent, ASRSessionContext
from voice_realtime.speechrail.transcription_events import (
    SpeechRailTranscriptionError,
    TranscriptionCompleted,
    TranscriptionDelta,
    decode_transcription_event,
)
from voice_realtime.speechrail.transport import SpeechRailV2Transport


class FakeConnection:
    uri = "ws://speechrail.test/v2/realtime"

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self._committed = asyncio.Event()
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
            {
                "type": "transcription.completed",
                "event_id": "evt-3",
                "session_id": "sess-1",
                "request_id": "req-1",
                "sequence": 3,
                "item_id": "item-1",
                "text": "你好世界",
                "language": "Chinese",
                "segments": [
                    {"id": "seg-1", "start_ms": 0, "end_ms": 100, "text": "你好世界"}
                ],
            },
            {
                "type": "transcription.diarization.completed",
                "event_id": "evt-4",
                "session_id": "sess-1",
                "request_id": "req-1",
                "sequence": 4,
                "mapping": {},
            },
        ]

    async def send(self, payload: str) -> None:
        event = json.loads(payload)
        self.sent.append(event)
        if event["type"] == "input_audio_buffer.commit":
            self._committed.set()

    async def recv(self) -> str:
        if len(self._messages) < 3:
            await self._committed.wait()
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


def test_transport_uri_uses_configured_url_when_connection_has_no_uri() -> None:
    async def scenario() -> None:
        connection = Websockets17StyleConnection()
        transport = SpeechRailV2Transport(
            url="ws://speechrail.test/v2/realtime",
            connection_factory=lambda _: _immediate(connection),
        )

        await transport.connect()

        assert transport.uri == "ws://speechrail.test/v2/realtime"
        await transport.close()

    asyncio.run(scenario())


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
        final = await anext(events)

        assert snapshot.kind == "snapshot"
        assert snapshot.window is not None
        assert snapshot.window.partial == "你好"
        assert final.kind == "final"
        assert final.window is not None
        assert final.window.segments[0].start_ms == 1_000
        assert final.window.segments[0].speaker_key == "epoch:2:speaker:0"
        assert connection.sent[1]["audio"] == base64.b64encode(b"\x00\x00").decode()
        assert connection.sent[2]["type"] == "input_audio_buffer.commit"

    asyncio.run(scenario())


def test_meeting_adapter_requests_diarization_and_preserves_anonymous_speaker_label() -> None:
    async def scenario() -> None:
        connection = FakeConnection()
        connection._messages[2]["segments"] = [
            {
                "id": "seg-1",
                "start_ms": 0,
                "end_ms": 100,
                "text": "你好世界",
                "speaker": "spk_02",
                "speakers": [{"id": "spk_02", "confidence": 0.93}],
            }
        ]
        connection._messages[3]["mapping"] = {"spk_02": "spk_01"}
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

        assert connection.sent[0]["session"]["diarization"] == {
            "enabled": True,
            "finalize": True,
            "speaker_count_hint": 2,
            "group_id": "a" * 64,
        }
        assert final.window is not None
        assert final.window.segments[0].speaker_key == "epoch:2:speaker:spk_02"
        assert final.window.speaker_remap == (
            ("epoch:2:speaker:spk_02", f"group:{'a' * 64}:speaker:spk_01"),
        )

    asyncio.run(scenario())


def test_finish_waits_for_the_confirmed_transcript_window() -> None:
    async def scenario() -> None:
        connection = FakeConnection()
        connection._messages[2]["segments"] = [
            {
                "id": "seg-1",
                "start_ms": 0,
                "end_ms": 100,
                "text": "你好世界",
                "speaker": "spk_01",
            }
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
        connection._messages[1]["session_id"] = "sess-other"
        client = SpeechRailRealtimeClient(
            url=connection.uri,
            connection_factory=lambda _: _immediate(connection),
        )

        await client.connect(language="Chinese")
        await client.commit()

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


class FastFakeConnection(FakeConnection):
    """FakeConnection variant that never waits for a commit before recv."""

    async def recv(self) -> str:
        return json.dumps(self._messages.pop(0))


def _stream_messages(*events: dict[str, object]) -> FastFakeConnection:
    """Build a connection whose event stream starts with session.created."""
    connection = FastFakeConnection()
    messages: list[dict[str, object]] = [
        {
            "type": "session.created",
            "event_id": "evt-0",
            "session_id": "sess-1",
            "request_id": "req-1",
            "sequence": 1,
        }
    ]
    for index, event in enumerate(events, start=2):
        envelope = dict(event)
        envelope.setdefault("event_id", f"evt-{index}")
        envelope.setdefault("session_id", "sess-1")
        envelope.setdefault("request_id", "req-1")
        envelope.setdefault("sequence", index)
        messages.append(envelope)
    connection._messages = messages
    return connection


def _collect_stream_events(
    connection: FastFakeConnection,
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
        delta_raw = {"type": "transcription.delta", "text": "你好"}
        completed_raw = {
            "type": "transcription.completed",
            "text": "你好世界",
            "segments": [{"start_ms": 0, "end_ms": 100, "text": "你好世界"}],
        }
        decoded_delta = decode_transcription_event(delta_raw)
        decoded_completed = decode_transcription_event(completed_raw)
        assert isinstance(decoded_delta, TranscriptionDelta)
        assert isinstance(decoded_completed, TranscriptionCompleted)

        connection = _stream_messages(delta_raw, completed_raw)
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
        assert final.window.segments[0].text == decoded_completed.segments[0].text
        assert final.window.segments[0].start_ms == decoded_completed.segments[0].start_ms
        assert final.window.segments[0].speaker_key == "epoch:2:speaker:0"

    asyncio.run(scenario())


def test_streaming_adapter_matches_decoder_for_error_event() -> None:
    error_raw = {"type": "error", "error": {"code": "speechrail_error", "message": "boom"}}
    decoded = decode_transcription_event(error_raw)
    assert isinstance(decoded, SpeechRailTranscriptionError)
    assert decoded.code == "speechrail_error"

    events = _collect_stream_events(
        _stream_messages(error_raw),
        ASRSessionContext(source_epoch=2, offset_ms=0, purpose="subtitles"),
    )

    assert [event.kind for event in events] == ["ready", "error"]
    assert events[1].error_code == "SPEECHRAIL_REQUEST_FAILED"
    assert events[1].error_message == "SpeechRail rejected the transcription request"


def test_streaming_adapter_reports_protocol_error_for_delta_without_text() -> None:
    events = _collect_stream_events(
        _stream_messages({"type": "transcription.delta"}),
        ASRSessionContext(source_epoch=2, offset_ms=0, purpose="subtitles"),
    )

    assert events[-1].kind == "error"
    assert events[-1].error_code == "SPEECHRAIL_PROTOCOL_ERROR"
    assert events[-1].error_message == "SpeechRail returned a transcription delta without text"


def test_streaming_adapter_reports_invalid_completed_segments() -> None:
    events = _collect_stream_events(
        _stream_messages(
            {
                "type": "transcription.completed",
                "text": "",
                "segments": [{"start_ms": 0, "end_ms": 100, "text": ""}],
            }
        ),
        ASRSessionContext(source_epoch=2, offset_ms=0, purpose="subtitles"),
    )

    assert events[-1].kind == "error"
    assert events[-1].error_code == "SPEECHRAIL_PROTOCOL_ERROR"
    assert events[-1].error_message == "SpeechRail returned invalid completed segments"


def test_streaming_adapter_reports_diarization_for_non_diarized_session() -> None:
    events = _collect_stream_events(
        _stream_messages({"type": "transcription.diarization.completed", "mapping": {}}),
        ASRSessionContext(source_epoch=2, offset_ms=0, purpose="subtitles"),
    )

    assert events[-1].kind == "error"
    assert events[-1].error_code == "SPEECHRAIL_PROTOCOL_ERROR"
    assert (
        events[-1].error_message
        == "SpeechRail returned diarization for a non-diarized session"
    )


def test_streaming_adapter_reports_invalid_diarization_mapping() -> None:
    events = _collect_stream_events(
        _stream_messages(
            {"type": "transcription.diarization.completed", "mapping": {"bad": "spk_01"}}
        ),
        ASRSessionContext(source_epoch=2, offset_ms=0, purpose="meeting"),
    )

    assert events[-1].kind == "error"
    assert events[-1].error_code == "SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR"
    assert events[-1].error_message == "SpeechRail returned an invalid diarization mapping"


def test_streaming_adapter_reports_diarized_session_end_without_final_mapping() -> None:
    events = _collect_stream_events(
        _stream_messages({"type": "session.completed"}),
        ASRSessionContext(source_epoch=2, offset_ms=0, purpose="meeting"),
    )

    assert events[-1].kind == "error"
    assert events[-1].error_code == "SPEECHRAIL_DIARIZATION_PROTOCOL_ERROR"
    assert (
        events[-1].error_message
        == "SpeechRail ended a diarized session without a final mapping"
    )


def test_streaming_adapter_requires_speaker_for_diarized_segments() -> None:
    events = _collect_stream_events(
        _stream_messages(
            {
                "type": "transcription.completed",
                "text": "",
                "segments": [{"start_ms": 0, "end_ms": 100, "text": "你好"}],
            }
        ),
        ASRSessionContext(source_epoch=2, offset_ms=0, purpose="meeting"),
    )

    assert events[-1].kind == "error"
    assert events[-1].error_code == "SPEECHRAIL_PROTOCOL_ERROR"
    assert events[-1].error_message == "SpeechRail returned invalid completed segments"


async def _next_final(events: AsyncIterator[ASREvent]) -> ASREvent:
    async for event in events:
        if event.kind == "final":
            return event
    raise AssertionError("adapter ended without a final event")


async def _immediate(connection: FakeConnection) -> FakeConnection:
    return connection
