"""InteractionSession 单一所有者与生命周期测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from voice_realtime.config import InteractionSettings
from voice_realtime.interaction.ownership import (
    InteractionOwnership,
    InteractionOwnershipError,
)
from voice_realtime.interaction.session import InteractionSession, InteractionSessionState
from voice_realtime.ui.protocol import DuplexMode


def test_second_interaction_owner_is_rejected(tmp_path: Path) -> None:
    lock_path = tmp_path / "interaction.lock"
    first = InteractionOwnership(lock_path)
    second = InteractionOwnership(lock_path)
    first.acquire()
    try:
        with pytest.raises(InteractionOwnershipError):
            second.acquire()
    finally:
        first.close()
        second.close()


def _session(
    tmp_path: Path,
    *,
    run: AsyncMock,
    end: AsyncMock,
    audio_queue: asyncio.Queue[bytes] | None = None,
) -> tuple[InteractionSession, MagicMock, MagicMock]:
    pipeline = MagicMock(processors=[])
    pipeline_factory = MagicMock(return_value=pipeline)
    worker = MagicMock()
    worker.queue_frame = AsyncMock()
    worker_factory = MagicMock(return_value=worker)
    runner = MagicMock()
    runner.add_workers = AsyncMock()
    runner.run = run
    runner.end = end
    runner_factory = MagicMock(return_value=runner)
    session = InteractionSession(
        InteractionSettings(max_session_seconds=3600),
        audio_queue=audio_queue,
        ownership=InteractionOwnership(tmp_path / "interaction.lock"),
        pipeline_factory=pipeline_factory,
        worker_factory=worker_factory,
        runner_factory=runner_factory,
        stop_timeout_secs=0.01,
    )
    return session, pipeline_factory, runner


async def test_stop_requests_graceful_end_then_cancels_stuck_runner(tmp_path: Path) -> None:
    async def hung() -> None:
        await asyncio.Event().wait()

    queue: asyncio.Queue[bytes] = asyncio.Queue()
    queue.put_nowait(b"stale")
    session, _factory, runner = _session(
        tmp_path,
        run=AsyncMock(side_effect=hung),
        end=AsyncMock(),
        audio_queue=queue,
    )
    await session.start()
    task = session.runner_task
    assert task is not None
    await asyncio.sleep(0)
    await session.stop()
    runner.end.assert_awaited_once()
    assert task.cancelled()
    assert queue.empty()
    assert session.state is InteractionSessionState.STOPPED


async def test_restart_preserves_persona_and_duplex(tmp_path: Path) -> None:
    stopped = asyncio.Event()

    async def run() -> None:
        await stopped.wait()

    async def end(*_args: object, **_kwargs: object) -> None:
        stopped.set()

    session, pipeline_factory, _runner = _session(
        tmp_path,
        run=AsyncMock(side_effect=run),
        end=AsyncMock(side_effect=end),
    )
    session.set_persona("你是孔子")
    session.set_duplex_mode(DuplexMode.HEADPHONE_DUPLEX)
    await session.start()
    await session.restart()
    assert session.persona == "你是孔子"
    assert session.duplex_mode is DuplexMode.HEADPHONE_DUPLEX
    assert pipeline_factory.call_count == 2
    assert pipeline_factory.call_args.kwargs["persona"] == "你是孔子"
    await session.stop()
