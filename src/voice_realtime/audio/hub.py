"""AudioHub：系统麦克风常驻采集 + asyncio 队列扇出。

单源采集（16k/16bit/mono），通过 asyncio.Queue 扇出到多个 sink。
- Pipecat 管道：通过 AudioInjector 节点消费
- wlk 字幕：通过 SubtitleProxy 消费
- 未来扩展：录音 wav、音量计、唤醒检测

macOS 支持同一输入设备多路打开（CoreAudio 共享），AudioHub 与 Pipecat
LocalAudioTransport 可同时运行而不争抢麦克风。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pyaudio  # type: ignore[import-untyped]

from voice_realtime.audio.devices import (
    AudioInputDeviceError,
    resolve_input_device_index_with,
)

logger = logging.getLogger(__name__)

# 音频参数：与 Pipecat 管道默认 sample_rate 一致
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit
CHUNK_SIZE = 512  # 每次读取的采样帧数（~32ms @ 16kHz；mono int16 为 1024 bytes）


@dataclass(slots=True)
class _SinkState:
    callback: Callable[[bytes], Awaitable[None]]
    queue: asyncio.Queue[bytes]
    task: asyncio.Task[None] | None = None
    dropped: int = 0


class AudioHub:
    """系统麦克风采集 + 扇出服务（专用后台线程采集，0 阻塞事件循环）。

    用法：
        hub = AudioHub(device_index=None)
        hub.add_sink("pipecat", callback)
        hub.add_sink("subtitle", callback)
        await hub.start()
        # ... 运行 ...
        await hub.stop()
    """

    def __init__(
        self,
        device_index: int | None = None,
        device_name: str | None = None,
        sample_rate: int = SAMPLE_RATE,
        chunk_size: int = CHUNK_SIZE,
        queue_size: int = 8,
        throttle_secs: float = 0.0,
    ) -> None:
        normalized_name = device_name.strip() if device_name is not None else None
        normalized_name = normalized_name or None
        if device_index is not None and normalized_name is not None:
            raise ValueError("麦克风设备索引与名称不能同时配置")
        self._device_index = device_index
        self._device_name = normalized_name
        self._resolved_device_index: int | None = None
        self._sample_rate = sample_rate
        self._chunk_size = chunk_size
        self._queue_size = max(1, queue_size)
        self._throttle_secs = throttle_secs
        self._sinks: dict[str, _SinkState] = {}
        self._running = False
        self._muted = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._qaudio: pyaudio.PyAudio | None = None
        self._open_future: asyncio.Future[None] | None = None

    @property
    def muted(self) -> bool:
        """麦克风是否处于真实静音状态。"""
        return self._muted

    def set_muted(self, muted: bool) -> None:
        """设置静音；进入静音时丢弃各 sink 尚未消费的音频。"""
        self._muted = muted
        if muted:
            for sink in self._sinks.values():
                self._drain_queue(sink.queue)
        logger.info("AudioHub: 麦克风%s", "已静音" if muted else "已恢复")

    def add_sink(
        self,
        name: str,
        callback: Callable[[bytes], Awaitable[None]],
    ) -> None:
        """注册一个消费端。callback 接收原始音频 bytes。"""
        if name in self._sinks:
            raise ValueError(f"Sink {name!r} 已存在")
        state = _SinkState(callback=callback, queue=asyncio.Queue(maxsize=self._queue_size))
        self._sinks[name] = state
        if self._running and self._loop is not None:
            state.task = asyncio.create_task(self._sink_worker(name, state))
        logger.info("AudioHub: 注册 sink %r (共 %d 个)", name, len(self._sinks))

    async def remove_sink(self, name: str) -> None:
        """移除消费端并等待其唯一 worker 退出。"""
        state = self._sinks.pop(name, None)
        if state is not None and state.task is not None:
            state.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state.task
            self._drain_queue(state.queue)
        logger.info("AudioHub: 移除 sink %r (剩余 %d 个)", name, len(self._sinks))

    async def start(self) -> None:
        """启动后台采集线程。"""
        if self._running:
            return
        self._loop = asyncio.get_running_loop()
        self._qaudio = pyaudio.PyAudio()
        try:
            self._resolved_device_index = self._resolve_device_index()
            device_info = self._get_device_info(self._resolved_device_index)
            logger.info(
                "AudioHub: 打开设备 %s (%s ch, %d Hz)",
                device_info or "默认",
                device_info.get("max_input_channels", "?") if device_info else "?",
                self._sample_rate,
            )
            self._running = True
            self._start_sink_workers()
            self._open_future = self._loop.create_future()
            self._thread = threading.Thread(
                target=self._worker_capture_loop,
                name="audio-hub-capture",
                daemon=True,
            )
            self._thread.start()
            await self._open_future
        except BaseException:
            await self._cleanup_after_failed_start()
            raise

    async def stop(self) -> None:
        """停止采集并释放资源。"""
        if not self._running and self._qaudio is None and self._thread is None:
            return
        self._running = False
        thread = self._thread
        if thread is not None:
            await asyncio.to_thread(thread.join, timeout=1.0)
            self._thread = None
        await self._stop_sink_workers()
        self._terminate_pyaudio()
        self._loop = None
        self._open_future = None
        self._resolved_device_index = None
        logger.info("AudioHub: 已停止")

    async def _cleanup_after_failed_start(self) -> None:
        self._running = False
        thread = self._thread
        if thread is not None:
            await asyncio.to_thread(thread.join, timeout=1.0)
            self._thread = None
        await self._stop_sink_workers()
        self._terminate_pyaudio()
        self._loop = None
        self._open_future = None
        self._resolved_device_index = None

    def _terminate_pyaudio(self) -> None:
        if self._qaudio is not None:
            self._qaudio.terminate()
            self._qaudio = None

    def _start_sink_workers(self) -> None:
        for name, sink in self._sinks.items():
            if sink.task is None or sink.task.done():
                sink.task = asyncio.create_task(self._sink_worker(name, sink))

    async def _stop_sink_workers(self) -> None:
        tasks = [sink.task for sink in self._sinks.values() if sink.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for sink in self._sinks.values():
            sink.task = None
            self._drain_queue(sink.queue)

    @staticmethod
    def _drain_queue(queue: asyncio.Queue[bytes]) -> None:
        while True:
            try:
                queue.get_nowait()
                queue.task_done()
            except asyncio.QueueEmpty:
                return

    def _report_stream_open(self, error: BaseException | None = None) -> None:
        future = self._open_future
        if future is None or future.done():
            return
        if error is None:
            future.set_result(None)
        else:
            future.set_exception(error)

    def _worker_capture_loop(self) -> None:
        """后台专用线程中的 PyAudio 阻塞采集循环。"""
        qaudio = self._qaudio
        if qaudio is None:
            return
        try:
            stream = qaudio.open(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=self._sample_rate,
                input=True,
                input_device_index=self._resolved_device_index,
                frames_per_buffer=self._chunk_size,
                start=True,
            )
        except Exception as exc:
            logger.exception("AudioHub: 打开音频流失败")
            loop = self._loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(self._report_stream_open, exc)
            return

        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._report_stream_open)
        chunk_ms = self._chunk_size / self._sample_rate * 1000
        logger.info(
            "AudioHub: 采集流已打开，chunk=%d frames/%d bytes (~%.0fms)",
            self._chunk_size,
            self._chunk_size * CHANNELS * SAMPLE_WIDTH,
            chunk_ms,
        )
        try:
            while self._running:
                if self._throttle_secs:
                    time.sleep(self._throttle_secs)
                if not self._sinks:
                    time.sleep(0.01)
                    continue
                try:
                    data = stream.read(self._chunk_size, exception_on_overflow=False)
                except OSError:
                    time.sleep(0.05)
                    continue

                if not data or not self._running:
                    continue

                loop = self._loop
                if loop is not None and loop.is_running():
                    loop.call_soon_threadsafe(self._on_chunk_received, data)
        finally:
            with contextlib.suppress(Exception):
                stream.stop_stream()
                stream.close()
            logger.info("AudioHub: 采集流已关闭")

    def _on_chunk_received(self, data: bytes) -> None:
        """主事件循环回调：并发扇出音频数据到所有 sink。"""
        if not self._running or self._muted or not self._sinks:
            return
        for sink in list(self._sinks.values()):
            if sink.queue.full():
                try:
                    sink.queue.get_nowait()
                    sink.queue.task_done()
                except asyncio.QueueEmpty:
                    pass
                sink.dropped += 1
            sink.queue.put_nowait(data)

    async def _sink_worker(self, name: str, sink: _SinkState) -> None:
        while True:
            data = await sink.queue.get()
            try:
                if not self._muted:
                    await sink.callback(data)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("AudioHub: sink %r 处理音频失败", name)
            finally:
                sink.queue.task_done()

    def _resolve_device_index(self) -> int:
        """按显式索引、显式名称、系统默认的优先级解析输入设备。"""
        pa = self._qaudio
        if pa is None:
            raise AudioInputDeviceError("PyAudio 尚未初始化")
        return resolve_input_device_index_with(
            pa,
            device_index=self._device_index,
            device_name=self._device_name,
        )

    def _get_device_info(self, index: int | None = None) -> dict[str, Any] | None:
        """获取设备信息（用于日志）。"""
        pa = self._qaudio
        if pa is None:
            return None
        if index is None:
            index = self._resolved_device_index
        if index is None:
            default = pa.get_default_input_device_info()
            index = int(default["index"])
        try:
            return {
                "name": pa.get_device_info_by_index(index).get("name"),
                "max_input_channels": pa.get_device_info_by_index(index).get("maxInputChannels"),
            }
        except (OSError, IndexError):
            return None
