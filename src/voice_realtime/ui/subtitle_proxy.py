"""SubtitleProxy：单 wlk 连接 + 音频推送 + 事件 multi-cast 到浏览器。

职责：
- 建立 WhisperLiveKit /asr WS 连接
- 从 AudioHub 接收音频块 → send_audio() 推给 wlk
- 接收 wlk 转写事件 → multi-cast 到 /ws/subtitles 浏览器连接
- 无浏览器订阅时暂停推送音频（按需采集）
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
    """wlk 字幕连接代理 + 事件广播。

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
        self._task: asyncio.Task[None] | None = None
        self._audio_buffer: asyncio.Queue[bytes] = asyncio.Queue(maxsize=512)
        self._paused = False  # 无浏览器订阅时暂停推音频

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
        return self._paused

    async def start(self) -> None:
        """启动代理循环。"""
        if self._running:
            return
        self._stream = SubtitleStream(
            url=f"ws://{self._settings.host}:{self._settings.port}",
            language=self._settings.language,
        )
        await self._stream.connect()
        logger.info("SubtitleProxy: 已连接 wlk %s", self._stream.uri)
        self._task = asyncio.create_task(self._process_loop())
        self._running = True

    async def stop(self) -> None:
        """停止代理并释放连接。"""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._stream is not None:
            await self._stream.close()
            self._stream = None
        logger.info("SubtitleProxy: 已停止")

    async def _process_loop(self) -> None:
        """主循环：推音频 + 收事件 + 广播。"""
        stream = self._stream
        if stream is None:
            return
        try:
            async for event in stream.events():
                # 广播事件给所有浏览器客户端
                await self._broadcast_event(event)
                # 按需推音频：有浏览器订阅时才推
                should_push = self.has_clients
                if should_push != self._paused:
                    self._paused = not should_push
                    action = "暂停" if self._paused else "恢复"
                    logger.info("SubtitleProxy: %s音频推送（无浏览器订阅）", action)
                # 从缓冲区取音频块推送（音频是独立队列，事件循环控制推送时机）
                if should_push and not self._audio_buffer.empty():
                    await self._push_audio_batch()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("SubtitleProxy: 处理循环异常")

    async def _broadcast_event(self, event: SubtitleEvent) -> None:
        """将规范化事件广播给所有浏览器 WS 客户端。"""
        if not self._ws_clients:
            return
        # 去重：只有新事件才广播
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
        """批量推送缓冲区中的音频块。"""
        batch = []
        while not self._audio_buffer.empty():
            try:
                batch.append(self._audio_buffer.get_nowait())
            except asyncio.QueueEmpty:
                break
        if not batch or self._stream is None:
            return
        # 逐个推送（避免大帧阻塞）
        for data in batch[:1]:  # 每次只推一帧，保持实时性
            try:
                await self._stream.send_audio(data)
            except Exception:
                logger.exception("SubtitleProxy: 推送音频失败")
                break
