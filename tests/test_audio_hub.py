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
        received_a = asyncio.Event()
        received_b = asyncio.Event()
        sink_a = AsyncMock(side_effect=lambda _data: received_a.set())
        sink_b = AsyncMock(side_effect=lambda _data: received_b.set())
        hub.add_sink("a", sink_a)
        hub.add_sink("b", sink_b)

        await hub.start()
        assert hub._qaudio is not None
        async with asyncio.timeout(1.0):
            await asyncio.gather(received_a.wait(), received_b.wait())
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
        received = asyncio.Event()
        bad_sink = AsyncMock(side_effect=RuntimeError("boom"))
        good_sink = AsyncMock(side_effect=lambda _data: received.set())
        hub.add_sink("bad", bad_sink)
        hub.add_sink("good", good_sink)
        await hub.start()
        async with asyncio.timeout(1.0):
            await received.wait()
        await hub.stop()
        assert good_sink.call_count >= 1


class TestLifecycle:
    async def test_running_reflects_start_and_stop(
        self, mock_pyaudio: MagicMock
    ) -> None:
        hub = AudioHub(throttle_secs=0.005)
        assert hub.running is False

        await hub.start()
        assert hub.running is True

        await hub.stop()
        assert hub.running is False

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
        received = asyncio.Event()
        sink = AsyncMock(side_effect=lambda _data: received.set())
        hub.add_sink("a", sink)

        await hub.start()
        assert hub._qaudio is not None
        info = hub._get_device_info()
        assert info is not None
        assert info["name"] == "mock-mic"
        async with asyncio.timeout(1.0):
            await received.wait()
        await hub.stop()

        assert sink.call_count >= 1
        mock_pyaudio.PyAudio.return_value.get_default_input_device_info.assert_called()
        mock_pyaudio.PyAudio.return_value.get_device_info_by_index.assert_called_with(0)


class TestNamedDevice:
    @staticmethod
    def _configure_devices(mock_pyaudio: MagicMock, devices: list[dict[str, object]]) -> None:
        instance = mock_pyaudio.PyAudio.return_value
        instance.get_device_count.return_value = len(devices)
        instance.get_device_info_by_index.side_effect = devices.__getitem__

    async def test_unique_name_fragment_selects_input_device(
        self, mock_pyaudio: MagicMock
    ) -> None:
        self._configure_devices(
            mock_pyaudio,
            [
                {"name": "OpenFit Pro by Shokz", "maxInputChannels": 1},
                {"name": "MacBook Pro麦克风", "maxInputChannels": 1},
            ],
        )
        hub = AudioHub(device_name="macbook pro", throttle_secs=0.005)
        hub.add_sink("a", AsyncMock())

        await hub.start()
        await hub.stop()

        kwargs = mock_pyaudio.PyAudio.return_value.open.call_args.kwargs
        assert kwargs["input_device_index"] == 1

    async def test_exact_name_wins_over_other_fragment_matches(
        self, mock_pyaudio: MagicMock
    ) -> None:
        self._configure_devices(
            mock_pyaudio,
            [
                {"name": "MacBook Pro USB Mic", "maxInputChannels": 1},
                {"name": "MacBook Pro", "maxInputChannels": 1},
            ],
        )
        hub = AudioHub(device_name="macbook pro", throttle_secs=0.005)
        hub.add_sink("a", AsyncMock())

        await hub.start()
        await hub.stop()

        kwargs = mock_pyaudio.PyAudio.return_value.open.call_args.kwargs
        assert kwargs["input_device_index"] == 1

    async def test_ambiguous_name_fails_without_opening_stream(
        self, mock_pyaudio: MagicMock
    ) -> None:
        self._configure_devices(
            mock_pyaudio,
            [
                {"name": "MacBook Pro Front", "maxInputChannels": 1},
                {"name": "MacBook Pro Rear", "maxInputChannels": 1},
            ],
        )
        hub = AudioHub(device_name="MacBook Pro")

        with pytest.raises(RuntimeError, match="匹配到多个输入设备"):
            await hub.start()

        mock_pyaudio.PyAudio.return_value.open.assert_not_called()

    async def test_missing_name_fails_without_default_fallback(
        self, mock_pyaudio: MagicMock
    ) -> None:
        self._configure_devices(
            mock_pyaudio,
            [
                {"name": "OpenFit Pro by Shokz", "maxInputChannels": 1},
                {"name": "HDMI Output", "maxInputChannels": 0},
            ],
        )
        hub = AudioHub(device_name="MacBook Pro")

        with pytest.raises(RuntimeError, match="未找到输入设备"):
            await hub.start()

        mock_pyaudio.PyAudio.return_value.open.assert_not_called()

    def test_name_and_index_cannot_be_configured_together(self) -> None:
        with pytest.raises(ValueError, match="不能同时配置"):
            AudioHub(device_index=3, device_name="MacBook Pro")


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

    def test_sink_diagnostics_count_drops_without_exposing_queue_or_audio(self) -> None:
        hub = AudioHub(queue_size=1)
        hub.add_sink("slow", AsyncMock())
        hub._running = True

        hub._on_chunk_received(b"old")
        hub._on_chunk_received(b"latest")

        diagnostics = hub.sink_diagnostics()
        assert diagnostics["slow"].queued_chunks == 1
        assert diagnostics["slow"].dropped_chunks == 1
        assert not hasattr(diagnostics["slow"], "queue")
        assert not hasattr(diagnostics["slow"], "audio")
        assert hub.sink_diagnostics() == diagnostics

        queued = hub._sinks["slow"].queue.get_nowait()
        hub._sinks["slow"].queue.task_done()
        assert queued == b"latest"
        assert hub.sink_diagnostics()["slow"].dropped_chunks == 1
