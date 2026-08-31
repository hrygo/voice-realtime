"""统一音频来源生命周期及麦克风适配器。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID

from voice_realtime.audio.frame import (
    AudioFrame,
    AudioSourceKind,
    AudioSourceRole,
)
from voice_realtime.audio.hub import AudioHub


class AudioSourceState(StrEnum):
    """音频来源的两阶段采集状态。"""

    STOPPED = "stopped"
    PREPARING = "preparing"
    READY = "ready"
    ACTIVE = "active"
    DEGRADED = "degraded"
    RECOVERING = "recovering"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AudioSourceHealth:
    """不包含队列对象和 PCM 内容的来源诊断快照。"""

    state: AudioSourceState
    queued_frames: int
    dropped_frames: int
    last_sequence: int | None
    last_host_time_ns: int | None


@runtime_checkable
class AudioSource(Protocol):
    """所有实时音频来源必须实现的两阶段契约。"""

    @property
    def kind(self) -> AudioSourceKind:
        """返回稳定来源类型。"""

    @property
    def role(self) -> AudioSourceRole:
        """返回 near-end 或 far-end 角色。"""

    @property
    def state(self) -> AudioSourceState:
        """返回当前生命周期状态。"""

    async def prepare(self, capture_id: UUID) -> None:
        """准备来源资源，但不提交业务 PCM。"""

    async def commit(self) -> None:
        """提交已准备来源并允许产生 PCM。"""

    async def abort(self) -> None:
        """回滚准备或活动来源。"""

    async def stop(self) -> None:
        """幂等停止来源并释放资源。"""

    def frames(self) -> AsyncIterator[AudioFrame]:
        """按来源顺序读取归一化帧。"""

    def health(self) -> AudioSourceHealth:
        """返回无敏感内容的诊断快照。"""


class MicrophoneSource:
    """把既有 `AudioHub` PCM sink 适配为统一帧来源。"""

    def __init__(
        self,
        hub: AudioHub,
        *,
        source_id: str,
        queue_size: int = 8,
    ) -> None:
        normalized_source_id = source_id.strip()
        if not normalized_source_id:
            raise ValueError("source_id must not be empty")
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self._hub = hub
        self._source_id = normalized_source_id
        self._sink_name = f"audio-source:{normalized_source_id}"
        self._queue: asyncio.Queue[AudioFrame] = asyncio.Queue(maxsize=queue_size)
        self._state = AudioSourceState.STOPPED
        self._capture_id: UUID | None = None
        self._registered = False
        self._next_sequence = 0
        self._dropped_frames = 0
        self._last_sequence: int | None = None
        self._last_host_time_ns: int | None = None

    @property
    def kind(self) -> AudioSourceKind:
        return AudioSourceKind.MICROPHONE

    @property
    def role(self) -> AudioSourceRole:
        return AudioSourceRole.NEAR_END

    @property
    def state(self) -> AudioSourceState:
        return self._state

    async def prepare(self, capture_id: UUID) -> None:
        if self._state is not AudioSourceState.STOPPED:
            raise RuntimeError("microphone source must be stopped before prepare")
        self._state = AudioSourceState.PREPARING
        self._capture_id = capture_id
        self._next_sequence = 0
        self._dropped_frames = 0
        self._last_sequence = None
        self._last_host_time_ns = None
        self._drain_queue()
        try:
            self._hub.add_sink(self._sink_name, self._receive_pcm)
        except BaseException:
            self._capture_id = None
            self._state = AudioSourceState.STOPPED
            raise
        self._registered = True
        self._state = AudioSourceState.READY

    async def commit(self) -> None:
        if self._state is not AudioSourceState.READY or self._capture_id is None:
            raise RuntimeError("microphone source is not ready")
        self._state = AudioSourceState.ACTIVE

    async def abort(self) -> None:
        await self._release()

    async def stop(self) -> None:
        await self._release()

    async def frames(self) -> AsyncIterator[AudioFrame]:
        while True:
            frame = await self._queue.get()
            try:
                yield frame
            finally:
                self._queue.task_done()

    def health(self) -> AudioSourceHealth:
        return AudioSourceHealth(
            state=self._state,
            queued_frames=self._queue.qsize(),
            dropped_frames=self._dropped_frames,
            last_sequence=self._last_sequence,
            last_host_time_ns=self._last_host_time_ns,
        )

    async def _receive_pcm(self, pcm: bytes) -> None:
        capture_id = self._capture_id
        if self._state is not AudioSourceState.ACTIVE or capture_id is None:
            return
        sequence = self._next_sequence
        self._next_sequence += 1
        host_time_ns = time.monotonic_ns()
        frame = AudioFrame(
            capture_id=capture_id,
            source_id=self._source_id,
            source_kind=self.kind,
            source_role=self.role,
            device_generation=0,
            sequence=sequence,
            host_time_ns=host_time_ns,
            pcm=pcm,
        )
        if self._queue.full():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                pass
            self._dropped_frames += 1
        self._queue.put_nowait(frame)
        self._last_sequence = sequence
        self._last_host_time_ns = host_time_ns

    async def _release(self) -> None:
        registered = self._registered
        self._registered = False
        self._state = AudioSourceState.STOPPED
        self._capture_id = None
        if registered:
            await self._hub.remove_sink(self._sink_name)
        self._drain_queue()

    def _drain_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                return
