"""SubtitleProxy 单元测试（Mock SubtitleStream，测去重广播/暂停语义）。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from voice_realtime.config import SubtitleSettings
from voice_realtime.meeting.models import TranscriptWindow
from voice_realtime.subtitles.events import SubtitleEvent
from voice_realtime.ui.subtitle_proxy import (
    FinalizationTimeout,
    SubtitleProxy,
    SubtitleProxyState,
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


class TestMeetingCapture:
    async def test_capture_does_not_write_legacy_srt(
        self, settings: SubtitleSettings
    ) -> None:
        stream = FlushableFakeStream(_snapshot("只写 PostgreSQL"))
        proxy = SubtitleProxy(settings, stream_factory=lambda **_: stream)
        await proxy.start()
        await proxy.begin_capture("meeting:test")
        await asyncio.sleep(0)
        await proxy.finish_capture(timeout_secs=1)

        assert not (settings.output_dir / "current.srt").exists()

    async def test_finish_capture_sends_empty_pcm_and_waits_ready(
        self, settings: SubtitleSettings
    ) -> None:
        stream = FlushableFakeStream(_snapshot("尾句"))
        proxy = SubtitleProxy(settings, stream_factory=lambda **_: stream)
        await proxy.start()
        await proxy.begin_capture("meeting:test")

        final = await proxy.finish_capture(timeout_secs=1)

        assert stream.sent[-1] == b""
        assert final.segments[-1].text == "尾句"
        assert proxy.capture_owner is None

    async def test_capture_accepts_audio_without_browser_clients(
        self, settings: SubtitleSettings
    ) -> None:
        stream = FlushableFakeStream(_snapshot("持续记录"))
        proxy = SubtitleProxy(settings, stream_factory=lambda **_: stream)
        await proxy.start()
        await proxy.begin_capture("meeting:test")
        assert not proxy.is_paused
        await proxy.push_audio(b"pcm")
        await asyncio.sleep(0)

        assert b"pcm" in stream.sent
        await proxy.abort_capture()

    async def test_finish_capture_timeout_preserves_last_window(
        self, settings: SubtitleSettings
    ) -> None:
        stream = FlushableFakeStream(_snapshot("不会 ready"))
        proxy = SubtitleProxy(settings, stream_factory=lambda **_: stream)
        await proxy.start()
        await proxy.begin_capture("meeting:test")
        proxy._capture_last_window = proxy._normalizer.normalize(_snapshot("上一句"), 1, 0)
        stream._events_queue = asyncio.Queue()

        with pytest.raises(FinalizationTimeout) as exc_info:
            await proxy.finish_capture(timeout_secs=0.01)

        assert isinstance(exc_info.value.last_window, TranscriptWindow)
        assert proxy.capture_owner is None


class TestBroadcast:
    async def test_broadcast_raw_payload_to_clients(self, settings: SubtitleSettings) -> None:
        """新 confirmed 事件应广播完整 raw payload 给浏览器。"""
        evt = SubtitleEvent(
            kind="confirmed",
            text="你好",
            start="0:00:01",
            end="0:00:02",
            speaker=1,
            raw=_snapshot("你好"),
        )
        fake = FakeStream([evt])
        proxy = SubtitleProxy(settings)
        proxy._stream = fake  # type: ignore[assignment]
        client = AsyncMock()
        proxy.add_client(client)

        with patch.object(proxy, "_task", create=True):
            task = asyncio.create_task(proxy._process_loop())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert client.call_count == 1
        payload = json.loads(client.call_args.args[0])
        assert payload["lines"][0]["text"] == "你好"

    async def test_duplicate_confirmed_not_rebroadcast(self, settings: SubtitleSettings) -> None:
        """同一 (start, text) 的 confirmed 事件去重，只广播一次。"""
        evt1 = SubtitleEvent(
            kind="confirmed",
            text="重复",
            start="0:00:01",
            end="0:00:02",
            speaker=1,
            raw=_snapshot("重复"),
        )
        evt2 = SubtitleEvent(
            kind="confirmed",
            text="重复",
            start="0:00:01",
            end="0:00:02",
            speaker=1,
            raw=_snapshot("重复"),
        )
        fake = FakeStream([evt1, evt2])
        proxy = SubtitleProxy(settings)
        proxy._stream = fake  # type: ignore[assignment]
        client = AsyncMock()
        proxy.add_client(client)

        # 直接调用广播方法（不依赖 _process_loop 挂起）
        await proxy._broadcast_event(evt1)
        await proxy._broadcast_event(evt2)
        assert client.call_count == 1

    async def test_partial_update_replaces(self, settings: SubtitleSettings) -> None:
        """partial 相同文本不重复广播；新文本则广播。"""
        proxy = SubtitleProxy(settings)
        client = AsyncMock()
        proxy.add_client(client)
        evt1 = SubtitleEvent(kind="partial", text="正在", raw={"buffer_transcription": "正在"})
        evt2 = SubtitleEvent(kind="partial", text="正在", raw={"buffer_transcription": "正在"})
        evt3 = SubtitleEvent(
            kind="partial", text="正在转写", raw={"buffer_transcription": "正在转写"}
        )

        await proxy._broadcast_event(evt1)
        await proxy._broadcast_event(evt2)
        await proxy._broadcast_event(evt3)
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
    async def test_start_uses_ws_scheme(self, settings: SubtitleSettings) -> None:
        """回归：wlk WS 连接必须用 ws:// scheme（http:// 会被 websockets 拒绝，
        此前导致 SubtitleStream.connect 抛 InvalidURI、字幕链路全断）。"""
        with patch("voice_realtime.ui.subtitle_proxy.SubtitleStream") as mock_stream:
            proxy = SubtitleProxy(settings)
            mock_stream.return_value.connect = AsyncMock()
            mock_stream.return_value.close = AsyncMock()

            async def no_events() -> AsyncIterator[SubtitleEvent]:
                await asyncio.Event().wait()
                if False:  # pragma: no cover - 仅用于把该挂起协程声明为 async generator
                    yield SubtitleEvent(kind="other")

            mock_stream.return_value.events = no_events
            await proxy.start()
            try:
                mock_stream.assert_called_once()
                url = mock_stream.call_args.kwargs["url"]
                assert url == f"ws://{settings.host}:{settings.port}"
                mock_stream.return_value.connect.assert_awaited_once()
            finally:
                await proxy.stop()
            mock_stream.return_value.close.assert_awaited_once()

    async def test_disconnect_reconnects_without_replacing_browser_clients(
        self, settings: SubtitleSettings
    ) -> None:
        attempts = 0

        class ReconnectingStream(FakeStream):
            async def connect(self) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise ConnectionRefusedError("wlk down")

        streams: list[ReconnectingStream] = []

        def factory(**_kwargs: object) -> ReconnectingStream:
            stream = ReconnectingStream([])
            streams.append(stream)
            return stream

        proxy = SubtitleProxy(settings, stream_factory=factory, backoff_delays=(0.01,))
        client = AsyncMock()
        proxy.add_client(client)

        await proxy.start()
        for _ in range(20):
            if attempts >= 2 and proxy.state == "connected":
                break
            await asyncio.sleep(0.01)

        assert attempts >= 2
        assert proxy.state == "connected"
        assert proxy.has_clients
        await proxy.stop()
        assert proxy.state == "stopped"

    async def test_stop_cancels_backoff_immediately(self, settings: SubtitleSettings) -> None:
        class OfflineStream(FakeStream):
            async def connect(self) -> None:
                raise ConnectionRefusedError("wlk down")

        proxy = SubtitleProxy(
            settings,
            stream_factory=lambda **_kwargs: OfflineStream([]),
            backoff_delays=(30.0,),
        )
        await proxy.start()
        for _ in range(20):
            if proxy.state == "backoff":
                break
            await asyncio.sleep(0)

        await asyncio.wait_for(proxy.stop(), timeout=0.1)

        assert proxy.state == "stopped"


class TestAudioPush:
    async def test_push_audio_discarded_when_no_client(self, settings: SubtitleSettings) -> None:
        """无浏览器订阅时丢弃音频，恢复订阅后不发送历史帧。"""
        fake = FakeStream([])
        proxy = SubtitleProxy(settings)
        proxy._stream = fake  # type: ignore[assignment]
        await proxy.push_audio(b"\x00" * 512)
        assert proxy._audio_buffer.qsize() == 0

    async def test_send_audio_when_client_present(self, settings: SubtitleSettings) -> None:
        """有浏览器订阅时，音频经 _push_audio_batch 送到 wlk。"""
        fake = FakeStream([])
        proxy = SubtitleProxy(settings)
        proxy._stream = fake  # type: ignore[assignment]
        proxy._running = True
        proxy._state = SubtitleProxyState.CONNECTED
        proxy.add_client(AsyncMock())

        await proxy.push_audio(b"\x01" * 512)
        await proxy._push_audio_batch()
        assert fake.sent == [b"\x01" * 512]

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
        proxy = SubtitleProxy(settings)

        await proxy._broadcast_payload(_snapshot("你好"))

        current = settings.output_dir / "current.srt"
        assert current.exists()
        assert not (settings.output_dir / "current.srt.tmp").exists()
        assert "00:00:01,000 --> 00:00:02,000" in current.read_text()

        await proxy.stop()

        archives = list(settings.output_dir.glob("session-*.srt"))
        assert len(archives) == 1
        assert archives[0].read_text() == current.read_text()
