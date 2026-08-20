"""AudioHub 单元测试（Mock pyaudio，测 sink 注册/扇出/生命周期）。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from voice_realtime.audio.hub import AudioHub


@pytest.fixture()
def mock_pyaudio() -> MagicMock:
    """替换 pyaudio 模块：PyAudio() 实例的 open() 返回恒定 bytes 流。"""
    mock_stream = MagicMock()
    mock_stream.read.return_value = b"\x00" * 1024
    mock_pa = MagicMock()
    mock_pa.PyAudio.return_value.open.return_value = mock_stream
    mock_pa.PyAudio.return_value.get_default_input_device_info.return_value = {
        "index": 0,
        "name": "mock-mic",
    }
    mock_pa.PyAudio.return_value.get_device_info_by_index.return_value = {
        "name": "mock-mic",
        "maxInputChannels": 1,
    }
    with patch("voice_realtime.audio.hub.pyaudio", mock_pa):
        yield mock_pa


class TestSinkManagement:
    async def test_add_sink_duplicate_raises(self) -> None:
        hub = AudioHub()
        hub.add_sink("a", AsyncMock())
        with pytest.raises(ValueError, match="已存在"):
            hub.add_sink("a", AsyncMock())

    async def test_remove_sink_idempotent(self) -> None:
        hub = AudioHub()
        hub.add_sink("a", AsyncMock())
        await hub.remove_sink("a")
        await hub.remove_sink("a")  # 第二次调用不应报错

    async def test_sinks_isolated(self) -> None:
        hub = AudioHub()
        assert not hub._sinks
        hub.add_sink("a", AsyncMock())
        assert len(hub._sinks) == 1


class TestFanout:
    async def test_both_sinks_receive_each_chunk(self, mock_pyaudio: MagicMock) -> None:
        """一个音频块扇出到两个 sink。"""
        hub = AudioHub(chunk_size=512, throttle_secs=0.005)
        sink_a = AsyncMock()
        sink_b = AsyncMock()
        hub.add_sink("a", sink_a)
        hub.add_sink("b", sink_b)

        await hub.start()
        assert hub._qaudio is not None
        await asyncio.sleep(0.05)
        await hub.stop()

        assert sink_a.call_count >= 1
        assert sink_b.call_count >= 1
        assert sink_a.call_args.args[0] == b"\x00" * 1024
        assert sink_b.call_args.args[0] == b"\x00" * 1024

    async def test_no_sinks_still_runs(self, mock_pyaudio: MagicMock) -> None:
        """无 sink 也不抛错（扇出空转）。"""
        hub = AudioHub(throttle_secs=0.005)
        await hub.start()
        await asyncio.sleep(0.05)
        await hub.stop()  # 不应抛异常

    async def test_dispatch_error_isolated(self, mock_pyaudio: MagicMock) -> None:
        """一个 sink 抛错不影响其他 sink 和采集循环。"""
        hub = AudioHub(throttle_secs=0.005)
        bad_sink = AsyncMock(side_effect=RuntimeError("boom"))
        good_sink = AsyncMock()
        hub.add_sink("bad", bad_sink)
        hub.add_sink("good", good_sink)
        await hub.start()
        await asyncio.sleep(0.05)
        await hub.stop()
        assert good_sink.call_count >= 1


class TestLifecycle:
    async def test_start_propagates_stream_open_failure(self) -> None:
        mock_pa = MagicMock()
        mock_pa.PyAudio.return_value.open.side_effect = OSError("permission denied")
        with patch("voice_realtime.audio.hub.pyaudio", mock_pa):
            hub = AudioHub()
            with pytest.raises(OSError, match="permission denied"):
                await hub.start()
        mock_pa.PyAudio.return_value.terminate.assert_called_once()

    async def test_start_twice_noop(self, mock_pyaudio: MagicMock) -> None:
        hub = AudioHub()
        await hub.start()
        await hub.start()  # 第二次不应重新创建 task
        await hub.stop()

    async def test_stop_terminates_pyaudio(self, mock_pyaudio: MagicMock) -> None:
        hub = AudioHub()
        await hub.start()
        await hub.stop()
        assert hub._qaudio is None
        mock_pyaudio.PyAudio.return_value.terminate.assert_called_once()

    async def test_read_oserror_does_not_kill_loop(self) -> None:
        """设备断流（read 持续抛 OSError）时循环重试不退出，stop 正常终止。"""
        state = {"reads": 0}

        def failing_read(*_args: object, **_kwargs: object) -> bytes:
            state["reads"] += 1
            raise OSError("device gone")

        mock_stream = MagicMock()
        mock_stream.read.side_effect = failing_read
        mock_pa = MagicMock()
        mock_pa.PyAudio.return_value.open.return_value = mock_stream
        with patch("voice_realtime.audio.hub.pyaudio", mock_pa):
            hub = AudioHub(throttle_secs=0.005)
            sink = AsyncMock()
            hub.add_sink("a", sink)
            await hub.start()
            await asyncio.sleep(0.35)
            await hub.stop()

        # 循环在 OSError 下持续重试（reads > 1），未被异常杀死
        assert state["reads"] > 1
        assert sink.call_count == 0


class TestDefaultDevice:
    async def test_default_device_info_dict_does_not_crash(self, mock_pyaudio: MagicMock) -> None:
        """回归：真实 pyaudio 默认设备信息是 dict（含 index 键），不得当整数传给
        get_device_info_by_index（此前 TypeError 导致 AudioHub.start 崩溃、页面无声）。"""
        hub = AudioHub(chunk_size=512, throttle_secs=0.005)  # device_index=None → 默认设备路径
        sink = AsyncMock()
        hub.add_sink("a", sink)

        await hub.start()
        assert hub._qaudio is not None
        info = hub._get_device_info()
        assert info is not None
        assert info["name"] == "mock-mic"
        await asyncio.sleep(0.05)
        await hub.stop()

        assert sink.call_count >= 1
        mock_pyaudio.PyAudio.return_value.get_default_input_device_info.assert_called()
        mock_pyaudio.PyAudio.return_value.get_device_info_by_index.assert_called_with(0)


class TestMuteAndBackpressure:
    async def test_muted_hub_drops_audio_and_drains_sink_queues(self) -> None:
        hub = AudioHub(queue_size=2)
        sink = AsyncMock()
        hub.add_sink("a", sink)
        hub._running = True
        hub._on_chunk_received(b"first")
        hub.set_muted(True)
        hub._on_chunk_received(b"second")
        await asyncio.sleep(0)
        assert hub.muted is True
        assert sink.await_count == 0

    async def test_slow_sink_queue_is_bounded_and_drops_oldest(self) -> None:
        hub = AudioHub(queue_size=2)
        gate = asyncio.Event()

        async def slow_sink(_data: bytes) -> None:
            await gate.wait()

        hub.add_sink("slow", slow_sink)
        hub._loop = asyncio.get_running_loop()
        hub._running = True
        hub._start_sink_workers()
        for index in range(10):
            hub._on_chunk_received(bytes([index]))
        sink_state = hub._sinks["slow"]
        assert sink_state.queue.qsize() <= 2
        assert sink_state.dropped > 0
        gate.set()
        await hub.stop()
