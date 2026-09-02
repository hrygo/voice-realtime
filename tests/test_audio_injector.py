"""AudioInjector 单测：Start/EndFrame 生命周期 + 音频块 → InputAudioRawFrame 泵送。

测试实例用 enable_direct_mode=True 构造：该模式下 FrameProcessor 不创建内部队列
task，process_frame 可直接 await；生产管道保持默认 False（走框架队列机制）。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    StartFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from sona.audio.audio_injector import AudioInjector


def _make_injector(queue: asyncio.Queue[bytes] | None = None, **kwargs) -> AudioInjector:
    return AudioInjector(queue or asyncio.Queue(), enable_direct_mode=True, **kwargs)


class _FrameCollector:
    """收集 push_frame 收到的帧；收满预期数量的音频帧后置位事件（替代轮询）。"""

    def __init__(self, expect_audio: int) -> None:
        self.audio_frames: list[InputAudioRawFrame] = []
        self._expect_audio = expect_audio
        self.audio_done = asyncio.Event()

    def __call__(self, frame: Frame, direction: FrameDirection) -> None:
        if isinstance(frame, InputAudioRawFrame):
            self.audio_frames.append(frame)
            if len(self.audio_frames) >= self._expect_audio:
                self.audio_done.set()

    async def wait(self, limit: float = 1.0) -> None:
        await asyncio.wait_for(self.audio_done.wait(), timeout=limit)


class TestLifecycle:
    async def test_startframe_starts_pump(self) -> None:
        injector = _make_injector()
        assert injector._pump_task is None  # type: ignore[attr-defined]
        await injector.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
        assert injector._pump_task is not None  # type: ignore[attr-defined]
        injector._stop_pump()

    async def test_endframe_stops_pump(self) -> None:
        injector = _make_injector()
        await injector.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
        assert injector._pump_task is not None  # type: ignore[attr-defined]
        await injector.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)
        assert injector._pump_task is None  # type: ignore[attr-defined]

    async def test_cancelframe_stops_pump(self) -> None:
        injector = _make_injector()
        await injector.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
        await injector.process_frame(CancelFrame(), FrameDirection.DOWNSTREAM)
        assert injector._pump_task is None  # type: ignore[attr-defined]


class TestPump:
    def test_drain_discards_stale_audio(self) -> None:
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        queue.put_nowait(b"old-1")
        queue.put_nowait(b"old-2")
        injector = _make_injector(queue)
        assert injector.drain() == 2
        assert queue.empty()

    async def test_pump_pushes_input_audio_frame(self) -> None:
        """队列中的音频块被构造成 InputAudioRawFrame 推入下游。"""
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        injector = _make_injector(queue, sample_rate=16000, num_channels=1)
        collector = _FrameCollector(expect_audio=1)
        mock_push = AsyncMock(side_effect=collector)
        with patch.object(injector, "push_frame", mock_push):
            await injector.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
            await queue.put(b"\x00" * 1024)
            await collector.wait()

        assert len(collector.audio_frames) == 1
        frame = collector.audio_frames[0]
        assert frame.audio == b"\x00" * 1024
        assert frame.sample_rate == 16000
        assert frame.num_channels == 1

    async def test_pump_continues_for_multiple_chunks(self) -> None:
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        injector = _make_injector(queue)
        collector = _FrameCollector(expect_audio=2)
        mock_push = AsyncMock(side_effect=collector)
        with patch.object(injector, "push_frame", mock_push):
            await injector.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
            await queue.put(b"a" * 512)
            await queue.put(b"b" * 512)
            await collector.wait()

        assert [f.audio for f in collector.audio_frames] == [b"a" * 512, b"b" * 512]

    async def test_system_frames_passthrough(self) -> None:
        """StartFrame/EndFrame 透传给下游。"""
        injector = _make_injector()
        mock_push = AsyncMock()
        with patch.object(injector, "push_frame", mock_push):
            await injector.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
            await injector.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)

        assert mock_push.call_count == 2
        assert isinstance(mock_push.call_args_list[0].args[0], StartFrame)
        assert isinstance(mock_push.call_args_list[1].args[0], EndFrame)
