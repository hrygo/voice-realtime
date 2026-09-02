"""浏览器字幕客户端 fan-out：callback hub + 每客户端有界队列。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

logger = logging.getLogger(__name__)

ClientSender = Callable[[str], Awaitable[None]]


@dataclass
class _ClientChannel:
    queue: asyncio.Queue[str]
    task: asyncio.Task[None]


class SubtitleClientHub:
    """向每个注册 sender 广播字幕快照；慢客户端只影响自己的通道。

    本类不感知 FastAPI WebSocket、SpeechRail client、会议仓库或文件系统；
    浏览器 disconnect 只移除自己的 sender，绝不关闭任何 SpeechRail capture。
    """

    _CLIENT_QUEUE_SIZE = 8

    def __init__(
        self,
        *,
        on_channel_closed: Callable[[], None] | None = None,
    ) -> None:
        self._clients: dict[ClientSender, _ClientChannel] = {}
        self._client_sequence = 0
        self._on_channel_closed = on_channel_closed

    def add(self, sender: ClientSender, *, snapshot: Mapping[str, object] | None) -> str:
        """注册浏览器发送端；每个客户端拥有独立有界队列并立即收到最新快照。"""
        if sender not in self._clients:
            queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self._CLIENT_QUEUE_SIZE)
            if snapshot is not None:
                queue.put_nowait(json.dumps(snapshot, ensure_ascii=False))
            task = asyncio.create_task(self._send_loop(sender, queue))
            task.add_done_callback(self._consume_task_result)
            self._clients[sender] = _ClientChannel(queue=queue, task=task)
        self._client_sequence += 1
        logger.info("SubtitleClientHub: 新浏览器订阅 (共 %d 个)", len(self._clients))
        return f"client_{self._client_sequence}"

    def remove(self, sender: ClientSender) -> None:
        """移除浏览器发送端；不阻塞、不等待其当前回调。"""
        channel = self._clients.pop(sender, None)
        if channel is not None:
            channel.task.cancel()
            logger.info("SubtitleClientHub: 浏览器取消订阅 (剩余 %d 个)", len(self._clients))

    @property
    def has_clients(self) -> bool:
        return bool(self._clients)

    def __len__(self) -> int:
        return len(self._clients)

    async def publish(self, payload: Mapping[str, object]) -> None:
        """广播完整快照；慢客户端只被丢弃最旧消息，publish 不等它。"""
        if not self._clients:
            return
        text = json.dumps(payload, ensure_ascii=False)
        for channel in tuple(self._clients.values()):
            if channel.queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    channel.queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                channel.queue.put_nowait(text)
        await asyncio.sleep(0)

    async def close(self) -> None:
        """取消全部客户端 worker；随后 hub 为空且可安全丢弃。"""
        channels = list(self._clients.values())
        self._clients.clear()
        for channel in channels:
            channel.task.cancel()
        if channels:
            await asyncio.gather(*(channel.task for channel in channels), return_exceptions=True)

    async def _send_loop(self, callback: ClientSender, queue: asyncio.Queue[str]) -> None:
        try:
            while True:
                await callback(await queue.get())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("SubtitleClientHub: 浏览器客户端已断开", exc_info=True)
        finally:
            current = self._clients.get(callback)
            if current is not None and current.task is asyncio.current_task():
                self._clients.pop(callback, None)
            if self._on_channel_closed is not None:
                self._on_channel_closed()

    @staticmethod
    def _consume_task_result(task: asyncio.Task[None]) -> None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()
