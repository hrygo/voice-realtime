import asyncio

import pytest

from voice_realtime.meeting.inner_os.workload import LocalLLMWorkloadGate


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
