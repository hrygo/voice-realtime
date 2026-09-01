"""普通字幕与会议采集 SpeechRail session。

浏览器 sender 与 SRT 归档不属于本模块；两类 session 通过 callbacks 交付
窗口/状态，并使用 facade 注入的共享 PCM queue。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from voice_realtime.asr.contracts import ASREvent, ASRSessionContext, StreamingTranscriber
from voice_realtime.asr.models import ASRWindow
from voice_realtime.asr.presenters import legacy_ready_payload, legacy_subtitle_payload
from voice_realtime.meeting.models import TranscriptWindow

logger = logging.getLogger(__name__)

TranscriberFactory = Callable[[ASRSessionContext], StreamingTranscriber]
PayloadSink = Callable[[dict[str, object]], Awaitable[None]]
CapturePayloadSink = Callable[[dict[str, object], bool], Awaitable[None]]
WindowListener = Callable[[TranscriptWindow], Awaitable[None]]
GapListener = Callable[["TranscriptionGap"], Awaitable[None]]


def _diarization_group_id(owner: str | None) -> str:
    """Keep the application meeting identifier out of the public audio protocol."""
    if not owner:
        raise RuntimeError("meeting diarization requires a capture owner")
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()


class FinalizationTimeoutError(TimeoutError):
    """会议 ASR 未在时限内排空 PCM 或完成 EOF，携带最后已知窗口。"""

    code = "finalization_timeout"

    def __init__(self, last_window: TranscriptWindow | None) -> None:
        self.last_window = last_window
        super().__init__("SpeechRail finalization timed out")


FinalizationTimeout = FinalizationTimeoutError


@dataclass(frozen=True, slots=True)
class CapturePreparation:
    """会议采集连接已 ready、尚未接收 PCM 的一次性凭证。"""

    owner: str
    generation: int


@dataclass(frozen=True)
class TranscriptionGap:
    """SpeechRail 重连期间无法转录的样本时钟区间。"""

    source_epoch: int
    start_ms: int
    end_ms: int


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


class MeetingCaptureSession:
    """拥有会议采集流、event/send tasks、重连、gap 与 finalize 状态。"""

    def __init__(
        self,
        *,
        audio_queue: asyncio.Queue[bytes],
        transcriber_factory: TranscriberFactory,
        backoff_delays: Sequence[float],
        stop_event: asyncio.Event,
        running: Callable[[], bool],
        to_transcript_window: Callable[[ASRWindow], TranscriptWindow],
        on_payload: CapturePayloadSink,
        on_state: Callable[[SubtitleSessionState], None],
        on_gap: Callable[[], None],
        on_reconnect: Callable[[], None],
        on_last_event: Callable[[], None],
        on_last_error: Callable[[str | None], None],
    ) -> None:
        self._audio_queue = audio_queue
        self._transcriber_factory = transcriber_factory
        self._backoff_delays = tuple(backoff_delays)
        self._stop_event = stop_event
        self._running = running
        self._to_transcript_window = to_transcript_window
        self._on_payload = on_payload
        self._on_state = on_state
        self._on_gap = on_gap
        self._on_reconnect = on_reconnect
        self._on_last_event = on_last_event
        self._on_last_error = on_last_error

        self._stream: StreamingTranscriber | None = None
        self._prepared: CapturePreparation | None = None
        self._owner: str | None = None
        self._generation = 0
        self._epoch = 0
        self._offset_ms = 0
        self._audio_ms = 0
        self._input_ms = 0
        self._accept_audio = False
        self._speaker_count_hint: int | None = None
        self._active = asyncio.Event()
        self._ready = asyncio.Event()
        self._stream_available = asyncio.Event()
        self._ready_to_stop = asyncio.Event()
        self._event_task: asyncio.Task[None] | None = None
        self._send_task: asyncio.Task[None] | None = None
        self._last_window: TranscriptWindow | None = None
        self._event_listeners: list[WindowListener] = []
        self._gap_listeners: list[GapListener] = []

    @property
    def owner(self) -> str | None:
        return self._owner

    @property
    def accepting(self) -> bool:
        return self._accept_audio

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def last_window(self) -> TranscriptWindow | None:
        return self._last_window

    @property
    def has_stream(self) -> bool:
        return self._stream is not None

    @property
    def ready(self) -> asyncio.Event:
        return self._ready

    @property
    def stream_available(self) -> asyncio.Event:
        return self._stream_available

    def add_event_listener(self, listener: WindowListener) -> None:
        if listener not in self._event_listeners:
            self._event_listeners.append(listener)

    def remove_event_listener(self, listener: WindowListener) -> None:
        with contextlib.suppress(ValueError):
            self._event_listeners.remove(listener)

    def add_gap_listener(self, listener: GapListener) -> None:
        if listener not in self._gap_listeners:
            self._gap_listeners.append(listener)

    def remove_gap_listener(self, listener: GapListener) -> None:
        with contextlib.suppress(ValueError):
            self._gap_listeners.remove(listener)

    async def prepare(
        self,
        owner: str,
        *,
        timeout_secs: float,
        speaker_count_hint: int | None = None,
    ) -> CapturePreparation:
        """建立会议流并等待 ready，但不接收 PCM。"""
        if self._owner is not None:
            raise RuntimeError("已有会议采集租约")
        self._generation += 1
        preparation = CapturePreparation(owner=owner, generation=self._generation)
        self._prepared = preparation
        self._epoch += 1
        self._offset_ms = 0
        self._speaker_count_hint = speaker_count_hint
        self._audio_ms = 0
        self._input_ms = 0
        self._owner = owner
        self._accept_audio = False
        self._active.clear()
        self._ready.clear()
        self._stream_available.clear()
        self._ready_to_stop.clear()
        self._last_window = None
        self._on_state(SubtitleSessionState.CONNECTING)

        try:
            stream = self._transcriber_factory(
                ASRSessionContext(
                    source_epoch=self._epoch,
                    offset_ms=self._offset_ms,
                    purpose="meeting",
                    speaker_count_hint=self._speaker_count_hint,
                    diarization_group_id=_diarization_group_id(owner),
                ),
            )
            self._stream = stream
            async with asyncio.timeout(timeout_secs):
                await stream.connect()
                self._on_state(SubtitleSessionState.CONNECTED)
                self._stream_available.set()
                self._event_task = asyncio.create_task(self._event_loop(stream))
                self._send_task = asyncio.create_task(self._send_loop(stream))
                await self._ready.wait()
        except TimeoutError as exc:
            await self.close()
            raise RuntimeError("SpeechRail 未发送 ready event") from exc
        except BaseException:
            await self.close()
            raise
        return preparation

    def commit(self, preparation: CapturePreparation) -> None:
        """同步提升 ready 的会议 preparation。"""
        if self._prepared is not preparation:
            raise RuntimeError("无效或已消费的 capture preparation")
        if (
            self._stream is None
            or not self._ready.is_set()
            or self._event_task is None
            or self._event_task.done()
            or self._send_task is None
            or self._send_task.done()
        ):
            raise RuntimeError("capture preparation 未 ready")
        self._prepared = None
        self._accept_audio = True
        self._active.set()
        self._on_state(SubtitleSessionState.CONNECTED)

    async def abort_prepared(self, preparation: CapturePreparation) -> None:
        """消费并关闭尚未提交的会议 preparation。"""
        if self._prepared is not preparation:
            raise RuntimeError("无效或已消费的 capture preparation")
        await self.close()

    async def finish(self, *, timeout_secs: float) -> TranscriptWindow:
        """发送空 PCM EOF，等待最终快照和 ready_to_stop，再关闭 epoch。"""
        stream = self._stream
        if self._owner is None or stream is None or not self._accept_audio:
            raise RuntimeError("没有活动的会议采集租约")
        if timeout_secs <= 0:
            raise ValueError("timeout_secs 必须大于 0")
        loop = asyncio.get_running_loop()
        start_time = loop.time()
        deadline = start_time + timeout_secs
        logger.info("开始会议 ASR 优雅停机冲刷 (EOF)... 租约: %s", self._owner)
        self._accept_audio = False
        try:
            async with asyncio.timeout_at(deadline):
                await self._audio_queue.join()
                self._active.clear()
                final_window = await stream.finish()
                self._last_window = self._to_transcript_window(final_window)
            elapsed_ms = (loop.time() - start_time) * 1000
            logger.info("会议 ASR 优雅冲刷完成，耗时 %.1f ms", elapsed_ms)
        except TimeoutError as exc:
            elapsed_ms = (loop.time() - start_time) * 1000
            logger.warning(
                "会议 ASR 优雅冲刷超时 (%.1f ms > %.1fs)，执行强制截断封存",
                elapsed_ms,
                timeout_secs,
            )
            last_window = self._last_window
            await self.close()
            raise FinalizationTimeoutError(last_window) from exc
        except BaseException:
            await self.close()
            raise
        result = self._last_window or TranscriptWindow(source_epoch=self._epoch)
        await self.close()
        return result

    async def abort(self) -> None:
        """取消会议采集，不发送 EOF；已确认窗口仍由上层保留。"""
        self._accept_audio = False
        self._active.clear()
        await self.close()

    async def push_audio(self, data: bytes) -> None:
        """会议租约已提交时接收 s16le 音频。"""
        duration_ms = len(data) // 32
        start_ms = self._input_ms
        self._input_ms += duration_ms
        if self._stream is None:
            return
        if self._audio_queue.full():
            await self._notify_gap(start_ms, self._input_ms)
            return
        with contextlib.suppress(asyncio.QueueFull):
            self._audio_queue.put_nowait(data)

    async def close(self) -> None:
        """取消事件/发送任务、关闭流并清空待发 PCM；幂等。"""
        self._accept_audio = False
        self._prepared = None
        self._active.clear()
        event_task = self._event_task
        send_task = self._send_task
        self._event_task = None
        self._send_task = None
        for task in (event_task, send_task):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        tasks = [
            task
            for task in (event_task, send_task)
            if task is not None and task is not asyncio.current_task()
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        stream = self._stream
        self._stream = None
        self._ready.clear()
        self._ready_to_stop.clear()
        self._stream_available.clear()
        if stream is not None:
            with contextlib.suppress(Exception):
                await stream.close()
        self._owner = None
        self._drain_queue()

    async def _event_loop(self, stream: StreamingTranscriber) -> None:
        active_stream = stream
        try:
            while self._owner is not None:
                try:
                    async for event in active_stream.events():
                        await self._handle_event(event)
                    if not self._accept_audio:
                        return
                    next_stream = await self._reconnect(active_stream)
                    if next_stream is None:
                        return
                    active_stream = next_stream
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._on_last_error(f"{type(exc).__name__}: {exc}")
                    if not self._accept_audio:
                        return
                    next_stream = await self._reconnect(active_stream)
                    if next_stream is None:
                        return
                    active_stream = next_stream
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._on_last_error(f"{type(exc).__name__}: {exc}")

    async def _send_loop(self, stream: StreamingTranscriber) -> None:
        del stream
        pending: bytes | None = None
        try:
            while self._owner is not None:
                if pending is None:
                    await self._active.wait()
                    if self._owner is None:
                        return
                    await self._stream_available.wait()
                    if self._owner is None:
                        return
                    if self._stream is None:
                        self._stream_available.clear()
                        continue
                    pending = await self._audio_queue.get()
                active_stream = self._stream
                if active_stream is None:
                    self._stream_available.clear()
                    await self._stream_available.wait()
                    continue
                chunk = pending
                if chunk is None:
                    continue
                try:
                    await active_stream.send_audio(chunk)
                except Exception as exc:
                    self._on_last_error(f"{type(exc).__name__}: {exc}")
                    if self._stream is active_stream:
                        self._stream = None
                    self._stream_available.clear()
                    with contextlib.suppress(Exception):
                        await active_stream.close()
                    gap_start_ms = self._offset_ms + self._audio_ms
                    self._audio_ms += len(chunk) // 32
                    await self._notify_gap(
                        gap_start_ms,
                        self._offset_ms + self._audio_ms,
                    )
                    self._audio_queue.task_done()
                    pending = None
                    await asyncio.sleep(0)
                    continue
                self._audio_ms += len(chunk) // 32
                self._audio_queue.task_done()
                pending = None
        finally:
            if pending is not None:
                self._audio_queue.task_done()

    async def _notify_gap(self, start_ms: int, end_ms: int) -> None:
        if end_ms <= start_ms:
            return
        self._on_gap()
        gap = TranscriptionGap(
            source_epoch=self._epoch,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        for listener in tuple(self._gap_listeners):
            with contextlib.suppress(Exception):
                await listener(gap)

    async def _reconnect(self, old_stream: StreamingTranscriber) -> StreamingTranscriber | None:
        """建立新 ASR epoch，并显式通知无法补录的间隔。"""
        self._on_reconnect()
        with contextlib.suppress(Exception):
            await old_stream.close()
        if self._stream is old_stream:
            self._stream = None
        self._stream_available.clear()
        self._offset_ms += self._audio_ms
        self._audio_ms = 0
        self._epoch += 1
        gap_start_ms = self._offset_ms
        self._on_state(SubtitleSessionState.BACKOFF)
        for delay in self._backoff_delays:
            if self._owner is None or not self._running():
                return None
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                return None
            try:
                resume_offset_ms = self._input_ms
                await self._notify_gap(gap_start_ms, resume_offset_ms)
                self._offset_ms = resume_offset_ms
                gap_start_ms = resume_offset_ms
                self._on_state(SubtitleSessionState.CONNECTING)
                stream = self._transcriber_factory(
                    ASRSessionContext(
                        source_epoch=self._epoch,
                        offset_ms=self._offset_ms,
                        purpose="meeting",
                        speaker_count_hint=self._speaker_count_hint,
                        diarization_group_id=_diarization_group_id(self._owner),
                    )
                )
                await stream.connect()
                self._stream = stream
                self._on_state(SubtitleSessionState.CONNECTED)
                self._stream_available.set()
                self._ready.clear()
                return stream
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._on_last_error(f"{type(exc).__name__}: {exc}")
                self._drain_queue()
                self._on_state(SubtitleSessionState.BACKOFF)
        await self._notify_gap(gap_start_ms, self._input_ms)
        self._offset_ms = self._input_ms
        return None

    async def _handle_event(self, event: ASREvent) -> None:
        self._on_last_event()
        if event.kind == "ready":
            self._ready.set()
            return
        if event.kind == "error":
            self._on_last_error(event.error_message)
            await self._on_payload(
                {"type": "error", "error": event.error_message or "ASR error"},
                False,
            )
            return
        window = event.window
        if window is None:
            return
        transcript_window = self._to_transcript_window(window)
        self._last_window = transcript_window
        if event.kind == "final":
            self._ready_to_stop.set()
            return
        for listener in tuple(self._event_listeners):
            try:
                await listener(transcript_window)
            except Exception:
                logger.exception("MeetingCaptureSession: 会议转录监听器失败")
        await self._on_payload(legacy_subtitle_payload(window), False)

    def _drain_queue(self) -> None:
        while True:
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._audio_queue.task_done()


__all__ = [
    "CapturePayloadSink",
    "CapturePreparation",
    "FinalizationTimeout",
    "FinalizationTimeoutError",
    "MeetingCaptureSession",
    "StandardSubtitleSession",
    "SubtitlePreparation",
    "SubtitleSessionState",
    "TranscriberFactory",
    "TranscriptionGap",
]
