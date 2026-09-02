import asyncio

import pytest

from sona.inference.scheduler import WorkloadKind
from sona.meeting.inner_os.workload import LocalLLMWorkloadGate


async def test_gate_allows_one_active_job_and_releases_after_cancel() -> None:
    gate = LocalLLMWorkloadGate()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def first() -> None:
        async with gate.slot("inner_os"):
            entered.set()
            await release.wait()

    first_task = asyncio.create_task(first())
    await entered.wait()
    assert await gate.try_acquire("summary") is False
    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task
    assert await gate.try_acquire("summary") is True
    gate.release()


async def test_background_admission_is_paused_during_recording() -> None:
    gate = LocalLLMWorkloadGate()
    gate.set_recording(True)
    assert await gate.try_acquire("summary", background=True) is False
    assert await gate.try_acquire("inner_os", background=False) is True
    gate.release()


async def test_interactive_waiter_precedes_queued_summary() -> None:
    gate = LocalLLMWorkloadGate()
    entered = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    async def blocker() -> None:
        async with gate.lease(workload=WorkloadKind.ASSISTANT_TURN):
            entered.set()
            await release.wait()

    async def acquire(name: str, workload: WorkloadKind) -> None:
        async with gate.lease(workload=workload):
            order.append(name)

    blocker_task = asyncio.create_task(blocker())
    await entered.wait()
    summary_task = asyncio.create_task(acquire("summary", WorkloadKind.SUMMARY))
    await asyncio.sleep(0)
    inner_os_task = asyncio.create_task(acquire("inner_os", WorkloadKind.INNER_OS))
    await asyncio.sleep(0)
    release.set()

    await asyncio.gather(blocker_task, summary_task, inner_os_task)
    assert order == ["inner_os", "summary"]


async def test_background_waiter_rechecks_recording_before_admission() -> None:
    gate = LocalLLMWorkloadGate()
    gate.pause_background()
    entered = asyncio.Event()

    async def summary() -> None:
        async with gate.lease(workload=WorkloadKind.SUMMARY):
            entered.set()

    task = asyncio.create_task(summary())
    await asyncio.sleep(0)
    assert entered.is_set() is False
    gate.resume_background()
    await asyncio.wait_for(entered.wait(), timeout=1)
    await task


async def test_waiting_workload_can_be_cancelled_without_leaking_slot() -> None:
    gate = LocalLLMWorkloadGate()
    entered = asyncio.Event()
    release = asyncio.Event()
    cancel = asyncio.Event()

    async def blocker() -> None:
        async with gate.lease(workload=WorkloadKind.ASSISTANT_TURN):
            entered.set()
            await release.wait()

    async def waiting() -> None:
        async with gate.lease(workload=WorkloadKind.INNER_OS, cancel_event=cancel):
            raise AssertionError("cancelled waiter must not enter")

    blocker_task = asyncio.create_task(blocker())
    await entered.wait()
    waiting_task = asyncio.create_task(waiting())
    await asyncio.sleep(0)
    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await waiting_task
    release.set()
    await blocker_task
    assert await gate.try_acquire("summary") is True
    gate.release()


async def test_closing_gate_releases_all_waiters() -> None:
    gate = LocalLLMWorkloadGate()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocker() -> None:
        async with gate.lease(workload=WorkloadKind.ASSISTANT_TURN):
            entered.set()
            await release.wait()

    blocker_task = asyncio.create_task(blocker())
    await entered.wait()
    waiter = asyncio.create_task(gate.acquire(WorkloadKind.SUMMARY))
    await asyncio.sleep(0)
    await gate.close()
    with pytest.raises(RuntimeError, match="closed"):
        await waiter
    release.set()
    await blocker_task
