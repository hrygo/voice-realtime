"""组合字幕推理租约和单一音频来源，提交前不向业务转发 PCM。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID, uuid4

from sona.audio.frame import AudioFrame
from sona.audio.output_source import AudioCaptureError
from sona.audio.selection import SubtitleCaptureSelection
from sona.audio.source import AudioSource


class SubtitleSink(Protocol):
    @property
    def browser_capture_active(self) -> bool: ...
    async def prepare_browser_capture(self, *, timeout_secs: float) -> Any: ...
    def commit_browser_capture(self, preparation: Any) -> None: ...
    async def abort_browser_capture(self, preparation: Any) -> None: ...
    async def deactivate_browser_capture(self) -> None: ...
    async def push_audio(self, data: bytes) -> None: ...


@dataclass(frozen=True, slots=True)
class AudioPreparation:
    capture_id: UUID
    selection: SubtitleCaptureSelection
    subtitle: Any
    source: AudioSource | None


class SubtitleAudioWorkload:
    """由模式协调器串行调用；锁定输出设备，失败后等待用户重试。"""

    def __init__(
        self,
        sink: SubtitleSink,
        source_factory: Callable[[str], AudioSource],
        *,
        can_forward: Callable[[], bool],
        on_frame: Callable[[AudioFrame], None],
        on_state: Callable[[], None],
        frame_timeout: float = 5.0,
    ) -> None:
        self._sink = sink
        self._source_factory = source_factory
        self._can_forward = can_forward
        self._on_frame = on_frame
        self._on_state = on_state
        self._frame_timeout = frame_timeout
        self.selection = SubtitleCaptureSelection()
        self.error: str | None = None
        self._prepared: AudioPreparation | None = None
        self._source: AudioSource | None = None
        self._pump: asyncio.Task[None] | None = None
        self._cleanup: asyncio.Task[None] | None = None

    @property
    def browser_capture_active(self) -> bool:
        return self._sink.browser_capture_active

    @property
    def uses_output(self) -> bool:
        return self.selection.source == "physical_output"

    async def prepare_browser_capture(
        self, *, timeout_secs: float, capture: SubtitleCaptureSelection | None = None
    ) -> AudioPreparation:
        if self._cleanup is not None:
            await asyncio.shield(self._cleanup)
            self._cleanup = None
        if self._prepared is not None or self.browser_capture_active:
            raise RuntimeError("请先停止字幕，再切换音频来源")
        selection = capture or self.selection
        source = (
            self._source_factory(selection.device_ref)
            if selection.device_ref is not None else None
        )
        capture_id = uuid4()
        try:
            if source is not None:
                await source.prepare(capture_id)
            subtitle = await self._sink.prepare_browser_capture(timeout_secs=timeout_secs)
        except BaseException:
            if source is not None:
                with suppress(Exception):
                    await source.abort()
            raise
        preparation = AudioPreparation(capture_id, selection, subtitle, source)
        self._prepared = preparation
        return preparation

    async def commit_browser_capture(self, preparation: AudioPreparation) -> None:
        if self._prepared is not preparation:
            raise RuntimeError("无效的字幕音频 preparation")
        if preparation.source is not None:
            await preparation.source.commit()
        self._sink.commit_browser_capture(preparation.subtitle)
        self.selection = preparation.selection
        self.error = None
        self._source = preparation.source
        self._prepared = None
        if self._source is not None:
            self._pump = asyncio.create_task(self._forward(preparation), name="output-subtitles")

    async def abort_browser_capture(self, preparation: AudioPreparation) -> None:
        if self._prepared is not preparation:
            raise RuntimeError("无效的字幕音频 preparation")
        self._prepared = None
        try:
            if preparation.source is not None:
                await preparation.source.abort()
        finally:
            await self._sink.abort_browser_capture(preparation.subtitle)

    async def deactivate_browser_capture(self) -> None:
        await asyncio.shield(self._start_cleanup())

    def _start_cleanup(self) -> asyncio.Task[None]:
        if self._cleanup is None:
            self._cleanup = asyncio.create_task(self._release(), name="output-subtitles-cleanup")
            # 后台断流也消费异常；显式 stop 的调用者仍可从 await 得到相同异常。
            self._cleanup.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        return self._cleanup

    async def _release(self) -> None:
        task, self._pump = self._pump, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        source, self._source = self._source, None
        try:
            if source is not None:
                try:
                    await source.stop()
                except AudioCaptureError as exc:
                    # PhysicalOutputSource 在 finally 中回收进程；断连导致 stop ack
                    # 缺失不应阻断用户停止后重试。
                    self.error = str(exc)
        finally:
            await self._sink.deactivate_browser_capture()
            self._on_state()

    async def _forward(self, preparation: AudioPreparation) -> None:
        source = preparation.source
        assert source is not None
        iterator = source.frames().__aiter__()
        try:
            while True:
                frame = await asyncio.wait_for(anext(iterator), self._frame_timeout)
                if frame.capture_id != preparation.capture_id or not self._can_forward():
                    continue
                self._on_frame(frame)
                await self._sink.push_audio(frame.pcm)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = (
                str(exc) if isinstance(exc, AudioCaptureError)
                else "电脑声音采集已中断，请停止后重试"
            )
            # 失去来源后关闭推理流；保留来源选择，麦克风不能接替这次采集。
            self._start_cleanup()
