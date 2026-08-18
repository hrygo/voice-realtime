"""UIRuntime：Voice Studio 运行时装配（采集 → 扇出 → 管道/字幕 + 状态桥）。

把 AudioHub（单源采集）、SubtitleProxy（wlk 字幕）、AudioInjector 注入模式的
Pipecat 管道与 StatusBridgeObserver 组装为 UI 进程内的一个运行时：

    AudioHub ──► audio_queue ──► PipelineWorker(注入式) ──► observer ─► /ws/assistant
         │                                                                      ▲
         └──► SubtitleProxy.push_audio ──► wlk ──► SubtitleEvent ──► /ws/subtitles

容错设计：任一外部依赖（wlk / 麦克风 / LM Studio / TTS 桥）不可用时仅记录
warning，UI 其余能力（健康灯、已就绪模块的 WS 流）不受影响。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.workers.runner import WorkerRunner

from voice_realtime.audio.hub import AudioHub
from voice_realtime.config import TTS_OUTPUT_SAMPLE_RATE, Settings
from voice_realtime.interaction.pipeline import build_pipeline
from voice_realtime.ui.assistant_bridge import StatusBridgeObserver
from voice_realtime.ui.subtitle_proxy import SubtitleProxy

logger = logging.getLogger(__name__)

# AudioInjector 队列上界：w lk/管道处理不及时时丢弃，避免无限积压
AUDIO_QUEUE_MAXSIZE = 256


class UIRuntime:
    """UI 进程运行时的组件门面。

    用法：
        runtime = UIRuntime(settings)
        await runtime.start()
        ...
        await runtime.stop()
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._started = False
        self.observer = StatusBridgeObserver()
        self.audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=AUDIO_QUEUE_MAXSIZE)
        self.hub = AudioHub(device_index=settings.interaction.input_device)
        self.subtitle_proxy = SubtitleProxy(settings.subtitles)
        self._worker: PipelineWorker | None = None
        self._runner_task: asyncio.Task[Any] | None = None

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        """装配并启动各组件（顺序：字幕 → 接线 → 采集 → 管道）。"""
        if self._started:
            return
        await self._start_subtitle_proxy()
        self._wire_sinks()
        # 麦克风不可用时跳过采集与管道，字幕等其余能力保持可用
        if await self._start_hub():
            self._start_pipeline()
        self._started = True
        logger.info("UIRuntime: 已启动 (pipeline=%s)", self._worker is not None)

    async def stop(self) -> None:
        """逆序停止：管道 → 采集 → 字幕。"""
        if not self._started:
            return
        await self._stop_pipeline()
        await self.hub.stop()
        await self.subtitle_proxy.stop()
        self._started = False
        logger.info("UIRuntime: 已停止")

    # ---------- WS 接入点 ----------

    @property
    def pipelines_active(self) -> bool:
        """交互管道是否在运行（麦克风 + 外部推理均就绪）。"""
        return self._worker is not None and self._runner_task is not None

    # ---------- 内部装配 ----------

    async def _start_subtitle_proxy(self) -> None:
        """启动 wlk 字幕代理；连接失败仅告警（后续 WS 仍可重连重试）。"""
        try:
            await self.subtitle_proxy.start()
        except Exception:
            logger.warning("UIRuntime: SubtitleProxy 启动失败（wlk 未运行？）", exc_info=True)

    def _wire_sinks(self) -> None:
        """AudioHub 单源扇出：pipecat 队列 + wlk 字幕。"""
        self.hub.add_sink("pipecat", self._enqueue_audio)
        self.hub.add_sink("subtitle", self.subtitle_proxy.push_audio)

    async def _start_hub(self) -> bool:
        """启动 AudioHub 采集；成功返回 True。"""
        try:
            await self.hub.start()
            return True
        except Exception:
            logger.warning("UIRuntime: AudioHub 启动失败（麦克风不可用？）", exc_info=True)
            return False

    async def _enqueue_audio(self, data: bytes) -> None:
        """AudioHub → AudioInjector 队列（有界背压：队满丢帧防积压）。"""
        with contextlib.suppress(asyncio.QueueFull):
            self.audio_queue.put_nowait(data)

    def _start_pipeline(self) -> None:
        """装配注入式交互管道并以 WorkerRunner 后台运行。"""
        try:
            pipeline = build_pipeline(
                self._settings.interaction, audio_queue=self.audio_queue
            )
        except Exception:
            logger.exception("UIRuntime: 交互管道装配失败")
            return
        worker = PipelineWorker(
            pipeline,
            observers=[self.observer],
            params=PipelineParams(
                audio_in_sample_rate=self._settings.interaction.sample_rate,
                audio_out_sample_rate=TTS_OUTPUT_SAMPLE_RATE,
            ),
            idle_timeout_secs=None,
        )
        self._worker = worker
        runner = WorkerRunner()
        self._runner_task = asyncio.create_task(self._run_pipeline(runner, worker))

    async def _run_pipeline(self, runner: WorkerRunner, worker: PipelineWorker) -> None:
        """后台运行管道；异常退出记录日志不影响 UI 进程。"""
        try:
            await runner.add_workers(worker)
            await runner.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("UIRuntime: 交互管道运行异常（LM Studio/TTS 桥未就绪？）")

    async def _stop_pipeline(self) -> None:
        """停止管道 worker 并等待其退出。"""
        if self._runner_task is not None:
            self._runner_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, RuntimeError):
                await self._runner_task
            self._runner_task = None
        self._worker = None
