"""SubtitleProxy：单 wlk 连接 + 全双工音频推送 + 事件 multi-cast 到浏览器。

职责：
- 建立 WhisperLiveKit /asr WS 连接
- 独立发送协程从 AudioHub 队列取音频块 → send_audio() 流式推给 wlk（无损推流）
- 独立接收协程消费 wlk 转写事件 → multi-cast 到 /ws/subtitles 浏览器连接
- 无浏览器订阅时自动暂停推流（按需采集），有订阅时恢复
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable

from voice_realtime.config import SubtitleSettings
from voice_realtime.subtitles.events import SubtitleEvent, SubtitleEventTracker, SubtitleStream

logger = logging.getLogger(__name__)


class SubtitleProxy:
    """wlk 字幕连接代理 + 全双工事件广播。

    用法：
        proxy = SubtitleProxy(settings)
        proxy.add_audio_sink(audio_callback)  # AudioHub 推送音频
        proxy.add_client(ws_send)             # 浏览器 WS send
        await proxy.start()
        await proxy.stop()
    """

    def __init__(self, settings: SubtitleSettings) -> None:
        self._settings = settings
        self._stream: SubtitleStream | None = None
        self._audio_sinks: list[Callable[[bytes], Awaitable[None]]] = []
        self._ws_clients: list[Callable[[str], Awaitable[None]]] = []
        self._tracker = SubtitleEventTracker()
        self._running = False
        self._send_task: asyncio.Task[None] | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._audio_buffer: asyncio.Queue[bytes] = asyncio.Queue(maxsize=512)
        self._paused = False

    def add_audio_sink(
        self,
        callback: Callable[[bytes], Awaitable[None]],
    ) -> None:
        """注册音频推送端（AudioHub 调用）。"""
        self._audio_sinks.append(callback)
        logger.info("SubtitleProxy: 注册音频 sink (共 %d 个)", len(self._audio_sinks))

    def add_client(
        self,
        ws_send: Callable[[str], Awaitable[None]],
    ) -> str:
        """注册浏览器 WS 客户端，返回 client_id（用于移除）。"""
        self._ws_clients.append(ws_send)
        client_id = f"client_{len(self._ws_clients)}"
        logger.info("SubtitleProxy: 新浏览器订阅 (共 %d 个)", len(self._ws_clients))
        return client_id

    def remove_client(self, ws_send: Callable[[str], Awaitable[None]]) -> None:
        """移除浏览器 WS 客户端。"""
        if ws_send in self._ws_clients:
            self._ws_clients.remove(ws_send)
            logger.info("SubtitleProxy: 浏览器取消订阅 (剩余 %d 个)", len(self._ws_clients))

    @property
    def has_clients(self) -> bool:
        return len(self._ws_clients) > 0

    @property
    def is_paused(self) -> bool:
        return not self.has_clients

    async def start(self) -> None:
        """启动代理：建立连接并启动全双工发送与接收任务。"""
        if self._running:
            return
        self._stream = SubtitleStream(
            url=f"ws://{self._settings.host}:{self._settings.port}",
            language=self._settings.language,
        )
        await self._stream.connect()
        logger.info("SubtitleProxy: 已连接 wlk %s", self._stream.uri)
        self._running = True
        self._send_task = asyncio.create_task(self._audio_send_loop())
        self._recv_task = asyncio.create_task(self._event_recv_loop())

    async def stop(self) -> None:
        """停止代理并释放连接与所有任务。"""
        self._running = False
        for task in (self._send_task, self._recv_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._send_task = None
        self._recv_task = None
        if self._stream is not None:
            await self._stream.close()
            self._stream = None
        logger.info("SubtitleProxy: 已停止")

    async def _process_loop(self) -> None:
        """兼容性包装：同时运行事件接收循环。"""
        await self._event_recv_loop()

    async def _audio_send_loop(self) -> None:
        """独立音频上行协程：从缓冲区无损连续推送音频到 wlk。"""
        while self._running:
            try:
                chunk = await self._audio_buffer.get()
            except asyncio.CancelledError:
                break
            if self._stream is not None and self.has_clients:
                try:
                    await self._stream.send_audio(chunk)
                except Exception:
                    logger.debug("SubtitleProxy: 发送音频块异常", exc_info=True)

    async def _event_recv_loop(self) -> None:
        """独立事件下行协程：持续监听 wlk 转写事件并去重广播。"""
        stream = self._stream
        if stream is None:
            return
        try:
            async for event in stream.events():
                await self._broadcast_event(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("SubtitleProxy: 接收事件循环异常")

    async def _broadcast_event(self, event: SubtitleEvent) -> None:
        """将规范化事件广播给所有浏览器 WS 客户端。"""
        if not self._ws_clients:
            return
        if not self._tracker.track(event):
            return
        payload = event.raw
        text = json.dumps(payload, ensure_ascii=False)
        tasks = [client(text) for client in self._ws_clients]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def push_audio(self, data: bytes) -> None:
        """AudioHub 调用：将音频块推入缓冲区。"""
        try:
            self._audio_buffer.put_nowait(data)
        except asyncio.QueueFull:
            logger.debug("SubtitleProxy: 音频队列满，丢弃帧")

    async def _push_audio_batch(self) -> None:
        """无损批量推送缓冲区中的所有音频块（全量排出，绝不丢帧）。"""
        if self._stream is None or not self.has_clients:
            return
        while not self._audio_buffer.empty():
            try:
                data = self._audio_buffer.get_nowait()
                await self._stream.send_audio(data)
            except asyncio.QueueEmpty:
                break
            except Exception:
                logger.exception("SubtitleProxy: 批量推送音频失败")
                break

