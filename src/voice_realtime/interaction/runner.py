"""`vr-interact` CLI：启动 Pipecat 语音交互管道。

装配 FunASR STT → LM Studio → TTS 桥 → 本地播放，处理 SIGINT 优雅退出。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.workers.runner import WorkerRunner

from voice_realtime.config import TTS_OUTPUT_SAMPLE_RATE, get_settings
from voice_realtime.interaction.nltk_data import ensure_punkt_tab
from voice_realtime.interaction.pipeline import build_pipeline
from voice_realtime.logging import setup_logging

logger = logging.getLogger(__name__)


async def _end_after_timeout(runner: WorkerRunner, seconds: int) -> None:
    await asyncio.sleep(seconds)
    logger.info("会话已达到最大时长 %s 秒，正在优雅停止", seconds)
    await runner.end(reason=f"达到最大会话时长 {seconds} 秒")


async def run() -> None:
    settings = get_settings()
    setup_logging()
    ensure_punkt_tab()
    logger.info("交互管道配置:\n%s", settings.interaction.model_dump())
    pipeline = build_pipeline(settings.interaction)
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=settings.interaction.sample_rate,
            audio_out_sample_rate=TTS_OUTPUT_SAMPLE_RATE,
        ),
        idle_timeout_secs=None,
    )
    runner = WorkerRunner()
    await runner.add_workers(worker)
    max_session_seconds = settings.interaction.max_session_seconds
    logger.info("语音交互已启动（Ctrl-C 退出，最大会话时长: %s 秒）", max_session_seconds)

    # Pipecat 1.7.0 的 WorkerRunner.end() 是 awaitable 公共 API，run() 等待其 shutdown event；
    # 用计时任务触发优雅停止，不干扰 WorkerRunner 自带的 SIGINT 路径。
    timeout_task = asyncio.create_task(_end_after_timeout(runner, max_session_seconds))
    try:
        await runner.run()
    finally:
        timeout_task.cancel()
        with suppress(asyncio.CancelledError):
            await timeout_task


def main() -> None:
    """`vr-interact` 控制台入口。"""
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("收到中断信号，优雅退出")


if __name__ == "__main__":
    main()
