"""`vr-interact` CLI：启动 Pipecat 语音交互管道。

装配 FunASR STT → LM Studio → TTS 桥 → 本地播放，处理 SIGINT 优雅退出。
"""

from __future__ import annotations

import asyncio
import logging

from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.workers.runner import WorkerRunner

from voice_realtime.config import get_settings
from voice_realtime.interaction.pipeline import OUTPUT_SAMPLE_RATE, build_pipeline

logger = logging.getLogger(__name__)


async def run() -> None:
    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logger.info("交互管道配置:\n%s", settings.interaction.model_dump())
    pipeline = build_pipeline(settings.interaction)
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=settings.interaction.sample_rate,
            audio_out_sample_rate=OUTPUT_SAMPLE_RATE,
        ),
    )
    runner = WorkerRunner()
    await runner.add_workers(worker)
    logger.info("语音交互已启动（Ctrl-C 退出）")
    await runner.run()


def main() -> None:
    """`vr-interact` 控制台入口。"""
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("收到中断信号，优雅退出")


if __name__ == "__main__":
    main()
