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
from typing import Any

import pyaudio  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

# 音频参数：与 Pipecat 管道默认 sample_rate 一致
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit
CHUNK_SIZE = 512  # 每帧字节数（~16ms @ 16kHz/16bit）


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
        sample_rate: int = SAMPLE_RATE,
        chunk_size: int = CHUNK_SIZE,
        queue_size: int = 256,
        throttle_secs: float = 0.0,
    ) -> None:
        self._device_index = device_index
        self._sample_rate = sample_rate
        self._chunk_size = chunk_size
        self._queue_size = queue_size
        self._throttle_secs = throttle_secs
        self._sinks: dict[str, Callable[[bytes], Awaitable[None]]] = {}
        self._running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._qaudio: pyaudio.PyAudio | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()

    def add_sink(
        self,
        name: str,
        callback: Callable[[bytes], Awaitable[None]],
    ) -> None:
        """注册一个消费端。callback 接收原始音频 bytes。"""
        if name in self._sinks:
            raise ValueError(f"Sink {name!r} 已存在")
        self._sinks[name] = callback
        logger.info("AudioHub: 注册 sink %r (共 %d 个)", name, len(self._sinks))

    def remove_sink(self, name: str) -> None:
        """移除消费端。"""
        self._sinks.pop(name, None)
        logger.info("AudioHub: 移除 sink %r (剩余 %d 个)", name, len(self._sinks))

    async def start(self) -> None:
        """启动后台采集线程。"""
        if self._running:
            return
        self._loop = asyncio.get_running_loop()
        self._qaudio = pyaudio.PyAudio()
        device_info = self._get_device_info()
        logger.info(
            "AudioHub: 打开设备 %s (%d ch, %d Hz)",
            device_info or "默认",
            device_info.get("max_input_channels", "?") if device_info else "?",
            self._sample_rate,
        )
        self._running = True
        self._thread = threading.Thread(
            target=self._worker_capture_loop,
            name="audio-hub-capture",
            daemon=True,
        )
        self._thread.start()

    async def stop(self) -> None:
        """停止采集并释放资源。"""
        if not self._running:
            return
        self._running = False
        thread = self._thread
        if thread is not None:
            await asyncio.to_thread(thread.join, timeout=1.0)
            self._thread = None
        if self._qaudio is not None:
            self._qaudio.terminate()
            self._qaudio = None
        self._loop = None
        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()
        logger.info("AudioHub: 已停止")

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
                input_device_index=self._device_index,
                frames_per_buffer=self._chunk_size,
                start=True,
            )
        except Exception:
            logger.exception("AudioHub: 打开音频流失败")
            return

        chunk_ms = self._chunk_size / self._sample_rate * 1000 * 8
        logger.info("AudioHub: 采集流已打开，chunk=%d bytes (~%.0fms)", self._chunk_size, chunk_ms)
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
        if not self._running or not self._sinks:
            return
        for name, sink in list(self._sinks.items()):
            task = asyncio.create_task(self._dispatch(name, sink, data))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _dispatch(
        self,
        name: str,
        callback: Callable[[bytes], Awaitable[None]],
        data: bytes,
    ) -> None:
        """分发音频数据到单个 sink。"""
        try:
            await callback(data)
        except Exception:
            logger.exception("AudioHub: sink %r 处理音频失败", name)

    def _get_device_info(self) -> dict[str, Any] | None:
        """获取设备信息（用于日志）。"""
        pa = self._qaudio
        if pa is None:
            return None
        index = self._device_index
        if index is None:
            default = pa.get_default_input_device_info()
            index = default["index"]
        try:
            return {
                "name": pa.get_device_info_by_index(index).get("name"),
                "max_input_channels": pa.get_device_info_by_index(index).get("maxInputChannels"),
            }
        except (OSError, IndexError):
            return None
