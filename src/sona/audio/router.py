"""统一音频来源的两阶段路由与有界背压。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable
from contextlib import suppress
from dataclasses import dataclass
from uuid import UUID

from sona.audio.frame import AudioFrame, AudioSourceKind
from sona.audio.profile import CaptureMode, CaptureProfile
from sona.audio.source import AudioSource, AudioSourceState

logger = logging.getLogger(__name__)


class UnsupportedCaptureProfileError(RuntimeError):
    """当前路由阶段不支持请求的来源布局。"""


@dataclass(frozen=True, slots=True)
class RouterHealth:
    """不包含 PCM 和内部任务对象的路由诊断快照。"""

    state: AudioSourceState
    active_kind: AudioSourceKind | None
    queued_frames: int
    dropped_frames: int


class AudioSourceRouter:
    """把一个已准备来源转为单一、有界的业务帧流。"""

    def __init__(
        self,
        sources: Iterable[AudioSource],
        *,
        queue_size: int = 8,
    ) -> None:
        source_map: dict[AudioSourceKind, AudioSource] = {}
        for source in sources:
            if source.kind in source_map:
                raise ValueError(f"duplicate audio source kind: {source.kind}")
            source_map[source.kind] = source
        self._sources = source_map
        self._queue: asyncio.Queue[AudioFrame] = asyncio.Queue(
            maxsize=max(1, queue_size)
        )
        self._state = AudioSourceState.STOPPED
        self._active_source: AudioSource | None = None
        self._capture_id: UUID | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._dropped_frames = 0

    async def prepare(self, profile: CaptureProfile, capture_id: UUID) -> None:
        """准备 single 来源；P0 在接触来源前拒绝 dual。"""
        if self._state is not AudioSourceState.STOPPED:
            raise RuntimeError("audio source router must be stopped before prepare")
        if profile.mode is CaptureMode.DUAL:
            raise UnsupportedCaptureProfileError(
                "dual capture requires DualSourceMixer"
            )

        spec = profile.sources[0]
        source = self._sources.get(spec.kind)
        if source is None or source.role is not spec.role:
            raise RuntimeError(f"audio source unavailable: {spec.kind}")

        self._drain_queue()
        self._dropped_frames = 0
        self._state = AudioSourceState.PREPARING
        try:
            await source.prepare(capture_id)
        except BaseException:
            await self._abort_source_after_failure(source)
            self._reset()
            raise

        self._active_source = source
        self._capture_id = capture_id
        self._state = AudioSourceState.READY

    async def commit(self) -> None:
        """提交已准备来源并启动唯一 pump task。"""
        source = self._active_source
        capture_id = self._capture_id
        if (
            self._state is not AudioSourceState.READY
            or source is None
            or capture_id is None
        ):
            raise RuntimeError("audio source router is not ready")

        try:
            await source.commit()
        except BaseException:
            await self._abort_source_after_failure(source)
            self._reset()
            raise

        self._state = AudioSourceState.ACTIVE
        self._pump_task = asyncio.create_task(
            self._pump(source, capture_id),
            name=f"audio-source-router:{source.kind}",
        )

    async def abort(self) -> None:
        """幂等回滚准备或活动来源。"""
        source = self._active_source
        await self._cancel_pump()
        try:
            if source is not None:
                await source.abort()
        finally:
            self._reset()

    async def stop(self) -> None:
        """幂等停止活动来源。"""
        source = self._active_source
        await self._cancel_pump()
        try:
            if source is not None:
                await source.stop()
        finally:
            self._reset()

    async def frames(self) -> AsyncIterator[AudioFrame]:
        """按到达顺序读取当前 capture 的归一化帧。"""
        while True:
            frame = await self._queue.get()
            try:
                yield frame
            finally:
                self._queue.task_done()

    def health(self) -> RouterHealth:
        """返回无音频内容的路由状态。"""
        active_kind = (
            self._active_source.kind if self._active_source is not None else None
        )
        return RouterHealth(
            state=self._state,
            active_kind=active_kind,
            queued_frames=self._queue.qsize(),
            dropped_frames=self._dropped_frames,
        )

    async def _pump(self, source: AudioSource, capture_id: UUID) -> None:
        try:
            async for frame in source.frames():
                if frame.capture_id != capture_id:
                    continue
                if self._queue.full():
                    try:
                        self._queue.get_nowait()
                        self._queue.task_done()
                    except asyncio.QueueEmpty:
                        pass
                    self._dropped_frames += 1
                self._queue.put_nowait(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._state = AudioSourceState.FAILED
            logger.exception("AudioSourceRouter: 来源 pump 异常退出")

    async def _cancel_pump(self) -> None:
        task = self._pump_task
        self._pump_task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _abort_source_after_failure(self, source: AudioSource) -> None:
        try:
            await source.abort()
        except (Exception, asyncio.CancelledError):
            logger.warning(
                "AudioSourceRouter: 来源失败后的 abort 未完成",
                exc_info=True,
            )

    def _reset(self) -> None:
        self._drain_queue()
        self._state = AudioSourceState.STOPPED
        self._active_source = None
        self._capture_id = None
        self._pump_task = None

    def _drain_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                return
