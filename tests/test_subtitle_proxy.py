"""SubtitleProxy 单元测试（Mock SubtitleStream，测去重广播/暂停语义）。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from voice_realtime.asr.adapters.wlk import TranscriptNormalizer
from voice_realtime.asr.contracts import ASREvent, ASRSessionContext
from voice_realtime.config import SubtitleSettings
from voice_realtime.meeting.models import PCMOwner, TranscriptWindow
from voice_realtime.subtitles.events import SubtitleEvent
from voice_realtime.ui.subtitle_proxy import (
    CapturePreparation,
    FinalizationTimeout,
    SubtitlePreparation,
    SubtitleProxy,
    SubtitleProxyDiagnostics,
    SubtitleProxyState,
    TranscriptionGap,
)

CONF = {"host": "127.0.0.1", "port": 8001, "language": "Chinese"}


def _snapshot(text: str, speaker: int = 1, start: str = "0:00:01", end: str = "0:00:02") -> dict:
    return {
        "type": "full_update",
        "buffer_transcription": "",
        "lines": [
            {
                "speaker": speaker if speaker != -2 else -2,
                "text": text,
                "start": start,
                "end": end,
                "translation": None,
                "detected_language": "zh",
            }
        ],
        "remaining_time": 0,
    }


class FakeStream:
    """模拟 SubtitleStream：events() 产出给定事件序列。"""

    def __init__(self, events: list[SubtitleEvent]) -> None:
        self._events = events
        self.sent: list[bytes] = []
        self.closed = False

    async def connect(self) -> None:
        self.closed = False
        if hasattr(self, "_events_queue"):
            self._events_queue.put_nowait(
                SubtitleEvent(kind="config", raw={"type": "config"})
            )
        return

    @property
    def uri(self) -> str:
        return "ws://mock/asr?language=Chinese&mode=full"

    async def events(self) -> AsyncIterator[SubtitleEvent]:
        for evt in self._events:
            yield evt
        # 永不结束（生产连接是持续的）；测试通过 cancel 停止
        await asyncio.Event().wait()

    async def send_audio(self, chunk: bytes) -> None:
        self.sent.append(chunk)

    async def close(self) -> None:
        self.closed = True


class FlushableFakeStream(FakeStream):
    """收到 EOF 后发送 ready_to_stop，模拟 WLK 最终冲刷。"""

    def __init__(self, final_payload: dict[str, object]) -> None:
        super().__init__([])
        self._final_payload = final_payload
        self._events_queue: asyncio.Queue[SubtitleEvent] = asyncio.Queue()
        self._events_queue.put_nowait(
            SubtitleEvent(kind="config", raw={"type": "config"})
        )

    async def events(self) -> AsyncIterator[SubtitleEvent]:
        while not self.closed:
            event = await self._events_queue.get()
            yield event

    async def send_audio(self, chunk: bytes) -> None:
        self.sent.append(chunk)
        if chunk == b"":
            self._events_queue.put_nowait(
                SubtitleEvent(kind="confirmed", raw=self._final_payload)
            )
            self._events_queue.put_nowait(SubtitleEvent(kind="ready_to_stop", raw={}))


class StreamSequence:
    """按普通字幕、会议捕获、恢复字幕的顺序返回预置连接。"""

    def __init__(self, *streams: FakeStream) -> None:
        self._streams = iter(streams)
        self.created: list[FakeStream] = []

    def __call__(self, **_kwargs: object) -> FakeStream:
        stream = next(self._streams)
        self.created.append(stream)
        return stream


class ControlledTranscriber:
    """可显式发送 ready/断线的后端无关流。"""

    def __init__(self) -> None:
        self.connected = asyncio.Event()
        self.sent_audio: list[bytes] = []
        self.closed = False
        self._events: asyncio.Queue[ASREvent | None] = asyncio.Queue()

    @property
    def uri(self) -> str:
        return "ws://mock/asr"

    async def connect(self) -> None:
        self.closed = False
        self.connected.set()

    async def send_audio(self, chunk: bytes) -> None:
        self.sent_audio.append(chunk)

    async def events(self) -> AsyncIterator[ASREvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    async def finish(self) -> TranscriptWindow:
        await self.send_audio(b"")
        return TranscriptWindow(source_epoch=0)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._events.put_nowait(None)

    async def emit(self, event: ASREvent) -> None:
        self._events.put_nowait(event)
        await asyncio.sleep(0)

    async def disconnect(self) -> None:
        self._events.put_nowait(None)
        await asyncio.sleep(0)


class BlockingTranscriber(ControlledTranscriber):
    """阻塞 PCM 发送，以便测试排队与关闭顺序。"""

    def __init__(self) -> None:
        super().__init__()
        self.send_started = asyncio.Event()
        self.allow_send = asyncio.Event()
        self.operations: list[tuple[str, bytes | None]] = []

    async def send_audio(self, chunk: bytes) -> None:
        self.send_started.set()
        await self.allow_send.wait()
        self.sent_audio.append(chunk)
        self.operations.append(("send", chunk))

    async def finish(self) -> TranscriptWindow:
        self.operations.append(("finish", None))
        return TranscriptWindow(source_epoch=0)


class FakeClock:
    """可精确推进的 monotonic clock。"""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def _wait_until(predicate: Callable[[], bool], *, attempts: int = 50) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met")


async def _start_meeting_capture(
    proxy: SubtitleProxy, owner: str = "meeting:test"
) -> None:
    preparation = await proxy.prepare_capture(owner, timeout_secs=5.0)
    proxy.commit_capture(preparation)


@pytest.fixture()
def settings(tmp_path: Path) -> SubtitleSettings:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    return SubtitleSettings(
        **CONF,
        model_dir=model_dir,
        output_dir=tmp_path / "subtitles",
    )


class TestClientManagement:
    async def test_add_remove_client(self, settings: SubtitleSettings) -> None:
        proxy = SubtitleProxy(settings)
        send = AsyncMock()
        proxy.add_client(send)
        assert proxy.has_clients
        proxy.remove_client(send)
        assert not proxy.has_clients

    async def test_add_client_replays_latest_snapshot(self, settings: SubtitleSettings) -> None:
        proxy = SubtitleProxy(settings)
        payload = _snapshot("历史第一句")
        await proxy._broadcast_payload(payload)

        send = AsyncMock()
        proxy.add_client(send)
        await asyncio.sleep(0.01)

        assert send.await_count == 1
        replayed = json.loads(send.await_args.args[0])
        assert replayed["lines"][0]["text"] == "历史第一句"
        await proxy.stop()

    async def test_clear_subtitles_resets_snapshot_and_broadcasts_empty(
        self, settings: SubtitleSettings
    ) -> None:
        proxy = SubtitleProxy(settings)
        send = AsyncMock()
        proxy.add_client(send)

        await proxy._broadcast_payload(_snapshot("旧内容"))
        await asyncio.sleep(0.01)
        assert send.await_count == 1

        await proxy.clear_subtitles()
        await asyncio.sleep(0.01)
        assert send.await_count == 2
        cleared = json.loads(send.await_args.args[0])
        assert cleared["lines"] == []
        assert cleared["buffer_transcription"] == ""
        await proxy.stop()


class TestPreparedLifecycle:
    async def test_start_initializes_without_connecting(
        self, settings: SubtitleSettings
    ) -> None:
        stream = ControlledTranscriber()
        factory = Mock(return_value=stream)
        proxy = SubtitleProxy(settings, transcriber_factory=factory)

        await proxy.start()

        assert factory.call_count == 0
        assert proxy.state == "paused"
        assert proxy.browser_capture_active is False
        assert proxy._supervisor_task is None
        await proxy.stop()

    async def test_prepare_waits_ready_but_rejects_pcm_until_commit(
        self, settings: SubtitleSettings
    ) -> None:
        stream = ControlledTranscriber()
        proxy = SubtitleProxy(
            settings,
            transcriber_factory=Mock(return_value=stream),
        )
        proxy.add_client(AsyncMock())
        await proxy.start()

        task = asyncio.create_task(proxy.prepare_browser_capture(timeout_secs=0.2))
        await stream.connected.wait()
        await stream.emit(ASREvent(kind="ready"))
        preparation = await task
        assert isinstance(preparation, SubtitlePreparation)

        await proxy.push_audio(b"before")
        await asyncio.sleep(0)
        assert stream.sent_audio == []
        assert proxy.browser_capture_active is False

        proxy.commit_browser_capture(preparation)
        await proxy.push_audio(b"after")
        await _wait_until(lambda: stream.sent_audio == [b"after"])
        assert proxy.browser_capture_active is True
        await proxy.deactivate_browser_capture()
        await proxy.stop()

    async def test_prepare_timeout_closes_stream_and_returns_to_paused(
        self, settings: SubtitleSettings
    ) -> None:
        stream = ControlledTranscriber()
        proxy = SubtitleProxy(
            settings,
            transcriber_factory=Mock(return_value=stream),
        )
        await proxy.start()

        with pytest.raises(RuntimeError, match="未发送 config"):
            await proxy.prepare_browser_capture(timeout_secs=0.01)

        assert stream.closed
        assert proxy.state == "paused"
        assert proxy.browser_capture_active is False
        assert proxy._supervisor_task is None
        await proxy.stop()

    async def test_browser_preparation_token_is_single_use_and_abortable(
        self, settings: SubtitleSettings
    ) -> None:
        first = ControlledTranscriber()
        second = ControlledTranscriber()
        streams = iter((first, second))
        proxy = SubtitleProxy(
            settings,
            transcriber_factory=lambda _context: next(streams),
        )
        await proxy.start()

        first_task = asyncio.create_task(
            proxy.prepare_browser_capture(timeout_secs=0.2)
        )
        await first.connected.wait()
        await first.emit(ASREvent(kind="ready"))
        first_preparation = await first_task
        assert first_preparation.generation == 1
        proxy.commit_browser_capture(first_preparation)

        with pytest.raises(RuntimeError, match="preparation"):
            proxy.commit_browser_capture(first_preparation)
        with pytest.raises(RuntimeError, match="preparation"):
            await proxy.abort_browser_capture(first_preparation)

        await proxy.deactivate_browser_capture()
        second_task = asyncio.create_task(
            proxy.prepare_browser_capture(timeout_secs=0.2)
        )
        await second.connected.wait()
        await second.emit(ASREvent(kind="ready"))
        second_preparation = await second_task
        assert second_preparation.generation == 2
        await proxy.abort_browser_capture(second_preparation)

        assert second.closed
        with pytest.raises(RuntimeError, match="preparation"):
            await proxy.abort_browser_capture(second_preparation)
        with pytest.raises(RuntimeError, match="preparation"):
            proxy.commit_browser_capture(first_preparation)
        await proxy.stop()

    async def test_deactivate_closes_tasks_stream_and_clears_audio(
        self, settings: SubtitleSettings
    ) -> None:
        stream = ControlledTranscriber()
        proxy = SubtitleProxy(
            settings,
            transcriber_factory=Mock(return_value=stream),
        )
        proxy.add_client(AsyncMock())
        await proxy.start()
        task = asyncio.create_task(proxy.prepare_browser_capture(timeout_secs=0.2))
        await stream.connected.wait()
        await stream.emit(ASREvent(kind="ready"))
        preparation = await task
        proxy.commit_browser_capture(preparation)
        proxy._audio_buffer.put_nowait(b"queued")

        await proxy.deactivate_browser_capture()

        assert stream.closed
        assert proxy.browser_capture_active is False
        assert proxy.state == "paused"
        assert proxy._supervisor_task is None
        assert proxy._audio_buffer.empty()
        assert not proxy._browser_ready.is_set()
        await proxy.stop()

    async def test_browser_reconnects_only_after_commit(
        self, settings: SubtitleSettings
    ) -> None:
        prepared = ControlledTranscriber()
        active = ControlledTranscriber()
        reconnected = ControlledTranscriber()
        streams = iter((prepared, active, reconnected))
        factory = Mock(side_effect=lambda _context: next(streams))
        proxy = SubtitleProxy(
            settings,
            transcriber_factory=factory,
            backoff_delays=(0.01,),
        )
        await proxy.start()

        prepared_task = asyncio.create_task(
            proxy.prepare_browser_capture(timeout_secs=0.2)
        )
        await prepared.connected.wait()
        await prepared.emit(ASREvent(kind="ready"))
        prepared_token = await prepared_task
        await prepared.disconnect()
        await _wait_until(lambda: proxy.state == "paused")
        assert factory.call_count == 1
        await proxy.abort_browser_capture(prepared_token)

        active_task = asyncio.create_task(
            proxy.prepare_browser_capture(timeout_secs=0.2)
        )
        await active.connected.wait()
        await active.emit(ASREvent(kind="ready"))
        active_token = await active_task
        proxy.commit_browser_capture(active_token)
        await active.disconnect()

        await reconnected.connected.wait()
        await reconnected.emit(ASREvent(kind="ready"))
        await _wait_until(lambda: proxy.state == "connected")
        assert factory.call_count == 3
        await proxy.deactivate_browser_capture()
        await proxy.stop()

class TestMeetingCapture:
    def test_subtitle_proxy_has_no_implicit_begin_capture_api(self) -> None:
        assert not hasattr(SubtitleProxy, "begin_capture")

    async def test_meeting_prepare_timeout_closes_prepared_stream(
        self, settings: SubtitleSettings
    ) -> None:
        stream = ControlledTranscriber()
        proxy = SubtitleProxy(
            settings,
            transcriber_factory=Mock(return_value=stream),
        )
        await proxy.start()

        with pytest.raises(RuntimeError, match="未发送 config"):
            await proxy.prepare_capture("meeting:timeout", timeout_secs=0.01)

        assert stream.closed
        assert proxy.capture_owner is None
        assert proxy.state == "paused"
        await proxy.stop()

    async def test_meeting_capture_preparation_is_silent_until_commit(
        self, settings: SubtitleSettings
    ) -> None:
        stream = ControlledTranscriber()
        proxy = SubtitleProxy(
            settings,
            transcriber_factory=Mock(return_value=stream),
        )
        await proxy.start()

        task = asyncio.create_task(
            proxy.prepare_capture("meeting:abc", timeout_secs=0.2)
        )
        await stream.connected.wait()
        await stream.emit(ASREvent(kind="ready"))
        preparation = await task
        assert isinstance(preparation, CapturePreparation)

        await proxy.push_audio(b"before")
        await asyncio.sleep(0)
        assert stream.sent_audio == []

        proxy.commit_capture(preparation)
        await proxy.push_audio(b"after")
        await _wait_until(lambda: stream.sent_audio == [b"after"])
        await proxy.abort_capture()

        assert proxy.capture_owner is None
        assert proxy.browser_capture_active is False
        assert proxy.state == "paused"
        assert proxy._supervisor_task is None
        await proxy.stop()

    async def test_capture_preparation_token_is_single_use_and_abortable(
        self, settings: SubtitleSettings
    ) -> None:
        first = ControlledTranscriber()
        second = ControlledTranscriber()
        streams = iter((first, second))
        proxy = SubtitleProxy(
            settings,
            transcriber_factory=lambda _context: next(streams),
        )
        await proxy.start()

        first_task = asyncio.create_task(
            proxy.prepare_capture("meeting:first", timeout_secs=0.2)
        )
        await first.connected.wait()
        await first.emit(ASREvent(kind="ready"))
        first_preparation = await first_task
        proxy.commit_capture(first_preparation)
        with pytest.raises(RuntimeError, match="preparation"):
            proxy.commit_capture(first_preparation)
        with pytest.raises(RuntimeError, match="preparation"):
            await proxy.abort_prepared_capture(first_preparation)
        await proxy.abort_capture()

        second_task = asyncio.create_task(
            proxy.prepare_capture("meeting:second", timeout_secs=0.2)
        )
        await second.connected.wait()
        await second.emit(ASREvent(kind="ready"))
        second_preparation = await second_task
        await proxy.abort_prepared_capture(second_preparation)

        assert second.closed
        with pytest.raises(RuntimeError, match="preparation"):
            await proxy.abort_prepared_capture(second_preparation)
        with pytest.raises(RuntimeError, match="preparation"):
            proxy.commit_capture(first_preparation)
        assert proxy.capture_owner is None
        assert proxy.state == "paused"
        await proxy.stop()

    async def test_send_loop_waits_for_reconnected_stream_with_pending_audio(
        self,
        settings: SubtitleSettings,
    ) -> None:
        old_stream = FakeStream([])
        new_stream = FakeStream([])
        proxy = SubtitleProxy(settings)
        proxy._capture_owner = "meeting:test"
        proxy._capture_accept_audio = True
        proxy._capture_active.set()
        proxy._capture_stream = old_stream  # type: ignore[assignment]
        proxy._capture_stream_available.set()

        task = asyncio.create_task(proxy._capture_send_loop(old_stream))  # type: ignore[arg-type]
        await asyncio.sleep(0)
        proxy._capture_stream = None
        proxy._capture_stream_available.clear()
        chunk = b"\x00" * 3_200
        proxy._audio_buffer.put_nowait(chunk)
        await asyncio.sleep(0)

        assert not task.done()

        proxy._capture_stream = new_stream  # type: ignore[assignment]
        proxy._capture_stream_available.set()
        await asyncio.wait_for(proxy._audio_buffer.join(), timeout=1)
        assert new_stream.sent == [chunk]

        proxy._capture_owner = None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_reconnect_reports_actual_backoff_audio_gap_and_new_offset(
        self,
        settings: SubtitleSettings,
    ) -> None:
        old_stream = FakeStream([])
        new_stream = FakeStream([])
        contexts: list[ASRSessionContext] = []

        def create(context: ASRSessionContext) -> FakeStream:
            contexts.append(context)
            return new_stream

        proxy = SubtitleProxy(
            settings,
            transcriber_factory=create,  # type: ignore[arg-type]
            backoff_delays=(0.01,),
        )
        gaps: list[TranscriptionGap] = []

        async def record_gap(gap: TranscriptionGap) -> None:
            gaps.append(gap)

        proxy.add_gap_listener(record_gap)
        proxy._running = True
        proxy._capture_owner = "meeting:test"
        proxy._capture_accept_audio = True
        proxy._capture_epoch = 1
        proxy._capture_offset_ms = 0
        proxy._capture_audio_ms = 1_000
        proxy._capture_input_ms = 1_000
        proxy._capture_stream = old_stream  # type: ignore[assignment]

        reconnect = asyncio.create_task(proxy._reconnect_capture(old_stream))  # type: ignore[arg-type]
        for _ in range(20):
            if proxy.state == "backoff":
                break
            await asyncio.sleep(0)
        await proxy.push_audio(b"\x00" * 3_200)
        result = await reconnect

        assert result is new_stream
        assert gaps == [TranscriptionGap(source_epoch=2, start_ms=1_000, end_ms=1_100)]
        assert contexts[0].offset_ms == 1_100

    async def test_reconnect_does_not_emit_zero_length_gap(
        self,
        settings: SubtitleSettings,
    ) -> None:
        old_stream = FakeStream([])
        new_stream = FakeStream([])
        proxy = SubtitleProxy(
            settings,
            transcriber_factory=lambda _context: new_stream,  # type: ignore[arg-type]
            backoff_delays=(0.001,),
        )
        listener = AsyncMock()
        proxy.add_gap_listener(listener)
        proxy._running = True
        proxy._capture_owner = "meeting:test"
        proxy._capture_accept_audio = True
        proxy._capture_audio_ms = 1_000
        proxy._capture_input_ms = 1_000
        proxy._capture_stream = old_stream  # type: ignore[assignment]

        await proxy._reconnect_capture(old_stream)  # type: ignore[arg-type]

        listener.assert_not_awaited()

    async def test_finish_capture_does_not_resume_browser_supervisor(
        self, settings: SubtitleSettings
    ) -> None:
        capture = FlushableFakeStream(_snapshot("尾句"))
        factory = StreamSequence(capture)
        proxy = SubtitleProxy(settings, stream_factory=factory)
        await proxy.start()

        await _start_meeting_capture(proxy)
        await proxy.finish_capture(timeout_secs=1)
        await asyncio.sleep(0)

        assert proxy.capture_owner is None
        assert proxy.browser_capture_active is False
        assert proxy.state == "paused"
        assert proxy._supervisor_task is None
        assert factory.created == [capture]
        await proxy.stop()

    async def test_abort_capture_does_not_resume_browser_supervisor(
        self, settings: SubtitleSettings
    ) -> None:
        capture = FlushableFakeStream(_snapshot("中止"))
        factory = StreamSequence(capture)
        proxy = SubtitleProxy(settings, stream_factory=factory)
        await proxy.start()

        await _start_meeting_capture(proxy)
        await proxy.abort_capture()
        await asyncio.sleep(0)

        assert proxy.capture_owner is None
        assert proxy.browser_capture_active is False
        assert proxy.state == "paused"
        assert proxy._supervisor_task is None
        assert factory.created == [capture]
        await proxy.stop()

    async def test_capture_timeout_does_not_resume_browser_supervisor(
        self, settings: SubtitleSettings
    ) -> None:
        capture = FlushableFakeStream(_snapshot("不会 ready"))
        capture._events_queue = asyncio.Queue()
        factory = StreamSequence(capture)
        proxy = SubtitleProxy(settings, stream_factory=factory)
        await proxy.start()

        await _start_meeting_capture(proxy)
        capture._events_queue = asyncio.Queue()
        with pytest.raises(FinalizationTimeout):
            await proxy.finish_capture(timeout_secs=0.01)
        await asyncio.sleep(0)

        assert proxy.capture_owner is None
        assert proxy.browser_capture_active is False
        assert proxy.state == "paused"
        assert proxy._supervisor_task is None
        assert factory.created == [capture]
        await proxy.stop()

    async def test_stop_during_capture_does_not_resume_browser_supervisor(
        self, settings: SubtitleSettings
    ) -> None:
        capture = FlushableFakeStream(_snapshot("关闭"))
        factory = StreamSequence(capture)
        proxy = SubtitleProxy(settings, stream_factory=factory)
        await proxy.start()

        await _start_meeting_capture(proxy)
        await proxy.stop()

        assert proxy.capture_owner is None
        assert proxy.state == "stopped"
        assert proxy._supervisor_task is None
        assert factory.created == [capture]

    async def test_capture_does_not_write_legacy_srt(
        self, settings: SubtitleSettings
    ) -> None:
        stream = FlushableFakeStream(_snapshot("只写 PostgreSQL"))
        proxy = SubtitleProxy(settings, stream_factory=lambda **_: stream)
        await proxy.start()
        await _start_meeting_capture(proxy)
        await asyncio.sleep(0)
        await proxy.finish_capture(timeout_secs=1)

        assert not (settings.output_dir / "current.srt").exists()
        await proxy.stop()

    async def test_finish_capture_sends_empty_pcm_and_waits_ready(
        self, settings: SubtitleSettings
    ) -> None:
        stream = FlushableFakeStream(_snapshot("尾句"))
        proxy = SubtitleProxy(settings, stream_factory=lambda **_: stream)
        await proxy.start()
        await _start_meeting_capture(proxy)

        final = await proxy.finish_capture(timeout_secs=1)

        assert stream.sent[-1] == b""
        assert final.segments[-1].text == "尾句"
        assert proxy.capture_owner is None
        await proxy.stop()

    async def test_finish_rejects_new_audio_but_drains_already_accepted_audio(
        self, settings: SubtitleSettings
    ) -> None:
        stream = BlockingTranscriber()
        proxy = SubtitleProxy(
            settings,
            transcriber_factory=Mock(return_value=stream),
        )
        await proxy.start()
        preparation_task = asyncio.create_task(
            proxy.prepare_capture("meeting:drain", timeout_secs=0.2)
        )
        await stream.connected.wait()
        await stream.emit(ASREvent(kind="ready"))
        preparation = await preparation_task
        proxy.commit_capture(preparation)

        first = b"a" * 32
        second = b"b" * 32
        late = b"c" * 32
        await proxy.push_audio(first)
        await stream.send_started.wait()
        await proxy.push_audio(second)

        finish_task = asyncio.create_task(proxy.finish_capture(timeout_secs=1))
        await asyncio.sleep(0)
        await proxy.push_audio(late)
        stream.allow_send.set()
        await finish_task

        assert stream.operations == [
            ("send", first),
            ("send", second),
            ("finish", None),
        ]
        assert late not in stream.sent_audio
        await proxy.stop()

    async def test_finish_times_out_before_eof_when_accepted_audio_cannot_drain(
        self, settings: SubtitleSettings
    ) -> None:
        stream = BlockingTranscriber()
        proxy = SubtitleProxy(
            settings,
            transcriber_factory=Mock(return_value=stream),
        )
        await proxy.start()
        preparation_task = asyncio.create_task(
            proxy.prepare_capture("meeting:blocked-drain", timeout_secs=0.2)
        )
        await stream.connected.wait()
        await stream.emit(ASREvent(kind="ready"))
        preparation = await preparation_task
        proxy.commit_capture(preparation)
        last_window = TranscriptNormalizer().normalize(_snapshot("上一句"), 1, 0)
        proxy._capture_last_window = last_window

        await proxy.push_audio(b"a" * 32)
        await stream.send_started.wait()
        await proxy.push_audio(b"b" * 32)
        assert proxy._audio_buffer.qsize() == 1

        with pytest.raises(FinalizationTimeout) as exc_info:
            await proxy.finish_capture(timeout_secs=0.01)

        assert exc_info.value.last_window is last_window
        assert stream.operations == []
        assert stream.closed
        assert proxy.capture_owner is None
        await proxy.stop()

    async def test_capture_accepts_audio_without_browser_clients(
        self, settings: SubtitleSettings
    ) -> None:
        stream = FlushableFakeStream(_snapshot("持续记录"))
        proxy = SubtitleProxy(settings, stream_factory=lambda **_: stream)
        await proxy.start()
        await _start_meeting_capture(proxy)
        assert not proxy.is_paused
        await proxy.push_audio(b"pcm")
        await asyncio.sleep(0)

        assert b"pcm" in stream.sent
        await proxy.abort_capture()
        await proxy.stop()

    async def test_finish_capture_timeout_preserves_last_window(
        self, settings: SubtitleSettings
    ) -> None:
        stream = FlushableFakeStream(_snapshot("不会 ready"))
        proxy = SubtitleProxy(settings, stream_factory=lambda **_: stream)
        await proxy.start()
        await _start_meeting_capture(proxy)
        proxy._capture_last_window = TranscriptNormalizer().normalize(
            _snapshot("上一句"), 1, 0
        )
        stream._events_queue = asyncio.Queue()

        with pytest.raises(FinalizationTimeout) as exc_info:
            await proxy.finish_capture(timeout_secs=0.01)

        assert isinstance(exc_info.value.last_window, TranscriptWindow)
        assert proxy.capture_owner is None
        await proxy.stop()


class TestBroadcast:
    async def test_broadcast_domain_snapshot_to_clients(
        self, settings: SubtitleSettings
    ) -> None:
        """统一 snapshot 应经 presenter 广播兼容 payload。"""
        proxy = SubtitleProxy(settings)
        client = AsyncMock()
        proxy.add_client(client)
        window = TranscriptNormalizer().normalize(_snapshot("你好"), 0, 0)

        await proxy._handle_stream_event(ASREvent(kind="snapshot", window=window))
        await asyncio.sleep(0)

        assert client.call_count == 1
        payload = json.loads(client.call_args.args[0])
        assert payload["lines"][0]["text"] == "你好"

    async def test_duplicate_confirmed_not_rebroadcast(self, settings: SubtitleSettings) -> None:
        """同一 (start, text) 的 confirmed 事件去重，只广播一次。"""
        window = TranscriptNormalizer().normalize(_snapshot("重复"), 0, 0)
        evt1 = ASREvent(kind="snapshot", window=window)
        evt2 = ASREvent(kind="snapshot", window=window)
        proxy = SubtitleProxy(settings)
        client = AsyncMock()
        proxy.add_client(client)

        await proxy._handle_stream_event(evt1)
        await proxy._handle_stream_event(evt2)
        assert client.call_count == 1

    async def test_partial_update_replaces(self, settings: SubtitleSettings) -> None:
        """partial 相同文本不重复广播；新文本则广播。"""
        proxy = SubtitleProxy(settings)
        client = AsyncMock()
        proxy.add_client(client)
        evt1 = ASREvent(
            kind="snapshot", window=TranscriptWindow(source_epoch=0, partial="正在")
        )
        evt2 = ASREvent(
            kind="snapshot", window=TranscriptWindow(source_epoch=0, partial="正在")
        )
        evt3 = ASREvent(
            kind="snapshot", window=TranscriptWindow(source_epoch=0, partial="正在转写")
        )

        await proxy._handle_stream_event(evt1)
        await proxy._handle_stream_event(evt2)
        await proxy._handle_stream_event(evt3)
        assert client.call_count == 2  # evt1 + evt3

    async def test_existing_confirmed_line_does_not_hide_new_partial(
        self, settings: SubtitleSettings
    ) -> None:
        proxy = SubtitleProxy(settings)
        client = AsyncMock()
        proxy.add_client(client)
        first = _snapshot("已确认")
        first["buffer_transcription"] = "新内容"
        second = _snapshot("已确认")
        second["buffer_transcription"] = "新内容继续"

        await proxy._broadcast_payload(first)
        await proxy._broadcast_payload(second)
        await asyncio.sleep(0)

        assert client.await_count == 2

    async def test_slow_or_failed_client_does_not_block_others(
        self, settings: SubtitleSettings
    ) -> None:
        proxy = SubtitleProxy(settings)
        blocked = asyncio.Event()

        async def slow(_text: str) -> None:
            await blocked.wait()

        fast = AsyncMock()
        proxy.add_client(slow)
        proxy.add_client(fast)

        await asyncio.wait_for(proxy._broadcast_payload(_snapshot("你好")), timeout=0.1)
        await asyncio.sleep(0)

        fast.assert_awaited_once()
        blocked.set()
        await proxy.stop()

    async def test_failed_client_is_removed(self, settings: SubtitleSettings) -> None:
        proxy = SubtitleProxy(settings)

        async def failed(_text: str) -> None:
            raise ConnectionError("browser gone")

        proxy.add_client(failed)
        await proxy._broadcast_payload(_snapshot("你好"))
        await asyncio.sleep(0)

        assert not proxy.has_clients

    async def test_slow_client_queue_is_bounded(self, settings: SubtitleSettings) -> None:
        proxy = SubtitleProxy(settings)
        blocked = asyncio.Event()

        async def slow(_text: str) -> None:
            await blocked.wait()

        proxy.add_client(slow)
        for index in range(20):
            await proxy._broadcast_payload(_snapshot(f"第 {index} 句", start=str(index)))

        channel = proxy._clients[slow]
        assert channel.queue.qsize() <= proxy._CLIENT_QUEUE_SIZE
        blocked.set()
        await proxy.stop()


class TestStreamConnection:
    async def test_prepare_uses_ws_scheme(self, settings: SubtitleSettings) -> None:
        """回归：wlk WS 连接必须用 ws:// scheme（http:// 会被 websockets 拒绝，
        此前导致 SubtitleStream.connect 抛 InvalidURI、字幕链路全断）。"""
        with patch("voice_realtime.asr.adapters.wlk.SubtitleStream") as mock_stream:
            proxy = SubtitleProxy(settings)
            mock_stream.return_value.connect = AsyncMock()
            mock_stream.return_value.close = AsyncMock()

            async def ready_events() -> AsyncIterator[SubtitleEvent]:
                yield SubtitleEvent(kind="config", raw={"type": "config"})
                await asyncio.Event().wait()
                if False:  # pragma: no cover - 仅用于把该挂起协程声明为 async generator
                    yield SubtitleEvent(kind="other")

            mock_stream.return_value.events = ready_events
            await proxy.start()
            try:
                preparation = await proxy.prepare_browser_capture(timeout_secs=0.2)
                mock_stream.assert_called_once()
                url = mock_stream.call_args.kwargs["url"]
                assert url == f"ws://{settings.host}:{settings.port}"
                mock_stream.return_value.connect.assert_awaited_once()
                await proxy.abort_browser_capture(preparation)
            finally:
                await proxy.stop()
            mock_stream.return_value.close.assert_awaited_once()

    async def test_disconnect_reconnects_without_replacing_browser_clients(
        self, settings: SubtitleSettings
    ) -> None:
        first = ControlledTranscriber()
        second = ControlledTranscriber()
        streams = iter((first, second))
        factory = Mock(side_effect=lambda _context: next(streams))
        proxy = SubtitleProxy(
            settings,
            transcriber_factory=factory,
            backoff_delays=(0.01,),
        )
        client = AsyncMock()
        proxy.add_client(client)

        await proxy.start()
        task = asyncio.create_task(proxy.prepare_browser_capture(timeout_secs=0.2))
        await first.connected.wait()
        await first.emit(ASREvent(kind="ready"))
        preparation = await task
        proxy.commit_browser_capture(preparation)
        await first.disconnect()
        await second.connected.wait()
        await second.emit(ASREvent(kind="ready"))
        await _wait_until(lambda: proxy.state == "connected")

        assert factory.call_count == 2
        assert proxy.state == "connected"
        assert proxy.has_clients
        await proxy.deactivate_browser_capture()
        await proxy.stop()
        assert proxy.state == "stopped"

    async def test_stop_cancels_backoff_immediately(self, settings: SubtitleSettings) -> None:
        first = ControlledTranscriber()

        class OfflineStream(ControlledTranscriber):
            async def connect(self) -> None:
                raise ConnectionRefusedError("wlk down")

        streams = iter((first, OfflineStream()))
        proxy = SubtitleProxy(
            settings,
            transcriber_factory=lambda _context: next(streams),
            backoff_delays=(30.0,),
        )
        await proxy.start()
        task = asyncio.create_task(proxy.prepare_browser_capture(timeout_secs=0.2))
        await first.connected.wait()
        await first.emit(ASREvent(kind="ready"))
        preparation = await task
        proxy.commit_browser_capture(preparation)
        await first.disconnect()
        await _wait_until(lambda: proxy.state == "backoff")

        await asyncio.wait_for(proxy.stop(), timeout=0.1)

        assert proxy.state == "stopped"


class TestSubtitleEpoch:
    async def test_reconnect_archives_resets_and_isolates_zero_timeline(
        self, settings: SubtitleSettings
    ) -> None:
        first = ControlledTranscriber()
        second = ControlledTranscriber()
        contexts: list[ASRSessionContext] = []
        streams = iter((first, second))

        def create(context: ASRSessionContext) -> ControlledTranscriber:
            contexts.append(context)
            return next(streams)

        proxy = SubtitleProxy(
            settings,
            transcriber_factory=create,
            backoff_delays=(0.01,),
        )
        client_messages: list[dict[str, object]] = []

        async def collect(message: str) -> None:
            client_messages.append(json.loads(message))

        proxy.add_client(collect)
        await proxy.start()
        prepare_task = asyncio.create_task(
            proxy.prepare_browser_capture(timeout_secs=0.2)
        )
        await first.connected.wait()
        await first.emit(ASREvent(kind="ready"))
        preparation = await prepare_task
        proxy.commit_browser_capture(preparation)
        first_window = TranscriptNormalizer().normalize(
            _snapshot("第一段"), preparation.generation, 0
        )
        await first.emit(ASREvent(kind="snapshot", window=first_window))
        await first.disconnect()
        await second.connected.wait()

        archives = list(settings.output_dir.glob("session-*.srt"))
        assert len(archives) == 1
        assert "第一段" in archives[0].read_text(encoding="utf-8")
        assert (settings.output_dir / "current.srt").read_text(encoding="utf-8") == ""
        assert not (settings.output_dir / "current.srt.tmp").exists()
        assert {"type": "reset", "source_epoch": preparation.generation} in client_messages
        assert [context.source_epoch for context in contexts] == [1, 2]

        new_client = AsyncMock()
        proxy.add_client(new_client)
        await asyncio.sleep(0)
        new_client.assert_not_awaited()

        await second.emit(ASREvent(kind="ready"))
        second_window = TranscriptNormalizer().normalize(_snapshot("第二段"), 2, 0)
        await second.emit(ASREvent(kind="snapshot", window=second_window))
        await proxy.deactivate_browser_capture()

        archives = sorted(settings.output_dir.glob("session-*.srt"))
        assert len(archives) == 2
        assert sum("第一段" in archive.read_text(encoding="utf-8") for archive in archives) == 1
        assert sum("第二段" in archive.read_text(encoding="utf-8") for archive in archives) == 1
        resets = [message for message in client_messages if message.get("type") == "reset"]
        assert resets == [
            {"type": "reset", "source_epoch": 1},
            {"type": "reset", "source_epoch": 2},
        ]
        assert (settings.output_dir / "current.srt").read_text(encoding="utf-8") == ""
        await proxy.stop()

    async def test_epoch_without_confirmed_clears_state_without_empty_archive(
        self, settings: SubtitleSettings
    ) -> None:
        stream = ControlledTranscriber()
        proxy = SubtitleProxy(
            settings,
            transcriber_factory=Mock(return_value=stream),
        )
        await proxy.start()
        prepare_task = asyncio.create_task(
            proxy.prepare_browser_capture(timeout_secs=0.2)
        )
        await stream.connected.wait()
        await stream.emit(ASREvent(kind="ready"))
        preparation = await prepare_task
        proxy.commit_browser_capture(preparation)
        await stream.emit(
            ASREvent(
                kind="snapshot",
                window=TranscriptWindow(
                    source_epoch=preparation.generation,
                    partial="未确认",
                ),
            )
        )

        await proxy.deactivate_browser_capture()

        assert list(settings.output_dir.glob("session-*.srt")) == []
        assert (settings.output_dir / "current.srt").read_text(encoding="utf-8") == ""
        assert proxy._last_payload is None
        assert proxy._snapshot_signature is None
        assert proxy._persisted_confirmed_signature is None
        assert proxy._session_has_confirmed is False
        assert not proxy._browser_ready.is_set()

        new_client = AsyncMock()
        proxy.add_client(new_client)
        await asyncio.sleep(0)
        new_client.assert_not_awaited()
        await proxy.stop()


class TestDiagnostics:
    async def test_frozen_snapshot_tracks_event_age_without_silence_degradation(
        self, settings: SubtitleSettings
    ) -> None:
        clock = FakeClock(10.0)
        stream = ControlledTranscriber()
        proxy = SubtitleProxy(
            settings,
            transcriber_factory=Mock(return_value=stream),
            clock=clock,
        )

        assert proxy.diagnostics(PCMOwner.NONE) == SubtitleProxyDiagnostics(
            workload="paused",
            ws_state="paused",
            reconnect_count=0,
            last_event_age_ms=None,
            dropped_chunks=0,
            gap_count=0,
        )

        await proxy.start()
        prepare_task = asyncio.create_task(
            proxy.prepare_browser_capture(timeout_secs=0.2)
        )
        await stream.connected.wait()
        starting = proxy.diagnostics(PCMOwner.SUBTITLES)
        assert starting.workload == "starting"
        assert starting.ws_state == "connected"
        await stream.emit(ASREvent(kind="ready"))
        preparation = await prepare_task
        proxy.commit_browser_capture(preparation)
        clock.advance(0.5)
        assert proxy.diagnostics(PCMOwner.SUBTITLES).last_event_age_ms == 500
        await stream.emit(
            ASREvent(
                kind="snapshot",
                window=TranscriptWindow(
                    source_epoch=preparation.generation,
                    partial="事件更新时间",
                ),
            )
        )
        clock.advance(3_600.25)

        diagnostics = proxy.diagnostics(PCMOwner.SUBTITLES)
        assert diagnostics.workload == "ready"
        assert diagnostics.ws_state == "connected"
        assert diagnostics.last_event_age_ms == 3_600_250
        assert proxy.diagnostics(PCMOwner.NONE).last_event_age_ms is None
        with pytest.raises(FrozenInstanceError):
            diagnostics.workload = "degraded"  # type: ignore[misc]

        proxy._browser_ready.clear()
        assert proxy.diagnostics(PCMOwner.SUBTITLES).workload == "degraded"
        proxy._state = SubtitleProxyState.BACKOFF
        backoff = proxy.diagnostics(PCMOwner.SUBTITLES)
        assert backoff.workload == "degraded"
        assert backoff.ws_state == "backoff"
        proxy._state = SubtitleProxyState.ERROR
        failed = proxy.diagnostics(PCMOwner.SUBTITLES)
        assert failed.workload == "error"
        assert failed.ws_state == "error"
        await proxy.stop()

    async def test_reconnect_drop_and_gap_counters_are_independent(
        self, settings: SubtitleSettings
    ) -> None:
        first = ControlledTranscriber()
        second = ControlledTranscriber()
        meeting = ControlledTranscriber()
        meeting_reconnected = ControlledTranscriber()
        streams = iter((first, second, meeting, meeting_reconnected))
        proxy = SubtitleProxy(
            settings,
            transcriber_factory=lambda _context: next(streams),
            backoff_delays=(0.01,),
        )
        await proxy.start()

        browser_task = asyncio.create_task(
            proxy.prepare_browser_capture(timeout_secs=0.2)
        )
        await first.connected.wait()
        await first.emit(ASREvent(kind="ready"))
        browser_preparation = await browser_task
        proxy.commit_browser_capture(browser_preparation)
        await first.disconnect()
        await second.connected.wait()
        await second.emit(ASREvent(kind="ready"))
        await _wait_until(lambda: proxy.state == "connected")
        assert proxy.diagnostics(PCMOwner.SUBTITLES).reconnect_count == 1
        await proxy.deactivate_browser_capture()

        meeting_task = asyncio.create_task(
            proxy.prepare_capture("meeting:diagnostics", timeout_secs=0.2)
        )
        await meeting.connected.wait()
        await meeting.emit(ASREvent(kind="ready"))
        meeting_preparation = await meeting_task
        proxy.commit_capture(meeting_preparation)
        await meeting.disconnect()
        await meeting_reconnected.connected.wait()
        await meeting_reconnected.emit(ASREvent(kind="ready"))
        await _wait_until(lambda: proxy.state == "connected")
        meeting_diagnostics = proxy.diagnostics(PCMOwner.MEETING)
        assert meeting_diagnostics.workload == "ready"
        assert meeting_diagnostics.ws_state == "meeting"
        assert meeting_diagnostics.reconnect_count == 2
        await proxy.abort_capture()

        proxy._audio_buffer = asyncio.Queue(maxsize=1)
        proxy._audio_buffer.put_nowait(b"old")
        proxy._browser_capture_active = True
        proxy._browser_stream = second
        proxy._browser_ready.set()
        proxy._state = SubtitleProxyState.CONNECTED
        await proxy.push_audio(b"new")
        await proxy._notify_capture_gap(0, 10)

        diagnostics = proxy.diagnostics(PCMOwner.SUBTITLES)
        assert diagnostics.reconnect_count == 2
        assert diagnostics.dropped_chunks == 1
        assert diagnostics.gap_count == 1

        await proxy.deactivate_browser_capture()
        await proxy.stop()


class TestAudioPush:
    async def test_committed_browser_capture_sends_without_clients(
        self, settings: SubtitleSettings
    ) -> None:
        stream = ControlledTranscriber()
        proxy = SubtitleProxy(
            settings,
            transcriber_factory=Mock(return_value=stream),
        )
        await proxy.start()
        preparation_task = asyncio.create_task(
            proxy.prepare_browser_capture(timeout_secs=0.2)
        )
        await stream.connected.wait()
        await stream.emit(ASREvent(kind="ready"))
        preparation = await preparation_task
        proxy.commit_browser_capture(preparation)

        await proxy.push_audio(b"pcm")
        await _wait_until(lambda: stream.sent_audio == [b"pcm"])

        assert proxy.browser_capture_active is True
        await proxy.deactivate_browser_capture()
        await proxy.stop()

    async def test_last_client_removal_preserves_active_browser_audio(
        self, settings: SubtitleSettings
    ) -> None:
        stream = BlockingTranscriber()
        proxy = SubtitleProxy(
            settings,
            transcriber_factory=Mock(return_value=stream),
        )
        client = AsyncMock()
        proxy.add_client(client)
        await proxy.start()
        preparation_task = asyncio.create_task(
            proxy.prepare_browser_capture(timeout_secs=0.2)
        )
        await stream.connected.wait()
        await stream.emit(ASREvent(kind="ready"))
        preparation = await preparation_task
        proxy.commit_browser_capture(preparation)

        first = b"first"
        queued = b"queued"
        after_remove = b"after-remove"
        await proxy.push_audio(first)
        await stream.send_started.wait()
        await proxy.push_audio(queued)
        proxy.remove_client(client)
        await proxy.push_audio(after_remove)
        stream.allow_send.set()
        await _wait_until(
            lambda: stream.sent_audio == [first, queued, after_remove]
        )

        assert proxy.browser_capture_active is True
        await proxy.deactivate_browser_capture()
        await proxy.stop()

    async def test_push_audio_discarded_when_no_client(self, settings: SubtitleSettings) -> None:
        """无浏览器订阅时丢弃音频，恢复订阅后不发送历史帧。"""
        fake = FakeStream([])
        proxy = SubtitleProxy(settings)
        proxy._browser_stream = fake  # type: ignore[assignment]
        await proxy.push_audio(b"\x00" * 512)
        assert proxy._audio_buffer.qsize() == 0

    async def test_push_audio_batch_sends_without_client_when_active(
        self, settings: SubtitleSettings
    ) -> None:
        """兼容批量入口只服从 workload 生命周期。"""
        fake = FakeStream([])
        proxy = SubtitleProxy(settings)
        proxy._browser_stream = fake  # type: ignore[assignment]
        proxy._browser_capture_active = True
        proxy._browser_ready.set()
        proxy._running = True
        proxy._state = SubtitleProxyState.CONNECTED

        proxy._audio_buffer.put_nowait(b"\x01" * 512)
        await proxy._push_audio_batch()
        assert fake.sent == [b"\x01" * 512]
        await asyncio.wait_for(proxy._audio_buffer.join(), timeout=0.1)

    async def test_audio_during_backoff_is_discarded(self, settings: SubtitleSettings) -> None:
        proxy = SubtitleProxy(settings)
        proxy._running = True
        proxy._state = SubtitleProxyState.BACKOFF
        proxy.add_client(AsyncMock())

        await proxy.push_audio(b"old")

        assert proxy._audio_buffer.empty()


class TestSrtPersistence:
    async def test_confirmed_snapshot_is_atomically_persisted_and_archived(
        self, tmp_path: Path
    ) -> None:
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        settings = SubtitleSettings(
            **CONF,
            model_dir=model_dir,
            output_dir=tmp_path / "subtitles",
        )
        stream = ControlledTranscriber()
        proxy = SubtitleProxy(
            settings,
            transcriber_factory=Mock(return_value=stream),
        )
        await proxy.start()
        prepare_task = asyncio.create_task(
            proxy.prepare_browser_capture(timeout_secs=0.2)
        )
        await stream.connected.wait()
        await stream.emit(ASREvent(kind="ready"))
        preparation = await prepare_task
        proxy.commit_browser_capture(preparation)

        window = TranscriptNormalizer().normalize(
            _snapshot("你好"), preparation.generation, 0
        )
        await stream.emit(ASREvent(kind="snapshot", window=window))

        current = settings.output_dir / "current.srt"
        assert current.exists()
        assert not (settings.output_dir / "current.srt.tmp").exists()
        assert "00:00:01,000 --> 00:00:02,000" in current.read_text()

        await proxy.stop()

        archives = list(settings.output_dir.glob("session-*.srt"))
        assert len(archives) == 1
        assert "你好" in archives[0].read_text(encoding="utf-8")
        assert current.read_text(encoding="utf-8") == ""
