"""InteractionSession 与 LM Studio 原生会话链的生命周期测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from voice_realtime.config import InteractionSettings
from voice_realtime.interaction.ownership import InteractionOwnership
from voice_realtime.interaction.reasoning import LmStudioNativeLLMService
from voice_realtime.interaction.session import InteractionSession


async def test_clear_context_resets_native_chat_chain(tmp_path: Path) -> None:
    stopped = asyncio.Event()

    async def run() -> None:
        await stopped.wait()

    async def end(*_args: object, **_kwargs: object) -> None:
        stopped.set()

    native = MagicMock(spec=LmStudioNativeLLMService)
    pipeline = MagicMock(processors=[native])
    worker = MagicMock()
    worker.queue_frame = AsyncMock()
    runner = MagicMock()
    runner.add_workers = AsyncMock()
    runner.run = AsyncMock(side_effect=run)
    runner.end = AsyncMock(side_effect=end)
    session = InteractionSession(
        InteractionSettings(max_session_seconds=3600),
        ownership=InteractionOwnership(tmp_path / "interaction.lock"),
        pipeline_factory=MagicMock(return_value=pipeline),
        worker_factory=MagicMock(return_value=worker),
        runner_factory=MagicMock(return_value=runner),
    )

    await session.start()
    await session.clear_context()

    native.reset_conversation.assert_called_once_with()
    worker.queue_frame.assert_awaited_once()
    await session.stop()
    assert session._llm_service is None  # type: ignore[attr-defined]
