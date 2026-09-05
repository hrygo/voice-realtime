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
        self.commit_sent = asyncio.Event()
        self._messages_available = asyncio.Event()
        self._messages = [*_session_events(),
            _transcription_delta("你好", sequence=4),
            _transcription_completed("你好世界", sequence=5),
        ]
        self._messages_available.set()

    async def send(self, payload: str) -> None:
        event = json.loads(payload)
        self.sent.append(event)
        if event.get("type") == "input_audio_buffer.commit":
            self.commit_sent.set()

    async def recv(self) -> str:
        while not self._messages:
            self._messages_available.clear()
            await self._messages_available.wait()
        message = self._messages.pop(0)
        if self._messages:
            self._messages_available.set()
        return json.dumps(message)

    def add_messages(self, *messages: dict[str, object]) -> None:
        self._messages.extend(messages)
        self._messages_available.set()

    async def close(self) -> None:
        return None


class AckDuringClearConnection(FakeConnection):
    async def send(self, payload: str) -> None:
        await super().send(payload)
        event = json.loads(payload)
        if event.get("type") == "input_audio_buffer.clear":
            self.add_messages(_envelope("input_audio_buffer.cleared", 6))
            await asyncio.sleep(0)


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


def _speech_started(audio_start_ms: int, *, sequence: int) -> dict[str, object]:
    return _envelope(
        "input_audio_buffer.speech_started",
        sequence,
        item_id="item-1",
        audio_start_ms=audio_start_ms,
    )


def _speech_stopped(audio_end_ms: int, *, sequence: int) -> dict[str, object]:
    return _envelope(
        "input_audio_buffer.speech_stopped",
        sequence,
        item_id="item-1",
        audio_end_ms=audio_end_ms,
    )


def _segment(
    text: str,
    *,
    speaker: str | None,
    sequence: int,
    start: float = 0.0,
    end: float = 1.0,
) -> dict[str, object]:
    return _envelope(
        "conversation.item.input_audio_transcription.segment",
        sequence,
        item_id="item-1",
        content_index=0,
        id=f"seg-{sequence}",
        text=text,
        speaker=speaker,
        start=start,
        end=end,
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
        assert (
            final.window.segments[0].start_ms,
            final.window.segments[0].end_ms,
        ) == (1_000, 1_480)
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
            context=ASRSessionContext(source_epoch=2, offset_ms=1_000, purpose="meeting"),
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
            _envelope("input_audio_buffer.cleared", 6),
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
        collected: list[ASREvent] = []

        async def collect_events() -> None:
            collected.extend([event async for event in events])

        event_task = asyncio.create_task(collect_events())
        completed = await adapter.finish()
        await event_task

        assert completed.segments[0].text == "你好世界"
        assert [event.kind for event in collected] == ["final"]

    asyncio.run(scenario())


def test_finish_drains_old_and_empty_eof_completed_before_clear_ack() -> None:
    async def scenario() -> None:
        connection = FakeConnection()
        connection._messages = _session_events()
        adapter = SpeechRailStreamingTranscriber(
            client=SpeechRailRealtimeClient(
                url=connection.uri,
                connection_factory=lambda _: _immediate(connection),
            ),
            context=ASRSessionContext(source_epoch=2, offset_ms=1_000, purpose="meeting"),
            language="Chinese",
        )
        await adapter.connect()
        events = adapter.events()
        assert (await anext(events)).kind == "ready"
        collected: list[ASREvent] = []

        async def collect_tail() -> None:
            collected.extend([event async for event in events])

        event_task = asyncio.create_task(collect_tail())

        async def append_tail_after_commit() -> None:
            await connection.commit_sent.wait()
            connection.add_messages(
                _transcription_completed("旧轮", sequence=4),
                _transcription_completed("", sequence=5),
                _envelope("input_audio_buffer.cleared", 6),
            )

        producer = asyncio.create_task(append_tail_after_commit())
        completed = await adapter.finish()
        await producer
        await event_task

        assert completed.segments[0].text == "旧轮"
        assert [event.kind for event in collected] == ["final", "final"]
        assert collected[0].window is not None
        assert collected[0].window.segments[0].text == "旧轮"
        assert collected[1].window is not None
        assert collected[1].window.segments == ()
        assert connection._messages == []
        assert [event["type"] for event in connection.sent[-2:]] == [
            "input_audio_buffer.commit",
            "input_audio_buffer.clear",
        ]

    asyncio.run(scenario())


def test_finish_is_idempotent_after_clear_barrier() -> None:
    async def scenario() -> None:
        connection = FakeConnection()
        connection._messages = [
            *_session_events(),
            _transcription_completed("最后一句", sequence=4),
            _envelope("input_audio_buffer.cleared", 5),
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

        event_task = asyncio.create_task(_drain_events(events))
        first = await adapter.finish()
        second = await adapter.finish()
        await event_task

        assert first == second
        assert [event["type"] for event in connection.sent].count("input_audio_buffer.commit") == 1
        assert [event["type"] for event in connection.sent].count("input_audio_buffer.clear") == 1

    asyncio.run(scenario())


def test_finish_accepts_clear_ack_arriving_before_clear_send_returns() -> None:
    async def scenario() -> None:
        connection = AckDuringClearConnection()
        connection._messages = [
            *_session_events(),
            _transcription_completed("尾句", sequence=4),
            _transcription_completed("", sequence=5),
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
        event_task = asyncio.create_task(_drain_events(events))

        completed = await adapter.finish()
        await event_task

        assert completed.segments[0].text == "尾句"
        assert connection._messages == []

    asyncio.run(scenario())


def test_events_yields_multiple_turns_without_terminating_until_finish() -> None:
    async def scenario() -> None:
        connection = FakeConnection()
        connection._messages = [
            *_session_events(),
            # Turn 1
            _transcription_delta("第一句", sequence=4),
            _transcription_completed("第一句完成", sequence=5),
            # Turn 2
            _transcription_delta("第二句", sequence=6),
            _transcription_completed("第二句完成", sequence=7),
        ]
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
        events = adapter.events()

        assert (await anext(events)).kind == "ready"

        # Turn 1 snapshot & final
        e1_snap = await anext(events)
        assert e1_snap.kind == "snapshot" and e1_snap.window and e1_snap.window.partial == "第一句"
        e1_final = await anext(events)
        assert e1_final.kind == "final" and e1_final.window
        assert e1_final.window.segments[0].text == "第一句完成"

        # Turn 2: events() must still be alive and yield Turn 2!
        e2_snap = await anext(events)
        assert e2_snap.kind == "snapshot" and e2_snap.window and e2_snap.window.partial == "第二句"
        e2_final = await anext(events)
        assert e2_final.kind == "final" and e2_final.window
        assert e2_final.window.segments[0].text == "第二句完成"

        # Finish terminates the stream
        adapter._commit_sent = True
        # Once closed / finished, events terminates
        await adapter.close()

    asyncio.run(scenario())


def test_streaming_adapter_normalizes_each_segment_from_speech_start_clock() -> None:
    async def scenario() -> None:
        connection = FakeConnection()
        connection._messages = [
            *_session_events(),
            # SpeechRail segment timestamps restart at zero for every committed item.
            _speech_started(1_024, sequence=4),
            _segment("第一句", speaker="spk_01", sequence=5, start=0.1, end=0.8),
            _speech_stopped(2_848, sequence=6),
            _transcription_completed("第一句", sequence=7),
            _speech_started(3_008, sequence=8),
            _segment("第二句", speaker="spk_01", sequence=9, start=0.0, end=0.6),
            _transcription_completed("第二句", sequence=10),
        ]
        client = SpeechRailRealtimeClient(
            url=connection.uri,
            connection_factory=lambda _: _immediate(connection),
        )
        adapter = SpeechRailStreamingTranscriber(
            client=client,
            context=ASRSessionContext(source_epoch=3, offset_ms=1_000, purpose="meeting"),
            language="Chinese",
        )
        await adapter.connect()
        events = adapter.events()

        assert (await anext(events)).kind == "ready"
        first = await anext(events)
        second = await anext(events)

        assert first.kind == "final" and first.window is not None
        assert second.kind == "final" and second.window is not None
        first_segment = first.window.segments[0]
        second_segment = second.window.segments[0]
        assert (first_segment.start_ms, first_segment.end_ms) == (1_100, 1_800)
        assert (second_segment.start_ms, second_segment.end_ms) == (3_848, 4_448)

    asyncio.run(scenario())


def test_streaming_adapter_accumulates_incremental_deltas_until_completed() -> None:
    async def scenario() -> None:
        connection = FakeConnection()
        connection._messages = [
            *_session_events(),
            _transcription_delta("第一", sequence=4),
            _transcription_delta("句", sequence=5),
            _transcription_completed("第一句", sequence=6),
        ]
        adapter = SpeechRailStreamingTranscriber(
            client=SpeechRailRealtimeClient(
                url=connection.uri,
                connection_factory=lambda _: _immediate(connection),
            ),
            context=ASRSessionContext(source_epoch=3, offset_ms=0, purpose="subtitles"),
            language="Chinese",
        )
        await adapter.connect()
        events = adapter.events()

        assert (await anext(events)).kind == "ready"
        first_snapshot = await anext(events)
        second_snapshot = await anext(events)
        final = await anext(events)

        assert first_snapshot.window is not None
        assert second_snapshot.window is not None
        assert first_snapshot.window.partial == "第一"
        assert second_snapshot.window.partial == "第一句"
        assert final.kind == "final"

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


def test_streaming_adapter_accepts_empty_completed_transcript_as_no_new_text() -> None:
    connection = FakeConnection()
    connection._messages = [*_session_events(),
        _envelope("conversation.item.input_audio_transcription.completed", 4, transcript="")
    ]
    events = _collect_stream_events(
        connection,
        ASRSessionContext(source_epoch=2, offset_ms=0, purpose="meeting"),
    )

    assert [event.kind for event in events] == ["ready", "final"]
    assert events[-1].window is not None
    assert events[-1].window.segments == ()


def test_streaming_adapter_falls_back_for_diarized_completed_without_segments() -> None:
    connection = FakeConnection()
    connection._messages = [*_session_events(),
        _transcription_completed("嗯。", sequence=4),
    ]
    events = _collect_stream_events(
        connection,
        ASRSessionContext(source_epoch=2, offset_ms=0, purpose="meeting"),
    )

    assert [event.kind for event in events] == ["ready", "final"]
    assert events[-1].window is not None
    assert events[-1].window.segments[0].text == "嗯。"
    assert events[-1].window.segments[0].speaker_key == "epoch:2:speaker:0"


def test_streaming_adapter_uses_vad_bounds_for_completed_without_segments() -> None:
    connection = FakeConnection()
    connection._messages = [
        *_session_events(),
        _speech_started(1_200, sequence=4),
        _speech_stopped(1_900, sequence=5),
        _transcription_completed("无 segment", sequence=6),
    ]
    events = _collect_stream_events(
        connection,
        ASRSessionContext(source_epoch=2, offset_ms=3_000, purpose="meeting"),
    )

    final = events[-1]
    assert final.window is not None
    segment = final.window.segments[0]
    assert (segment.start_ms, segment.end_ms) == (4_200, 4_900)


def test_streaming_adapter_fallback_starts_after_previous_confirmed_segment() -> None:
    async def scenario() -> None:
        connection = FakeConnection()
        connection._messages = [
            *_session_events(),
            _segment("已确认", speaker="spk_01", sequence=4, start=0.5, end=1.5),
            _transcription_completed("已确认", sequence=5),
            _transcription_completed("无边界", sequence=6),
        ]
        adapter = SpeechRailStreamingTranscriber(
            client=SpeechRailRealtimeClient(
                url=connection.uri,
                connection_factory=lambda _: _immediate(connection),
            ),
            context=ASRSessionContext(source_epoch=2, offset_ms=1_000, purpose="meeting"),
            language="Chinese",
        )
        await adapter.connect()
        events = adapter.events()

        assert (await anext(events)).kind == "ready"
        first = await anext(events)
        second = await anext(events)

        assert first.window is not None
        assert second.window is not None
        first_segment = first.window.segments[0]
        second_segment = second.window.segments[0]
        assert (first_segment.start_ms, first_segment.end_ms) == (1_500, 2_500)
        assert second_segment.start_ms >= first_segment.end_ms
        assert 0 < second_segment.end_ms - second_segment.start_ms <= 10_000

    asyncio.run(scenario())


def test_finish_keeps_last_confirmed_after_partial_and_empty_completed() -> None:
    async def scenario() -> None:
        connection = FakeConnection()
        connection._messages = _session_events()
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
        collected: list[ASREvent] = []

        async def collect_tail() -> None:
            collected.extend([event async for event in events])

        event_task = asyncio.create_task(collect_tail())

        async def append_tail_after_commit() -> None:
            await connection.commit_sent.wait()
            connection.add_messages(
                _transcription_completed("确认 A", sequence=4),
                _transcription_delta("partial B", sequence=5),
                _transcription_completed("", sequence=6),
                _envelope("input_audio_buffer.cleared", 7),
            )

        producer = asyncio.create_task(append_tail_after_commit())
        completed = await adapter.finish()
        await producer
        await event_task

        assert completed.partial == ""
        assert [segment.text for segment in completed.segments] == ["确认 A"]
        assert [event.kind for event in collected] == ["final", "snapshot", "final"]
        assert collected[1].window is not None
        assert collected[1].window.partial == "partial B"
        assert collected[2].window is not None
        assert collected[2].window.partial == ""
        assert collected[2].window.segments == ()

    asyncio.run(scenario())


def test_streaming_adapter_falls_back_for_diarized_segment_without_speaker() -> None:
    connection = FakeConnection()
    connection._messages = [*_session_events(),
        _segment("你好", speaker=None, sequence=4),
        _transcription_completed("你好", sequence=5),
    ]
    events = _collect_stream_events(
        connection,
        ASRSessionContext(source_epoch=2, offset_ms=0, purpose="meeting"),
    )

    assert [event.kind for event in events] == ["ready", "final"]
    assert events[-1].window is not None
    assert events[-1].window.segments[0].text == "你好"
    assert events[-1].window.segments[0].speaker_key == "epoch:2:speaker:0"


async def _next_final(events: AsyncIterator[ASREvent]) -> ASREvent:
    async for event in events:
        if event.kind == "final":
            return event
    raise AssertionError("adapter ended without a final event")


async def _drain_events(events: AsyncIterator[ASREvent]) -> list[ASREvent]:
    return [event async for event in events]


async def _immediate(connection: FakeConnection) -> FakeConnection:
    return connection
