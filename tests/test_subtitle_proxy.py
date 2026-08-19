"""SubtitleProxy 单元测试（Mock SubtitleStream，测去重广播/暂停语义）。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest

from voice_realtime.config import SubtitleSettings
from voice_realtime.subtitles.events import SubtitleEvent
from voice_realtime.ui.subtitle_proxy import SubtitleProxy

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


@pytest.fixture()
def settings() -> SubtitleSettings:
    return SubtitleSettings(**CONF)


class TestClientManagement:
    async def test_add_remove_client(self, settings: SubtitleSettings) -> None:
        proxy = SubtitleProxy(settings)
        send = AsyncMock()
        proxy.add_client(send)
        assert proxy.has_clients
        proxy.remove_client(send)
        assert not proxy.has_clients


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


class TestStreamConnection:
    async def test_start_uses_ws_scheme(self, settings: SubtitleSettings) -> None:
        """回归：wlk WS 连接必须用 ws:// scheme（http:// 会被 websockets 拒绝，
        此前导致 SubtitleStream.connect 抛 InvalidURI、字幕链路全断）。"""
        proxy = SubtitleProxy(settings)
        with patch("voice_realtime.ui.subtitle_proxy.SubtitleStream") as mock_stream:
            mock_stream.return_value.connect = AsyncMock()
            mock_stream.return_value.close = AsyncMock()

            async def no_events() -> AsyncIterator[SubtitleEvent]:
                await asyncio.Event().wait()

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


class TestAudioPush:
    async def test_push_audio_queued_when_no_client(self, settings: SubtitleSettings) -> None:
        """无浏览器订阅时音频入队但不推送（_paused=True 生效）。"""
        fake = FakeStream([])
        proxy = SubtitleProxy(settings)
        proxy._stream = fake  # type: ignore[assignment]
        await proxy.push_audio(b"\x00" * 512)
        assert proxy._audio_buffer.qsize() == 1

    async def test_send_audio_when_client_present(self, settings: SubtitleSettings) -> None:
        """有浏览器订阅时，音频经 _push_audio_batch 送到 wlk。"""
        fake = FakeStream([])
        proxy = SubtitleProxy(settings)
        proxy._stream = fake  # type: ignore[assignment]
        proxy.add_client(AsyncMock())

        await proxy.push_audio(b"\x01" * 512)
        await proxy._push_audio_batch()
        assert fake.sent == [b"\x01" * 512]
