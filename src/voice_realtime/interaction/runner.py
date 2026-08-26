"""`vr-interact`：以 headless 所有者启动统一 InteractionSession。"""

from __future__ import annotations

import asyncio
import logging

from voice_realtime.config import get_settings
from voice_realtime.interaction.nltk_data import ensure_punkt_tab
from voice_realtime.interaction.ownership import InteractionOwnershipError
from voice_realtime.interaction.session import InteractionSession
from voice_realtime.logging import setup_logging

logger = logging.getLogger(__name__)


async def run() -> None:
    settings = get_settings()
    setup_logging("interact")
    ensure_punkt_tab()
    logger.info("交互管道配置:\n%s", settings.interaction.model_dump())
    session = InteractionSession(settings.interaction, handle_signals=True)
    try:
        await session.start()
        logger.info(
            "语音交互已启动（Ctrl-C 退出，最大会话时长: %s 秒）",
            settings.interaction.max_session_seconds,
        )
        task = session.runner_task
        if task is not None:
            await task
    finally:
        await session.stop(reason="headless 入口退出")


def main() -> None:
    try:
        asyncio.run(run())
    except InteractionOwnershipError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        logger.info("收到中断信号，优雅退出")


if __name__ == "__main__":
    main()
