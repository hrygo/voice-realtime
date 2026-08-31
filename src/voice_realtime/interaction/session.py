"""UI 与 headless 入口共用的交互会话生命周期。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from enum import StrEnum

from pipecat.frames.frames import (
    LLMMessagesUpdateFrame,
    TranscriptionFrame,
    TTSUpdateSettingsFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.services.settings import TTSSettings
from pipecat.workers.runner import WorkerRunner

from voice_realtime.asr.contracts import ConversationSTTFactory
from voice_realtime.config import TTS_OUTPUT_SAMPLE_RATE, InteractionSettings
from voice_realtime.interaction.ownership import (
    InteractionOwnership,
    InteractionOwnershipError,
)
from voice_realtime.interaction.pipeline import (
    EchoState,
    EchoSuppressionProcessor,
    build_pipeline,
    build_system_prompt,
)
from voice_realtime.interaction.reasoning import LmStudioNativeLLMService
from voice_realtime.interaction.types import DuplexMode

logger = logging.getLogger(__name__)


class InteractionSessionState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"
    OWNERSHIP_CONFLICT = "ownership_conflict"


class InteractionSession:
    """串行化管理管道装配、运行、优雅停止、超时与用户偏好。"""

    def __init__(
        self,
        settings: InteractionSettings,
        *,
        audio_queue: asyncio.Queue[bytes] | None = None,
        observers: Sequence[BaseObserver] = (),
        ownership: InteractionOwnership | None = None,
        pipeline_factory: Callable[..., Pipeline] = build_pipeline,
        worker_factory: Callable[..., PipelineWorker] | None = None,
        runner_factory: Callable[[], WorkerRunner] | None = None,
        stop_timeout_secs: float = 3.0,
        handle_signals: bool = False,
        echo_state: EchoState | None = None,
        stt_factory: ConversationSTTFactory | None = None,
    ) -> None:
        self._settings = settings
        self._audio_queue = audio_queue
        self._observers = list(observers)
        self._ownership = ownership or InteractionOwnership()
        self._pipeline_factory = pipeline_factory
        self._worker_factory = worker_factory or PipelineWorker
        self._runner_factory = runner_factory or (
            lambda: WorkerRunner(handle_sigint=handle_signals)
        )
        self._stop_timeout_secs = stop_timeout_secs
        self._lock = asyncio.Lock()
        self._state = InteractionSessionState.STOPPED
        self._worker: PipelineWorker | None = None
        self._runner: WorkerRunner | None = None
        self._runner_task: asyncio.Task[None] | None = None
        self._timeout_task: asyncio.Task[None] | None = None
        self._echo_state = echo_state if echo_state is not None else EchoState()
        self._stt_factory = stt_factory
        self._echo_suppressor: EchoSuppressionProcessor | None = None
        self._llm_service: LmStudioNativeLLMService | None = None
        self._persona: str | None = None
        self._duplex_mode = DuplexMode.SPEAKER_FOCUS
        self._started_at: str | None = None

    @property
    def echo_state(self) -> EchoState:
        return self._echo_state

    def is_echo_suppressing(self, audio: bytes | None = None) -> bool:
        """检查当前会话是否处于回声抑制期（TTS 播报且未发生真人插话）。"""
        if not self.active:
            return False
        now = time.monotonic()
        if self._echo_suppressor is not None:
            return self._echo_suppressor.is_suppressing(now)
        return self._echo_state.is_suppressing(now, self._settings.echo_tail_hangover_secs)

    @property
    def state(self) -> InteractionSessionState:
        return self._state

    @property
    def active(self) -> bool:
        return (
            self._state is InteractionSessionState.RUNNING
            and self._runner_task is not None
            and not self._runner_task.done()
        )

    @property
    def worker(self) -> PipelineWorker | None:
        return self._worker

    @property
    def runner_task(self) -> asyncio.Task[None] | None:
        return self._runner_task

    @property
    def persona(self) -> str | None:
        return self._persona

    @property
    def duplex_mode(self) -> DuplexMode:
        return self._duplex_mode

    @property
    def started_at(self) -> str | None:
        return self._started_at

    async def start(self) -> None:
        async with self._lock:
            await self._start_locked()

    async def stop(self, reason: str = "会话停止") -> None:
        async with self._lock:
            await self._stop_locked(reason=reason, release_ownership=True)

    async def restart(self) -> None:
        async with self._lock:
            await self._stop_locked(reason="会话重启", release_ownership=False)
            await self._start_locked()

    def set_persona(self, persona: str) -> None:
        self._persona = persona

    def set_duplex_mode(self, mode: DuplexMode | str) -> None:
        self._duplex_mode = DuplexMode(mode)
        self._apply_duplex_mode()

    async def set_voice(self, voice: str) -> None:
        """Update the public preset and propagate it to a running TTS service."""
        self._settings.tts_voice = voice
        if self._worker is None or not self.active:
            return
        await self._worker.queue_frame(
            TTSUpdateSettingsFrame(delta=TTSSettings(voice=voice))
        )

    async def clear_context(self) -> None:
        async with self._lock:
            worker = self._worker
            if worker is None:
                return
            if self._llm_service is not None:
                self._llm_service.reset_conversation()
            prompt = build_system_prompt(self._persona)
            await worker.queue_frame(
                LLMMessagesUpdateFrame(messages=[{"role": "system", "content": prompt}])
            )

    async def send_text(self, text: str) -> None:
        async with self._lock:
            worker = self._worker
            if worker is None or not self.active:
                raise RuntimeError("交互会话未运行")
            now = datetime.now(UTC).isoformat()
            await worker.queue_frame(
                TranscriptionFrame(text=text, user_id="user", timestamp=now)
            )
            await worker.queue_frame(UserStoppedSpeakingFrame())

    async def _start_locked(self) -> None:
        if self._state in {InteractionSessionState.STARTING, InteractionSessionState.RUNNING}:
            return
        self._state = InteractionSessionState.STARTING
        try:
            self._ownership.acquire()
        except InteractionOwnershipError:
            self._state = InteractionSessionState.OWNERSHIP_CONFLICT
            raise
        try:
            self._echo_state.reset()
            pipeline_kwargs: dict[str, object] = {
                "persona": self._persona,
                "audio_queue": self._audio_queue,
                "echo_state": self._echo_state,
            }
            if self._stt_factory is not None:
                pipeline_kwargs["stt_factory"] = self._stt_factory
            pipeline = self._pipeline_factory(self._settings, **pipeline_kwargs)
            self._echo_suppressor = next(
                (
                    processor
                    for processor in pipeline.processors
                    if isinstance(processor, EchoSuppressionProcessor)
                ),
                None,
            )
            self._llm_service = next(
                (
                    processor
                    for processor in pipeline.processors
                    if isinstance(processor, LmStudioNativeLLMService)
                ),
                None,
            )
            self._apply_duplex_mode()
            self._worker = self._worker_factory(
                pipeline,
                observers=list(self._observers),
                params=PipelineParams(
                    audio_in_sample_rate=self._settings.sample_rate,
                    audio_out_sample_rate=TTS_OUTPUT_SAMPLE_RATE,
                ),
                idle_timeout_secs=None,
            )
            self._runner = self._runner_factory()
            await self._runner.add_workers(self._worker)
            self._runner_task = asyncio.create_task(self._run_runner(self._runner))
            if self._settings.max_session_seconds > 0:
                self._timeout_task = asyncio.create_task(
                    self._end_after_timeout(self._settings.max_session_seconds)
                )
            self._started_at = datetime.now(UTC).isoformat()
            self._state = InteractionSessionState.RUNNING
        except BaseException:
            if self._runner is not None:
                with contextlib.suppress(Exception):
                    await self._runner.end(reason="会话启动失败")
            if self._runner_task is not None:
                self._runner_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._runner_task
            self._state = InteractionSessionState.ERROR
            self._clear_runtime_references()
            self._ownership.close()
            raise

    async def _run_runner(self, runner: WorkerRunner) -> None:
        try:
            await runner.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("InteractionSession: 管道运行异常")
            self._state = InteractionSessionState.ERROR
        finally:
            if self._state in {
                InteractionSessionState.RUNNING,
                InteractionSessionState.ERROR,
            }:
                if self._state is InteractionSessionState.RUNNING:
                    self._state = InteractionSessionState.STOPPED
                timeout_task = self._timeout_task
                if timeout_task is not None:
                    timeout_task.cancel()
                    self._timeout_task = None
                self._started_at = None
                self._ownership.close()

    async def _stop_locked(self, *, reason: str, release_ownership: bool) -> None:
        if self._state is InteractionSessionState.STOPPED and self._runner is None:
            if release_ownership:
                self._ownership.close()
            self._drain_audio_queue()
            return
        self._state = InteractionSessionState.STOPPING
        timeout_task = self._timeout_task
        self._timeout_task = None
        current_task = asyncio.current_task()
        if timeout_task is not None and timeout_task is not current_task:
            timeout_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await timeout_task
        runner = self._runner
        if runner is not None:
            with contextlib.suppress(Exception):
                await runner.end(reason=reason)
        task = self._runner_task
        if task is not None and task is not current_task and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self._stop_timeout_secs)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._clear_runtime_references()
        self._echo_state.reset()
        self._drain_audio_queue()
        if release_ownership:
            self._ownership.close()
        self._state = InteractionSessionState.STOPPED

    async def _end_after_timeout(self, seconds: int) -> None:
        await asyncio.sleep(seconds)
        logger.info("会话已达到最大时长 %s 秒，正在优雅停止", seconds)
        await self.stop(reason=f"达到最大会话时长 {seconds} 秒")

    def _apply_duplex_mode(self) -> None:
        suppressor = self._echo_suppressor
        if suppressor is None:
            return
        if self._duplex_mode is DuplexMode.HEADPHONE_DUPLEX:
            suppressor.set_mode(allow_barge_in=True, barge_in_gain=1.15, barge_in_frames=2)
        else:
            suppressor.set_mode(
                allow_barge_in=False,
                barge_in_gain=self._settings.echo_barge_in_gain,
                barge_in_frames=self._settings.echo_barge_in_frames,
            )

    def _drain_audio_queue(self) -> None:
        queue = self._audio_queue
        if queue is None:
            return
        while True:
            try:
                queue.get_nowait()
                queue.task_done()
            except asyncio.QueueEmpty:
                return

    def _clear_runtime_references(self) -> None:
        self._worker = None
        self._runner = None
        self._runner_task = None
        self._echo_suppressor = None
        self._llm_service = None
        self._started_at = None
