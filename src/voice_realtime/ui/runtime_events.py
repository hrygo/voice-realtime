"""面向控制连接的 latest-only 权威运行状态广播。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

from voice_realtime.ui.protocol import RuntimeStateSnapshot


class _CloseSentinel:
    """唤醒已阻塞状态订阅者的私有队列标记。"""


_CLOSE_SENTINEL = _CloseSentinel()
type _QueueItem = RuntimeStateSnapshot | _CloseSentinel


@dataclass(eq=False, slots=True)
class RuntimeStateClient:
    """一个控制客户端的独立、容量为一的状态队列。"""

    _queue: asyncio.Queue[_QueueItem]
    _closed: bool = False

    async def receive(self) -> RuntimeStateSnapshot:
        """等待并返回下一个可用的权威状态。"""
        if self._closed:
            raise RuntimeError("runtime state client is closed")
        item = await self._queue.get()
        if isinstance(item, _CloseSentinel):
            raise RuntimeError("runtime state client is closed")
        return item

    def latest_nowait(self) -> RuntimeStateSnapshot:
        """同步取出队列当前保留的最新状态。"""
        if self._closed:
            raise RuntimeError("runtime state client is closed")
        latest = self._queue.get_nowait()
        while True:
            try:
                latest = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        if isinstance(latest, _CloseSentinel):
            raise RuntimeError("runtime state client is closed")
        return latest

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._queue.put_nowait(_CLOSE_SENTINEL)


class RuntimeStateBroadcaster:
    """同步向每个控制客户端发布最新完整运行状态。"""

    def __init__(self, snapshot_provider: Callable[[], RuntimeStateSnapshot]) -> None:
        self._snapshot_provider = snapshot_provider
        self._clients: set[RuntimeStateClient] = set()

    def add_client(self) -> RuntimeStateClient:
        """注册客户端并立即入队当前完整状态。"""
        queue: asyncio.Queue[_QueueItem] = asyncio.Queue(maxsize=1)
        client = RuntimeStateClient(queue)
        self._clients.add(client)
        self._replace_latest(client, self._snapshot_provider())
        return client

    def remove_client(self, client: RuntimeStateClient) -> None:
        """幂等移除客户端并清空其待发状态。"""
        self._clients.discard(client)
        client._close()

    def publish(self, state: RuntimeStateSnapshot) -> None:
        """无等待地替换所有活动客户端的待发状态。"""
        for client in tuple(self._clients):
            self._replace_latest(client, state)

    @staticmethod
    def _replace_latest(
        client: RuntimeStateClient, state: RuntimeStateSnapshot
    ) -> None:
        if client._closed:
            return
        with suppress(asyncio.QueueEmpty):
            client._queue.get_nowait()
        client._queue.put_nowait(state)
