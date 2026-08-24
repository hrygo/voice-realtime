"""WhisperLiveKit ASR adapter 的领域事件与兼容输出测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from voice_realtime.asr.adapters.wlk import WLKStreamingAdapter
from voice_realtime.asr.contracts import ASREvent, ASRSessionContext
from voice_realtime.asr.presenters import legacy_ready_payload, legacy_subtitle_payload
from voice_realtime.subtitles.events import SubtitleEvent


def _snapshot() -> dict[str, object]:
    return {
        "type": "full_update",
        "buffer_transcription": "下一句",
        "lines": [
            {
                "speaker": 2,
                "text": "第一句",
                "start": "0:00:01.00",
                "end": "0:00:02.50",
                "translation": "first",
                "detected_language": "zh",
            }
        ],
    }


class FakeWLKStream:
    def __init__(self, events: list[SubtitleEvent]) -> None:
        self._queue: asyncio.Queue[SubtitleEvent] = asyncio.Queue()
        for event in events:
            self._queue.put_nowait(event)
        self.sent: list[bytes] = []
        self.connected = False
        self.closed = False

    @property
    def uri(self) -> str:
        return "ws://127.0.0.1:8001/asr?language=Chinese&mode=full"

    async def connect(self) -> None:
        self.connected = True

    async def send_audio(self, chunk: bytes) -> None:
        self.sent.append(chunk)
        if chunk == b"":
            self._queue.put_nowait(SubtitleEvent(kind="ready_to_stop", raw={}))

    async def events(self) -> AsyncIterator[SubtitleEvent]:
        while not self.closed:
            yield await self._queue.get()

    async def close(self) -> None:
        self.closed = True


async def test_wlk_snapshot_is_atomic_and_does_not_leak_raw_payload() -> None:
    raw = _snapshot()
    stream = FakeWLKStream(
        [
            SubtitleEvent(kind="config", raw={"type": "config"}),
            SubtitleEvent(kind="confirmed", text="第一句", raw=raw),
            SubtitleEvent(kind="partial", text="下一句", raw=raw),
            SubtitleEvent(kind="ready_to_stop", raw={"type": "ready_to_stop"}),
        ]
    )
    adapter = WLKStreamingAdapter(
        url="ws://127.0.0.1:8001",
        language="Chinese",
        context=ASRSessionContext(source_epoch=2, offset_ms=30_000, purpose="meeting"),
        stream_factory=lambda **_kwargs: stream,
    )

    await adapter.connect()
    assert adapter.capabilities.supports_segment_timestamps
    assert not adapter.capabilities.supports_word_timestamps
    assert adapter.capabilities.supports_speaker_labels
    events: list[ASREvent] = []
    async for event in adapter.events():
        events.append(event)
        if event.kind == "final":
            break

    assert [event.kind for event in events] == ["ready", "snapshot", "final"]
    snapshot = events[1].window
    assert snapshot is not None
    assert snapshot.source_epoch == 2
    assert snapshot.partial == "下一句"
    assert snapshot.segments[0].start_ms == 31_000
    assert snapshot.segments[0].speaker_key == "epoch:2:speaker:2"
    assert "raw" not in events[1].metadata


async def test_wlk_adapter_can_audit_raw_vendor_events_outside_domain_events() -> None:
    raw = _snapshot()
    stream = FakeWLKStream(
        [
            SubtitleEvent(kind="config", raw={"type": "config"}),
            SubtitleEvent(kind="confirmed", text="第一句", raw=raw),
            SubtitleEvent(kind="ready_to_stop", raw={"type": "ready_to_stop"}),
        ]
    )
    audited: list[dict[str, object]] = []
    adapter = WLKStreamingAdapter(
        url="ws://127.0.0.1:8001",
        language="Chinese",
        context=ASRSessionContext(source_epoch=2, offset_ms=0, purpose="subtitles"),
        stream_factory=lambda **_kwargs: stream,
        raw_event_sink=audited.append,
    )

    await adapter.connect()
    async for event in adapter.events():
        if event.kind == "final":
            break

    assert audited == [
        {"type": "config"},
        raw,
        {"type": "ready_to_stop"},
    ]


def test_legacy_presenter_builds_stable_browser_payload() -> None:
    raw = _snapshot()
    stream = FakeWLKStream([])
    adapter = WLKStreamingAdapter(
        url="ws://127.0.0.1:8001",
        language="Chinese",
        context=ASRSessionContext(source_epoch=2, offset_ms=30_000, purpose="subtitles"),
        stream_factory=lambda **_kwargs: stream,
    )
    window = adapter.normalize_snapshot(raw)

    payload = legacy_subtitle_payload(window)

    assert payload["type"] == "full_update"
    assert payload["buffer_transcription"] == "下一句"
    assert payload["lines"] == [
        {
            "speaker": 2,
            "text": "第一句",
            "start": "0:00:31.000",
            "end": "0:00:32.500",
            "translation": "first",
            "detected_language": "zh",
        }
    ]


def test_legacy_ready_payload_preserves_browser_pcm_contract() -> None:
    assert legacy_ready_payload() == {
        "type": "config",
        "useAudioWorklet": False,
        "mode": "full",
    }


async def test_finish_sends_one_empty_pcm_and_waits_for_final() -> None:
    raw = _snapshot()
    stream = FakeWLKStream(
        [
            SubtitleEvent(kind="config", raw={"type": "config"}),
            SubtitleEvent(kind="confirmed", text="第一句", raw=raw),
        ]
    )
    adapter = WLKStreamingAdapter(
        url="ws://127.0.0.1:8001",
        language="Chinese",
        context=ASRSessionContext(source_epoch=1, offset_ms=0, purpose="meeting"),
        stream_factory=lambda **_kwargs: stream,
    )
    await adapter.connect()

    observed: list[ASREvent] = []

    async def consume() -> None:
        async for event in adapter.events():
            observed.append(event)
            if event.kind == "final":
                return

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0)
    final = await asyncio.wait_for(adapter.finish(), timeout=1)
    await consumer

    assert stream.sent == [b""]
    assert final.segments[0].text == "第一句"
    assert observed[-1].kind == "final"
