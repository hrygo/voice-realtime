"""Fun-ASR-Nano 官方实时 WebSocket 适配器协议测试。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from voice_realtime.asr.adapters.funasr_nano_ws import FunASRNanoWSAdapter
from voice_realtime.asr.contracts import ASREvent, ASRSessionContext


class FakeFunASRWebSocket:
    """只模拟官方协议所需的 send/recv/close 边界。"""

    def __init__(self, incoming: list[str | bytes]) -> None:
        self._incoming: asyncio.Queue[str | bytes | BaseException] = asyncio.Queue()
        for item in incoming:
            self._incoming.put_nowait(item)
        self.sent: list[str | bytes] = []
        self.closed = False

    @property
    def uri(self) -> str:
        return "ws://127.0.0.1:10095"

    async def send(self, payload: str | bytes) -> None:
        self.sent.append(payload)

    async def recv(self) -> str | bytes:
        item = await self._incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True

    def push(self, payload: dict[str, object] | str | bytes) -> None:
        if isinstance(payload, dict):
            self._incoming.put_nowait(json.dumps(payload, ensure_ascii=False))
        else:
            self._incoming.put_nowait(payload)

    def disconnect(self) -> None:
        self._incoming.put_nowait(ConnectionError("peer closed"))


def _context(*, purpose: str = "subtitles") -> ASRSessionContext:
    return ASRSessionContext(source_epoch=3, offset_ms=1_000, purpose=purpose)  # type: ignore[arg-type]


def _adapter(
    stream: FakeFunASRWebSocket,
    *,
    hotwords: tuple[str, ...] = ("Fun-ASR", "语音助手"),
    finish_timeout_secs: float = 0.05,
    audited: list[dict[str, object]] | None = None,
) -> FunASRNanoWSAdapter:
    async def connect_factory(_url: str) -> FakeFunASRWebSocket:
        return stream

    return FunASRNanoWSAdapter(
        url="ws://127.0.0.1:10095",
        language="Chinese",
        context=_context(),
        hotwords=hotwords,
        connect_factory=connect_factory,
        raw_event_sink=audited.append if audited is not None else None,
        handshake_timeout_secs=0.2,
        finish_timeout_secs=finish_timeout_secs,
    )


def _handshake() -> list[str]:
    return [
        json.dumps({"event": "started"}),
        json.dumps({"event": "language_set", "language": "Chinese"}),
        json.dumps({"event": "hotwords_set", "hotwords": ["Fun-ASR", "语音助手"]}),
    ]


async def _next_event(events: AsyncIterator[ASREvent]) -> ASREvent:
    return await anext(events)


async def test_connect_sends_official_commands_before_binary_audio() -> None:
    stream = FakeFunASRWebSocket(_handshake())
    adapter = _adapter(stream)

    await adapter.connect()
    await adapter.send_audio(b"\x01\x02")

    assert stream.sent == [
        "START",
        "LANGUAGE:Chinese",
        "HOTWORDS:Fun-ASR,语音助手",
        b"\x01\x02",
    ]
    assert adapter.uri == "ws://127.0.0.1:10095"
    assert adapter.capabilities.supports_partial
    assert not adapter.capabilities.supports_segment_timestamps
    assert adapter.capabilities.supports_hotwords
    assert adapter.capabilities.supports_eof_flush


async def test_events_normalize_ready_partial_sentences_and_final() -> None:
    stream = FakeFunASRWebSocket(_handshake())
    adapter = _adapter(stream)
    await adapter.connect()
    events = adapter.events()

    assert (await _next_event(events)).kind == "ready"
    stream.push(
        {
            "sentences": [{"text": "你好", "start": 0, "end": 400, "spk": 2}],
            "partial": "下一句",
            "duration_ms": 400,
            "is_final": False,
        }
    )
    snapshot = await _next_event(events)
    assert snapshot.kind == "snapshot"
    assert snapshot.window is not None
    assert snapshot.window.partial == "下一句"
    assert snapshot.window.segments[0].text == "你好"
    assert snapshot.window.segments[0].start_ms == 1_000
    assert snapshot.window.segments[0].end_ms == 1_400
    assert snapshot.window.segments[0].speaker_key == "epoch:3:speaker:2"

    stream.push(
        {
            "sentences": [{"text": "最终句", "start": 400, "end": 900}],
            "partial": "",
            "duration_ms": 900,
            "is_final": True,
        }
    )
    final = await _next_event(events)
    assert final.kind == "final"
    assert final.window is not None
    assert final.window.segments[0].text == "最终句"


async def test_raw_events_are_audited_without_leaking_into_domain_event() -> None:
    audited: list[dict[str, object]] = []
    stream = FakeFunASRWebSocket(_handshake())
    adapter = _adapter(stream, hotwords=(), audited=audited)
    await adapter.connect()
    events = adapter.events()
    assert (await _next_event(events)).kind == "ready"
    stream.push({"partial": "审计", "sentences": [], "duration_ms": 0, "is_final": False})
    snapshot = await _next_event(events)

    assert snapshot.metadata == {"backend_id": "funasr-nano-ws", "is_final": False}
    assert audited == [
        {"event": "started"},
        {"event": "language_set", "language": "Chinese"},
        {"event": "hotwords_set", "hotwords": ["Fun-ASR", "语音助手"]},
        {"partial": "审计", "sentences": [], "duration_ms": 0, "is_final": False},
    ]


async def test_service_error_is_stable_and_does_not_expose_stack() -> None:
    stream = FakeFunASRWebSocket(_handshake())
    adapter = _adapter(stream, hotwords=())
    await adapter.connect()
    events = adapter.events()
    assert (await _next_event(events)).kind == "ready"
    stream.push({"event": "error", "error": "model unavailable", "traceback": "secret"})

    event = await _next_event(events)
    assert event.kind == "error"
    assert event.error_code == "FUNASR_WS_ERROR"
    assert event.error_message == "model unavailable"
    assert "traceback" not in event.error_message.lower()


async def test_disconnect_is_reported_as_stable_error() -> None:
    stream = FakeFunASRWebSocket(_handshake())
    adapter = _adapter(stream, hotwords=())
    await adapter.connect()
    events = adapter.events()
    assert (await _next_event(events)).kind == "ready"
    stream.disconnect()

    event = await _next_event(events)
    assert event.kind == "error"
    assert event.error_code == "FUNASR_WS_DISCONNECTED"


async def test_finish_sends_stop_once_and_waits_for_final_result() -> None:
    stream = FakeFunASRWebSocket(_handshake())
    adapter = _adapter(stream, hotwords=(), finish_timeout_secs=0.5)
    await adapter.connect()
    events = adapter.events()
    assert (await _next_event(events)).kind == "ready"
    consumer = asyncio.create_task(_next_event(events))

    finish_one = asyncio.create_task(adapter.finish())
    finish_two = asyncio.create_task(adapter.finish())
    await asyncio.sleep(0)
    stream.push(
        {
            "sentences": [{"text": "收尾", "start": 0, "end": 100}],
            "partial": "",
            "duration_ms": 100,
            "is_final": True,
        }
    )
    final = await asyncio.wait_for(finish_one, timeout=1)
    second = await asyncio.wait_for(finish_two, timeout=1)
    event = await asyncio.wait_for(consumer, timeout=1)

    assert event.kind == "final"
    assert final == second
    assert stream.sent.count("STOP") == 1


async def test_finish_timeout_has_stable_error() -> None:
    stream = FakeFunASRWebSocket(_handshake())
    adapter = _adapter(stream, hotwords=(), finish_timeout_secs=0.01)
    await adapter.connect()

    with pytest.raises(TimeoutError, match="FUNASR_FINAL_TIMEOUT"):
        await adapter.finish()
    assert stream.sent.count("STOP") == 1


async def test_missing_timestamps_are_rejected_without_synthetic_segments() -> None:
    stream = FakeFunASRWebSocket(_handshake())
    adapter = _adapter(stream, hotwords=())
    await adapter.connect()
    events = adapter.events()
    assert (await _next_event(events)).kind == "ready"
    stream.push(
        {
            "sentences": [{"text": "没有时间戳"}],
            "partial": "",
            "duration_ms": 500,
            "is_final": False,
        }
    )

    error = await _next_event(events)
    snapshot = await _next_event(events)
    assert error.kind == "error"
    assert error.error_code == "FUNASR_INVALID_TIMESTAMPS"
    assert snapshot.kind == "snapshot"
    assert snapshot.window is not None
    assert snapshot.window.segments == ()


async def test_non_monotonic_or_out_of_duration_timestamps_are_rejected() -> None:
    stream = FakeFunASRWebSocket(_handshake())
    adapter = _adapter(stream, hotwords=())
    await adapter.connect()
    events = adapter.events()
    assert (await _next_event(events)).kind == "ready"
    stream.push(
        {
            "sentences": [
                {"text": "第一段", "start": 100, "end": 200},
                {"text": "逆序", "start": 50, "end": 80},
                {"text": "越界", "start": 200, "end": 501},
            ],
            "partial": "",
            "duration_ms": 500,
            "is_final": False,
        }
    )

    error = await _next_event(events)
    snapshot = await _next_event(events)
    assert error.error_code == "FUNASR_INVALID_TIMESTAMPS"
    assert snapshot.window is not None
    assert [segment.text for segment in snapshot.window.segments] == ["第一段"]


@pytest.mark.parametrize(
    "payload",
    [
        {"partial": {"text": "wrong type"}, "sentences": [], "is_final": False},
        {"partial": "x" * 100_001, "sentences": [], "is_final": False},
        {"partial": "", "sentences": [], "is_final": "true"},
        {
            "partial": "",
            "sentences": [{"text": ["wrong type"], "start": 0, "end": 1}],
            "is_final": False,
        },
    ],
)
async def test_invalid_result_field_types_are_stable_protocol_errors(
    payload: dict[str, object],
) -> None:
    stream = FakeFunASRWebSocket(_handshake())
    adapter = _adapter(stream)
    await adapter.connect()
    events = adapter.events()
    assert (await _next_event(events)).kind == "ready"
    stream.push(payload)

    error = await _next_event(events)

    assert error.kind == "error"
    assert error.error_code == "FUNASR_WS_PROTOCOL_ERROR"
    assert "traceback" not in (error.error_message or "").lower()


async def test_close_is_idempotent() -> None:
    stream = FakeFunASRWebSocket(_handshake())
    adapter = _adapter(stream, hotwords=())
    await adapter.connect()

    await adapter.close()
    await adapter.close()
    assert stream.closed


def test_public_constructor_rejects_invalid_timeout() -> None:
    stream = FakeFunASRWebSocket(_handshake())

    async def connect_factory(_url: str) -> FakeFunASRWebSocket:
        return stream

    with pytest.raises(ValueError, match="timeout"):
        FunASRNanoWSAdapter(
            url="ws://127.0.0.1:10095",
            language="Chinese",
            context=_context(),
            connect_factory=connect_factory,
            handshake_timeout_secs=0,
        )
