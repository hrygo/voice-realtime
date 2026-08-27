from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class LocalLLMWorkloadGate:
    """进程级单槽模型准入仲裁；不强杀已进入模型的任务。"""

    def __init__(self) -> None:
        self._slot = asyncio.Semaphore(1)
        self._recording = False

    def set_recording(self, recording: bool) -> None:
        self._recording = recording

    async def try_acquire(self, owner: str, *, background: bool = False) -> bool:
        del owner
        if background and self._recording:
            return False
        if self._slot.locked():
            return False
        await self._slot.acquire()
        return True

    @asynccontextmanager
    async def slot(self, owner: str, *, background: bool = False) -> AsyncIterator[None]:
        if background and self._recording:
            raise RuntimeError("background model admission paused during recording")
        await self._slot.acquire()
        try:
            yield
        finally:
            self._slot.release()

    def release(self) -> None:
        self._slot.release()
