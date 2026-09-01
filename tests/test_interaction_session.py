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
    stt_factory: object | None = None,
    pipeline_factory: object | None = None,
    pipeline_factories: object | None = None,
) -> tuple[InteractionSession, MagicMock, MagicMock]:
    pipeline = MagicMock(processors=[])
    resolved_factory = (
        pipeline_factory if pipeline_factory is not None else MagicMock(return_value=pipeline)
    )
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
        pipeline_factory=resolved_factory,
        worker_factory=worker_factory,
        runner_factory=runner_factory,
        stop_timeout_secs=0.01,
        stt_factory=stt_factory,
        pipeline_factories=pipeline_factories,
    )
    return session, resolved_factory, runner


async def test_session_passes_stt_factory_to_pipeline(tmp_path: Path) -> None:
    stopped = asyncio.Event()

    async def run() -> None:
        await stopped.wait()

    async def end(*_args: object, **_kwargs: object) -> None:
        stopped.set()

    stt_factory = MagicMock(name="conversation_stt_factory")
    session, pipeline_factory, _runner = _session(
        tmp_path,
        run=AsyncMock(side_effect=run),
        end=AsyncMock(side_effect=end),
        stt_factory=stt_factory,
    )

    await session.start()

    assert pipeline_factory.call_args.kwargs["stt_factory"] is stt_factory
    await session.stop()


async def test_session_passes_factories_bundle_to_accepting_factory(
    tmp_path: Path,
) -> None:
    stopped = asyncio.Event()

    async def run() -> None:
        await stopped.wait()

    async def end(*_args: object, **_kwargs: object) -> None:
        stopped.set()

    received: dict[str, object] = {}

    def recording_factory(
        settings: object, **kwargs: object
    ) -> MagicMock:
        received.update(kwargs)
        return MagicMock(processors=[])

    bundle = MagicMock(name="pipeline_factories")
    session, _factory, _runner = _session(
        tmp_path,
        run=AsyncMock(side_effect=run),
        end=AsyncMock(side_effect=end),
        pipeline_factory=recording_factory,
        pipeline_factories=bundle,
    )

    await session.start()

    assert received["factories"] is bundle
    await session.stop()


async def test_session_does_not_pass_factories_to_closed_factory(
    tmp_path: Path,
) -> None:
    stopped = asyncio.Event()

    async def run() -> None:
        await stopped.wait()

    async def end(*_args: object, **_kwargs: object) -> None:
        stopped.set()

    pipeline = MagicMock(processors=[])
    calls: list[dict[str, object]] = []

    def closed_factory(
        settings: object,
        *,
        persona: object | None = None,
        audio_queue: object | None = None,
        echo_state: object | None = None,
    ) -> MagicMock:
        calls.append(
            {"persona": persona, "audio_queue": audio_queue, "echo_state": echo_state}
        )
        return pipeline

    session, _factory, _runner = _session(
        tmp_path,
        run=AsyncMock(side_effect=run),
        end=AsyncMock(side_effect=end),
        pipeline_factory=closed_factory,
        pipeline_factories=MagicMock(name="bundle"),
    )

    await session.start()

    assert calls == [{"persona": None, "audio_queue": None, "echo_state": session.echo_state}]
    await session.stop()


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
    observer = MagicMock(name="observer")
    session._observers = [observer]  # type: ignore[attr-defined]
    await session.start()
    first_observers = session._worker_factory.call_args.kwargs["observers"]  # type: ignore[attr-defined]
    first_observers.append(MagicMock(name="framework-added-observer"))
    await session.restart()
    assert session.persona == "你是孔子"
    assert session.duplex_mode is DuplexMode.HEADPHONE_DUPLEX
    assert pipeline_factory.call_count == 2
    assert pipeline_factory.call_args.kwargs["persona"] == "你是孔子"
    second_observers = session._worker_factory.call_args.kwargs["observers"]  # type: ignore[attr-defined]
    assert second_observers == [observer]
    await session.stop()


async def test_echo_state_and_is_echo_suppressing(tmp_path: Path) -> None:
    """测试 InteractionSession 的 echo_state 与 is_echo_suppressing 状态。"""
    stopped = asyncio.Event()

    async def run() -> None:
        await stopped.wait()

    async def end(*_args: object, **_kwargs: object) -> None:
        stopped.set()

    session, _factory, _runner = _session(
        tmp_path,
        run=AsyncMock(side_effect=run),
        end=AsyncMock(side_effect=end),
    )
    # 未启动时 is_echo_suppressing 为 False
    assert not session.is_echo_suppressing()

    await session.start()
    assert session.active
    assert not session.is_echo_suppressing()

    # 模拟 TTS 播报启动
    session.echo_state.on_tts_started()
    assert session.is_echo_suppressing()

    # 模拟 TTS 结束
    session.echo_state.on_tts_stopped()
    # 在 hangover 期内仍处于 suppressing
    assert session.is_echo_suppressing()

    await session.stop()
    # 会话停止后 reset，is_echo_suppressing 为 False
    assert not session.is_echo_suppressing()


async def test_send_text_queues_frames(tmp_path: Path) -> None:
    """测试 send_text 推送文本帧并显式触发一次 LLM 运行。"""
    stopped = asyncio.Event()

    async def run() -> None:
        await stopped.wait()

    async def end(*_args: object, **_kwargs: object) -> None:
        stopped.set()

    session, _factory, _runner = _session(
        tmp_path,
        run=AsyncMock(side_effect=run),
        end=AsyncMock(side_effect=end),
    )

    with pytest.raises(RuntimeError, match="交互会话未运行"):
        await session.send_text("你好")

    await session.start()
    assert session.active
    worker = session.worker
    assert worker is not None

    await session.send_text("你好，语音助手")
    assert worker.queue_frame.call_count == 2
    first_frame = worker.queue_frame.call_args_list[0].args[0]
    second_frame = worker.queue_frame.call_args_list[1].args[0]
    assert type(first_frame).__name__ == "TranscriptionFrame"
    assert first_frame.text == "你好，语音助手"
    assert type(second_frame).__name__ == "LLMRunFrame"

    await session.stop()
