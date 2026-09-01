"""普通字幕 SpeechRail session：supervisor、send loop、backoff 与 epoch。

浏览器 sender 与 SRT 归档不属于本类；它通过 callbacks 交付窗口/状态，
并使用 facade 注入的共享 PCM queue。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from voice_realtime.asr.contracts import ASREvent, ASRSessionContext, StreamingTranscriber
from voice_realtime.asr.presenters import legacy_ready_payload, legacy_subtitle_payload

logger = logging.getLogger(__name__)

TranscriberFactory = Callable[[ASRSessionContext], StreamingTranscriber]
PayloadSink = Callable[[dict[str, object]], Awaitable[None]]


class SubtitleSessionState(StrEnum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    BACKOFF = "backoff"
    PAUSED = "paused"


@dataclass(frozen=True, slots=True)
class SubtitlePreparation:
    """普通字幕连接已 ready、尚未接收 PCM 的一次性凭证。"""

    generation: int


class StandardSubtitleSession:
    """拥有普通字幕流、ready/active、supervisor、send loop、backoff 与 epoch。"""

    def __init__(
        self,
        *,
        audio_queue: asyncio.Queue[bytes],
        transcriber_factory: TranscriberFactory,
        backoff_delays: Sequence[float],
        stop_event: asyncio.Event,
        running: Callable[[], bool],
        capture_active: Callable[[], bool],
        on_payload: PayloadSink,
        on_state: Callable[[SubtitleSessionState], None],
        on_epoch_opened: Callable[[], None],
        on_epoch_closed: Callable[[int], Awaitable[None]],
        on_reconnect: Callable[[], None],
        on_last_event: Callable[[], None],
        on_last_error: Callable[[str | None], None],
        on_dropped_chunk: Callable[[], None],
    ) -> None:
        self._audio_queue = audio_queue
        self._transcriber_factory = transcriber_factory
        self._backoff_delays = tuple(backoff_delays)
        self._stop_event = stop_event
        self._running = running
        self._capture_active = capture_active
        self._on_payload = on_payload
        self._on_state = on_state
        self._on_epoch_opened = on_epoch_opened
        self._on_epoch_closed = on_epoch_closed
        self._on_reconnect = on_reconnect
        self._on_last_event = on_last_event
        self._on_last_error = on_last_error
        self._on_dropped_chunk = on_dropped_chunk

        self._stream: StreamingTranscriber | None = None
        self._prepared: SubtitlePreparation | None = None
        self._committed = False
        self._supervisor_task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._active = asyncio.Event()
        self._epoch = 0
        self._epoch_open = False

    @property
    def ready(self) -> asyncio.Event:
        return self._ready

    @property
    def active(self) -> asyncio.Event:
        return self._active

    @property
    def committed(self) -> bool:
        return self._committed

    @property
    def has_stream(self) -> bool:
        return self._stream is not None

    @property
    def epoch(self) -> int:
        return self._epoch

    def reset_flags(self) -> None:
        """start() 时清空会话事件。"""
        self._ready.clear()
        self._active.clear()

    async def prepare(self, *, timeout_secs: float) -> SubtitlePreparation:
        """建立普通字幕流并等待 ready，但不接收 PCM。"""
        if timeout_secs <= 0:
            raise ValueError("timeout_secs 必须大于 0")
        if self._committed or self._prepared is not None:
            raise RuntimeError("普通字幕已活动或正在准备")
        epoch = await self._open_epoch()
        preparation = SubtitlePreparation(generation=epoch)
        self._prepared = preparation
        self._ready.clear()
        self._active.clear()
        self._drain_queue()
        self._on_state(SubtitleSessionState.CONNECTING)
        stream = self._transcriber_factory(
            ASRSessionContext(source_epoch=epoch, offset_ms=0, purpose="subtitles")
        )
        self._stream = stream
        try:
            async with asyncio.timeout(timeout_secs):
                await stream.connect()
                if not self._running() or self._prepared is not preparation:
                    raise RuntimeError("普通字幕 preparation 已取消")
                self._on_last_error(None)
                self._on_state(SubtitleSessionState.CONNECTED)
                self._supervisor_task = asyncio.create_task(self._supervise(stream))
                await self._ready.wait()
        except TimeoutError as exc:
            await self.close_stream()
            raise RuntimeError("SpeechRail 未完成 transcription session 初始化") from exc
        except BaseException:
            await self.close_stream()
            raise
        return preparation

    def commit(self, preparation: SubtitlePreparation) -> None:
        """同步提升 ready 的普通字幕 preparation。"""
        if self._prepared is not preparation:
            raise RuntimeError("无效或已消费的 browser preparation")
        task = self._supervisor_task
        if (
            not self._running()
            or self._stream is None
            or not self._ready.is_set()
            or task is None
            or task.done()
        ):
            raise RuntimeError("browser preparation 未 ready")
        self._prepared = None
        self._committed = True
        self._active.set()
        self._on_state(SubtitleSessionState.CONNECTED)

    async def abort_prepared(self, preparation: SubtitlePreparation) -> None:
        """消费并关闭尚未提交的普通字幕 preparation。"""
        if self._prepared is not preparation:
            raise RuntimeError("无效或已消费的 browser preparation")
        await self.close_stream()

    async def close_stream(self) -> None:
        """停用普通字幕：关闭任务与流、封存 epoch 并清空待发 PCM。"""
        self._prepared = None
        self._committed = False
        self._active.clear()
        task = self._supervisor_task
        self._supervisor_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        stream = self._stream
        self._stream = None
        if stream is not None:
            with contextlib.suppress(Exception):
                await stream.close()
        await self._close_epoch()
        self._drain_queue()

    async def reset_stream(self) -> None:
        """clear_subtitles：关闭当前浏览器流而不封存 epoch。"""
        stream = self._stream
        self._stream = None
        self._ready.clear()
        if stream is not None:
            with contextlib.suppress(Exception):
                await stream.close()

    async def stop(self) -> None:
        """终止会话并回收浏览器流资源。"""
        await self.close_stream()

    async def push_audio(self, data: bytes) -> None:
        """普通字幕已提交且流 ready 时接收 s16le 音频。"""
        if (
            not self._running()
            or not self._committed
            or self._stream is None
            or not self._ready.is_set()
        ):
            return
        if self._audio_queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._audio_queue.get_nowait()
                self._audio_queue.task_done()
                self._on_dropped_chunk()
        with contextlib.suppress(asyncio.QueueFull):
            self._audio_queue.put_nowait(data)

    async def push_audio_batch(self) -> None:
        """兼容性批量发送入口；只发送当前订阅期内已排队的音频。"""
        stream = self._stream
        if stream is None or not self._committed:
            self._drain_queue()
            return
        while True:
            try:
                data = self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await stream.send_audio(data)
            finally:
                self._audio_queue.task_done()

    async def process_loop(self) -> None:
        """兼容既有测试/调用者的接收循环入口。"""
        await self._event_recv_loop(self._stream)

    async def _open_epoch(self) -> int:
        """关闭旧边界并建立一个不复用时间轴的新普通字幕 epoch。"""
        await self._close_epoch()
        self._epoch += 1
        self._epoch_open = True
        self._on_epoch_opened()
        self._ready.clear()
        return self._epoch

    async def _close_epoch(self) -> None:
        """归档当前 epoch 并通知 facade 广播 reset。"""
        if not self._epoch_open:
            return
        epoch = self._epoch
        await self._on_epoch_closed(epoch)
        self._epoch_open = False

    async def _supervise(self, initial_stream: StreamingTranscriber) -> None:
        attempt = 0
        stream: StreamingTranscriber | None = initial_stream
        while self._running() and (self._prepared is not None or self._committed):
            try:
                if stream is None:
                    self._on_reconnect()
                    epoch = await self._open_epoch()
                    self._on_state(SubtitleSessionState.CONNECTING)
                    stream = self._transcriber_factory(
                        ASRSessionContext(
                            source_epoch=epoch,
                            offset_ms=0,
                            purpose="subtitles",
                        )
                    )
                    self._stream = stream
                    await stream.connect()
                    if not self._running() or not self._committed:
                        break
                    self._on_last_error(None)
                    self._on_state(SubtitleSessionState.CONNECTED)
                attempt = 0
                logger.info("StandardSubtitleSession: 已连接 SpeechRail %s", stream.uri)
                await self._serve_connection(stream)
                if self._running():
                    raise ConnectionError("SpeechRail 字幕连接已关闭")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._on_last_error(f"{type(exc).__name__}: {exc}")
                logger.warning(
                    "StandardSubtitleSession: SpeechRail 连接中断，将自动重连: %s", exc
                )
            finally:
                if stream is not None:
                    with contextlib.suppress(Exception):
                        await stream.close()
                if self._stream is stream:
                    self._stream = None
                await self._close_epoch()
                self._drain_queue()

            if not self._running() or not self._committed:
                if self._running() and not self._capture_active():
                    self._on_state(SubtitleSessionState.PAUSED)
                break
            delay = self._backoff_delays[min(attempt, len(self._backoff_delays) - 1)]
            attempt += 1
            self._on_state(SubtitleSessionState.BACKOFF)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            except TimeoutError:
                stream = None
                continue
            break

    async def _serve_connection(self, stream: StreamingTranscriber) -> None:
        send_task = asyncio.create_task(self._audio_send_loop(stream))
        recv_task = asyncio.create_task(self._event_recv_loop(stream))
        tasks = (send_task, recv_task)
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _audio_send_loop(self, stream: StreamingTranscriber) -> None:
        while self._running() and self._stream is stream:
            await self._active.wait()
            if not self._running() or self._stream is not stream:
                return
            chunk = await self._audio_queue.get()
            try:
                if self._committed:
                    await stream.send_audio(chunk)
            finally:
                self._audio_queue.task_done()

    async def _event_recv_loop(self, stream: StreamingTranscriber | None) -> None:
        active_stream = stream or self._stream
        if active_stream is None:
            return
        async for event in active_stream.events():
            await self._handle_stream_event(event)

    async def _handle_stream_event(self, event: ASREvent) -> None:
        """只消费后端无关事件，并按完整领域窗口广播。"""
        self._on_last_event()
        if event.kind == "ready":
            self._ready.set()
            await self._on_payload(legacy_ready_payload())
            return
        if event.kind == "error":
            self._on_last_error(event.error_message)
            await self._on_payload(
                {"type": "error", "error": event.error_message or "ASR error"}
            )
            return
        if event.window is not None:
            await self._on_payload(legacy_subtitle_payload(event.window))

    def _drain_queue(self) -> None:
        while True:
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._audio_queue.task_done()


__all__ = [
    "StandardSubtitleSession",
    "SubtitlePreparation",
    "SubtitleSessionState",
    "TranscriberFactory",
]
