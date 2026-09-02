"""AudioInjector：把外部队列中的原始音频块注入 Pipecat 管道。

作为管道首节点替代 `LocalAudioTransport.input()`：
从 AudioHub 扇出的 asyncio.Queue 取 16k/16bit/mono 音频块，
构造 `InputAudioRawFrame` 推入下游（EchoSuppressionProcessor → STT → …）。

既有管道调优（VAD 参数、回声抑制、ttfs）不感知来源差异，
因为 Injector 产出的帧类型与 LocalAudioInputTransport 回调完全一致。
"""

from __future__ import annotations

import asyncio
import logging

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    StartFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger(__name__)


class AudioInjector(FrameProcessor):
    """从音频队列泵送 InputAudioRawFrame 到管道下游。

    生命周期与 Pipecat 集成：收到 StartFrame 时启动泵送任务，
    收到 EndFrame/CancelFrame 时停止。系统帧全部透传给下游，
    保证管道控制帧（Start/End/Interruption/Cancel）正常流转。
    """

    def __init__(
        self,
        audio_queue: asyncio.Queue[bytes],
        sample_rate: int = 16000,
        num_channels: int = 1,
        enable_direct_mode: bool = False,
    ) -> None:
        super().__init__(
            name="audio-injector",
            enable_direct_mode=enable_direct_mode,
        )
        self._queue = audio_queue
        self._sample_rate = sample_rate
        self._num_channels = num_channels
        self._pump_task: asyncio.Task[None] | None = None

    @property
    def queue(self) -> asyncio.Queue[bytes]:
        """供 AudioHub sink 推送音频块的队列。"""
        return self._queue

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self._start_pump()
        elif isinstance(frame, (EndFrame, CancelFrame)):
            self._stop_pump()
        await self.push_frame(frame, direction)

    def _start_pump(self) -> None:
        if self._pump_task is None:
            self._pump_task = asyncio.create_task(self._pump())
            logger.info("AudioInjector: 泵送任务已启动")

    def _stop_pump(self) -> None:
        if self._pump_task is not None:
            self._pump_task.cancel()
            self._pump_task = None
            logger.info("AudioInjector: 泵送任务已停止")

    def drain(self) -> int:
        """丢弃尚未注入的旧音频，返回清理的块数。"""
        count = 0
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                count += 1
            except asyncio.QueueEmpty:
                return count

    async def _pump(self) -> None:
        while True:
            try:
                audio = await self._queue.get()
            except asyncio.CancelledError:
                return
            frame = InputAudioRawFrame(
                audio=audio,
                sample_rate=self._sample_rate,
                num_channels=self._num_channels,
            )
            await self.push_frame(frame, FrameDirection.DOWNSTREAM)
