"""Priority admission for workloads sharing one local LM Studio instance."""

from __future__ import annotations

import asyncio
import heapq
import itertools
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class WorkloadKind(IntEnum):
    """Lower values are admitted first; active work is never preempted."""

    ASSISTANT_TURN = 0
    INNER_OS = 10
    MAINTENANCE = 50
    SUMMARY = 100


class SchedulerClosedError(RuntimeError):
    """Raised when work is submitted after scheduler shutdown."""


@dataclass(order=True, slots=True)
class _Waiter:
    priority: int
    sequence: int
    future: asyncio.Future[None] = field(compare=False)
    workload: WorkloadKind = field(compare=False)
    cancel_event: asyncio.Event | None = field(compare=False, default=None)
    admitted: bool = field(compare=False, default=False)


class LocalInferenceScheduler:
    """Single-slot, priority-ordered, non-preemptive local inference scheduler."""

    def __init__(self) -> None:
        self._waiters: list[_Waiter] = []
        self._sequence = itertools.count()
        self._active = False
        self._active_workload: WorkloadKind | None = None
        self._background_paused = False
        self._closed = False

    def pause_background(self) -> None:
        self._background_paused = True

    def resume_background(self) -> None:
        self._background_paused = False
        self._admit_next()

    def set_recording(self, recording: bool) -> None:
        if recording:
            self.pause_background()
        else:
            self.resume_background()

    @property
    def background_paused(self) -> bool:
        return self._background_paused

    @property
    def active_workload(self) -> WorkloadKind | None:
        return self._active_workload

    async def acquire(
        self,
        workload: WorkloadKind,
        *,
        priority: int | None = None,
        cancel_event: asyncio.Event | None = None,
        acquire_timeout_secs: float | None = None,
    ) -> None:
        if self._closed:
            raise SchedulerClosedError("local inference scheduler is closed")
        loop = asyncio.get_running_loop()
        waiter = _Waiter(
            priority=int(workload) if priority is None else priority,
            sequence=next(self._sequence),
            future=loop.create_future(),
            workload=workload,
            cancel_event=cancel_event,
        )
        heapq.heappush(self._waiters, waiter)
        self._admit_next()
        cancel_task: asyncio.Task[bool] | None = None
        try:
            if cancel_event is None:
                await asyncio.wait_for(
                    asyncio.shield(waiter.future), timeout=acquire_timeout_secs
                )
                return
            cancel_task = asyncio.create_task(cancel_event.wait())
            waitables: set[asyncio.Future[Any]] = {waiter.future, cancel_task}
            done, _ = await asyncio.wait(
                waitables,
                timeout=acquire_timeout_secs,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if waiter.future in done:
                await waiter.future
                return
            if cancel_task in done and cancel_event.is_set():
                raise asyncio.CancelledError
            raise TimeoutError
        except BaseException:
            if not waiter.admitted:
                self._remove_waiter(waiter)
                self._admit_next()
            raise
        finally:
            if cancel_task is not None:
                cancel_task.cancel()

    @asynccontextmanager
    async def lease(
        self,
        *,
        workload: WorkloadKind,
        priority: int | None = None,
        cancel_event: asyncio.Event | None = None,
        acquire_timeout_secs: float | None = None,
    ) -> AsyncIterator[None]:
        await self.acquire(
            workload,
            priority=priority,
            cancel_event=cancel_event,
            acquire_timeout_secs=acquire_timeout_secs,
        )
        try:
            yield
        finally:
            self.release()

    def slot(
        self,
        owner: str,
        *,
        background: bool = False,
        acquire_timeout_secs: float | None = None,
    ) -> AbstractAsyncContextManager[None]:
        workload = WorkloadKind.SUMMARY if background else _workload_for_owner(owner)
        return self.lease(
            workload=workload,
            acquire_timeout_secs=acquire_timeout_secs,
        )

    async def try_acquire(self, owner: str, *, background: bool = False) -> bool:
        if self._closed or self._active or self._waiters:
            return False
        workload = WorkloadKind.SUMMARY if background else _workload_for_owner(owner)
        if self._is_paused(workload):
            return False
        self._active = True
        self._active_workload = workload
        return True

    def release(self) -> None:
        if not self._active:
            raise RuntimeError("local inference scheduler has no active lease")
        self._active = False
        self._active_workload = None
        self._admit_next()

    async def close(self) -> None:
        self._closed = True
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.future.done():
                waiter.future.set_exception(
                    SchedulerClosedError("local inference scheduler is closed")
                )

    def _admit_next(self) -> None:
        if self._active or self._closed:
            return
        eligible = [
            waiter
            for waiter in self._waiters
            if not waiter.future.done()
            and not (waiter.cancel_event is not None and waiter.cancel_event.is_set())
            and not self._is_paused(waiter.workload)
        ]
        if not eligible:
            return
        waiter = min(eligible)
        self._remove_waiter(waiter)
        self._active = True
        self._active_workload = waiter.workload
        waiter.admitted = True
        waiter.future.set_result(None)

    def _remove_waiter(self, waiter: _Waiter) -> None:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            return
        heapq.heapify(self._waiters)

    def _is_paused(self, workload: WorkloadKind) -> bool:
        return self._background_paused and workload == WorkloadKind.SUMMARY


def _workload_for_owner(owner: str) -> WorkloadKind:
    normalized = owner.strip().lower()
    if normalized in {"summary", "meeting_summary"}:
        return WorkloadKind.SUMMARY
    if normalized in {"assistant", "assistant_turn"}:
        return WorkloadKind.ASSISTANT_TURN
    if normalized in {"maintenance", "compaction", "title"}:
        return WorkloadKind.MAINTENANCE
    return WorkloadKind.INNER_OS
