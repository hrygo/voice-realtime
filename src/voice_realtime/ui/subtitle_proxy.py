"""SpeechRail 字幕代理：可重连 PCM 上行、快照广播与 SRT 落盘。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from voice_realtime.asr.adapters.speechrail_realtime import (
    ConnectionFactory,
    SpeechRailRealtimeClient,
    SpeechRailStreamingTranscriber,
)
from voice_realtime.asr.contracts import ASREvent, ASRSessionContext, StreamingTranscriber
from voice_realtime.asr.presenters import legacy_ready_payload, legacy_subtitle_payload
from voice_realtime.config import SubtitleSettings
from voice_realtime.meeting.models import PCMOwner, TranscriptWindow

logger = logging.getLogger(__name__)

ClientSender = Callable[[str], Awaitable[None]]
TranscriberFactory = Callable[[ASRSessionContext], StreamingTranscriber]
CaptureListener = Callable[[TranscriptWindow], Awaitable[None]]
GapListener = Callable[["TranscriptionGap"], Awaitable[None]]
AudioListener = Callable[[bytes], None]


class FinalizationTimeoutError(TimeoutError):
    """会议 ASR 未在时限内排空 PCM 或完成 EOF，携带最后已知窗口。"""

    code = "finalization_timeout"

    def __init__(self, last_window: TranscriptWindow | None) -> None:
        self.last_window = last_window
        super().__init__("SpeechRail finalization timed out")


FinalizationTimeout = FinalizationTimeoutError


@dataclass(frozen=True, slots=True)
class SubtitlePreparation:
    """普通字幕连接已 ready、尚未接收 PCM 的一次性凭证。"""

    generation: int


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


class SubtitleProxyState(StrEnum):
    """字幕代理对外可观察状态。"""

    STOPPED = "stopped"
    PAUSED = "paused"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    BACKOFF = "backoff"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class SubtitleProxyDiagnostics:
    """字幕代理工作负载诊断快照。"""

    workload: str
    ws_state: str
    reconnect_count: int
    last_event_age_ms: int | None
    dropped_chunks: int
    gap_count: int


@dataclass
class _ClientChannel:
    queue: asyncio.Queue[str]
    task: asyncio.Task[None]


class SubtitleProxy:
    """显式准备并激活普通字幕或会议 SpeechRail 流。"""

    _CLIENT_QUEUE_SIZE = 8
    _AUDIO_QUEUE_SIZE = 512
    _DEFAULT_BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)

    def __init__(
        self,
        settings: SubtitleSettings,
        *,
        transcriber_factory: TranscriberFactory | None = None,
        speechrail_connection_factory: ConnectionFactory | None = None,
        backoff_delays: Sequence[float] = _DEFAULT_BACKOFF,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not backoff_delays or any(delay <= 0 for delay in backoff_delays):
            raise ValueError("backoff_delays 必须包含正数")
        if transcriber_factory is not None and speechrail_connection_factory is not None:
            raise ValueError("transcriber_factory 不能与 SpeechRail 连接工厂同时提供")
        self._settings = settings
        self._profile = settings.asr_profile
        self._transcriber_factory = transcriber_factory or self._build_speechrail_transcriber(
            speechrail_connection_factory
        )
        self._backoff_delays = tuple(backoff_delays)
        self._clock = clock
        self._browser_stream: StreamingTranscriber | None = None
        self._capture_stream: StreamingTranscriber | None = None
        self._clients: dict[ClientSender, _ClientChannel] = {}
        self._client_sequence = 0
        self._running = False
        self._state = SubtitleProxyState.STOPPED
        self._last_error: str | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._subtitle_epoch = 0
        self._subtitle_epoch_open = False
        self._browser_prepared: SubtitlePreparation | None = None
        self._browser_capture_active = False
        self._browser_ready = asyncio.Event()
        self._browser_active = asyncio.Event()
        self._audio_buffer: asyncio.Queue[bytes] = asyncio.Queue(maxsize=self._AUDIO_QUEUE_SIZE)
        self._last_payload: dict[str, Any] | None = None
        self._snapshot_signature: tuple[tuple[tuple[str, ...], ...], str] | None = None
        self._persisted_confirmed_signature: tuple[tuple[str, ...], ...] | None = None
        self._archived = False
        self._session_has_confirmed = False
        self._capture_owner: str | None = None
        self._capture_generation = 0
        self._capture_prepared: CapturePreparation | None = None
        self._capture_epoch = 0
        self._capture_offset_ms = 0
        self._capture_speaker_count_hint: int | None = None
        self._capture_audio_ms = 0
        self._capture_input_ms = 0
        self._capture_accept_audio = False
        self._capture_active = asyncio.Event()
        self._capture_ready = asyncio.Event()
        self._capture_stream_available = asyncio.Event()
        self._capture_ready_to_stop = asyncio.Event()
        self._capture_event_task: asyncio.Task[None] | None = None
        self._capture_send_task: asyncio.Task[None] | None = None
        self._capture_last_window: TranscriptWindow | None = None
        self._event_listeners: list[CaptureListener] = []
        self._gap_listeners: list[GapListener] = []
        self._audio_listeners: list[AudioListener] = []
        self._last_event_at: float | None = None
        self._reconnect_count = 0
        self._dropped_chunks = 0
        self._gap_count = 0

    def _build_speechrail_transcriber(
        self, connection_factory: ConnectionFactory | None
    ) -> TranscriberFactory:
        def create(context: ASRSessionContext) -> StreamingTranscriber:
            return SpeechRailStreamingTranscriber(
                client=SpeechRailRealtimeClient(
                    url=self._profile.url,
                    connection_factory=connection_factory,
                ),
                context=context,
                language=self._profile.language,
                finish_timeout_secs=self._profile.final_timeout_secs,
            )

        return create

    def _create_transcriber(self, context: ASRSessionContext) -> StreamingTranscriber:
        return self._transcriber_factory(context)

    @property
    def state(self) -> str:
        """返回当前状态机状态。"""
        return self._state.value

    @property
    def last_error(self) -> str | None:
        """返回最近一次 SpeechRail 连接错误，仅用于本机诊断。"""
        return self._last_error

    @property
    def capture_owner(self) -> str | None:
        """返回当前会议采集租约持有者；浏览器订阅不影响该租约。"""
        return self._capture_owner

    @property
    def browser_capture_active(self) -> bool:
        """普通字幕是否已提交并可接收 PCM。"""
        return self._browser_capture_active

    @property
    def subtitle_epoch(self) -> int:
        return self._subtitle_epoch

    @property
    def capture_epoch(self) -> int:
        return self._capture_epoch

    def add_event_listener(self, listener: CaptureListener) -> None:
        if listener not in self._event_listeners:
            self._event_listeners.append(listener)

    def remove_event_listener(self, listener: CaptureListener) -> None:
        with contextlib.suppress(ValueError):
            self._event_listeners.remove(listener)

    def add_gap_listener(self, listener: GapListener) -> None:
        if listener not in self._gap_listeners:
            self._gap_listeners.append(listener)

    def remove_gap_listener(self, listener: GapListener) -> None:
        with contextlib.suppress(ValueError):
            self._gap_listeners.remove(listener)

    def add_audio_listener(self, listener: AudioListener) -> None:
        if listener not in self._audio_listeners:
            self._audio_listeners.append(listener)

    def remove_audio_listener(self, listener: AudioListener) -> None:
        with contextlib.suppress(ValueError):
            self._audio_listeners.remove(listener)

    def add_client(self, ws_send: ClientSender) -> str:
        """注册浏览器发送端；每个客户端拥有独立有界队列并立即接收当前最新快照。"""
        if ws_send not in self._clients:
            queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self._CLIENT_QUEUE_SIZE)
            if self._last_payload is not None:
                queue.put_nowait(json.dumps(self._last_payload, ensure_ascii=False))
            task = asyncio.create_task(self._client_send_loop(ws_send, queue))
            task.add_done_callback(self._consume_client_task_result)
            self._clients[ws_send] = _ClientChannel(queue=queue, task=task)
        self._client_sequence += 1
        logger.info("SubtitleProxy: 新浏览器订阅 (共 %d 个)", len(self._clients))
        return f"client_{self._client_sequence}"

    def remove_client(self, ws_send: ClientSender) -> None:
        """移除浏览器发送端，并在最后一个客户端离开时丢弃待发音频。"""
        channel = self._clients.pop(ws_send, None)
        if channel is not None:
            channel.task.cancel()
            logger.info("SubtitleProxy: 浏览器取消订阅 (剩余 %d 个)", len(self._clients))
        if (
            not self._clients
            and not self._browser_capture_active
            and self._capture_owner is None
        ):
            self._drain_audio_buffer()

    @property
    def has_clients(self) -> bool:
        return bool(self._clients)

    @property
    def is_paused(self) -> bool:
        return not self._browser_capture_active and not self._capture_accept_audio

    def diagnostics(self, expected_owner: PCMOwner) -> SubtitleProxyDiagnostics:
        """返回不以静音时长推断降级的原始工作负载诊断。"""
        active_owner = expected_owner in {PCMOwner.SUBTITLES, PCMOwner.MEETING}
        if not active_owner:
            workload = "paused"
            ws_state = "paused"
        else:
            ws_state = self._diagnostic_ws_state(expected_owner)
            committed = (
                self._browser_capture_active
                if expected_owner is PCMOwner.SUBTITLES
                else self._capture_accept_audio
            )
            ready = self._diagnostic_ready(expected_owner)
            if self._state is SubtitleProxyState.ERROR:
                workload = "error"
            elif self._state is SubtitleProxyState.BACKOFF or (committed and not ready):
                workload = "degraded"
            elif ready:
                workload = "ready"
            else:
                workload = "starting"

        last_event_age_ms = None
        if active_owner and self._last_event_at is not None:
            elapsed = max(0.0, self._clock() - self._last_event_at)
            last_event_age_ms = int(elapsed * 1000)
        return SubtitleProxyDiagnostics(
            workload=workload,
            ws_state=ws_state,
            reconnect_count=self._reconnect_count,
            last_event_age_ms=last_event_age_ms,
            dropped_chunks=self._dropped_chunks,
            gap_count=self._gap_count,
        )

    def _diagnostic_ready(self, expected_owner: PCMOwner) -> bool:
        if expected_owner is PCMOwner.SUBTITLES:
            return (
                self._browser_stream is not None
                and self._browser_ready.is_set()
                and self._state is SubtitleProxyState.CONNECTED
            )
        return (
            self._capture_stream is not None
            and self._capture_ready.is_set()
            and self._capture_stream_available.is_set()
            and self._state is SubtitleProxyState.CONNECTED
        )

    def _diagnostic_ws_state(self, expected_owner: PCMOwner) -> str:
        if self._state is SubtitleProxyState.STOPPED:
            return "paused"
        if self._state is SubtitleProxyState.CONNECTED:
            return "meeting" if expected_owner is PCMOwner.MEETING else "connected"
        return self._state.value

    async def start(self) -> None:
        """初始化代理；不建立 SpeechRail 连接。"""
        if self._running:
            return
        try:
            self._settings.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._state = SubtitleProxyState.ERROR
            raise
        self._running = True
        self._archived = False
        self._session_has_confirmed = False
        self._snapshot_signature = None
        self._persisted_confirmed_signature = None
        self._last_error = None
        self._last_event_at = None
        self._stop_event.clear()
        self._browser_ready.clear()
        self._browser_active.clear()
        self._capture_ready.clear()
        self._capture_active.clear()
        self._state = SubtitleProxyState.PAUSED

    async def stop(self) -> None:
        """停止重连、关闭流和客户端 worker，并归档本次 SRT。"""
        self._running = False
        self._stop_event.set()
        if self._capture_owner is not None:
            await self._close_capture()
        await self._close_browser_connection()
        channels = list(self._clients.values())
        self._clients.clear()
        for channel in channels:
            channel.task.cancel()
        if channels:
            await asyncio.gather(*(channel.task for channel in channels), return_exceptions=True)
        self._state = SubtitleProxyState.STOPPED
        logger.info("SubtitleProxy: 已停止")

    async def prepare_browser_capture(
        self, *, timeout_secs: float
    ) -> SubtitlePreparation:
        """建立普通字幕流并等待 ready，但不接收 PCM。"""
        if timeout_secs <= 0:
            raise ValueError("timeout_secs 必须大于 0")
        if not self._running:
            await self.start()
        if self._capture_owner is not None:
            raise RuntimeError("会议采集租约存在，无法准备普通字幕")
        if self._browser_capture_active or self._browser_prepared is not None:
            raise RuntimeError("普通字幕已活动或正在准备")

        epoch = await self._open_subtitle_epoch()
        preparation = SubtitlePreparation(generation=epoch)
        self._browser_prepared = preparation
        self._browser_ready.clear()
        self._browser_active.clear()
        self._drain_audio_buffer()
        self._state = SubtitleProxyState.CONNECTING
        stream = self._create_transcriber(
            ASRSessionContext(source_epoch=epoch, offset_ms=0, purpose="subtitles")
        )
        self._browser_stream = stream
        try:
            async with asyncio.timeout(timeout_secs):
                await stream.connect()
                if not self._running or self._browser_prepared is not preparation:
                    raise RuntimeError("普通字幕 preparation 已取消")
                self._last_error = None
                self._state = SubtitleProxyState.CONNECTED
                self._supervisor_task = asyncio.create_task(
                    self._supervise_connection(stream)
                )
                await self._browser_ready.wait()
        except TimeoutError as exc:
            await self._close_browser_connection()
            raise RuntimeError("SpeechRail 未完成 transcription session 初始化") from exc
        except BaseException:
            await self._close_browser_connection()
            raise
        return preparation

    def commit_browser_capture(self, preparation: SubtitlePreparation) -> None:
        """同步提升 ready 的普通字幕 preparation。"""
        if self._browser_prepared is not preparation:
            raise RuntimeError("无效或已消费的 browser preparation")
        task = self._supervisor_task
        if (
            not self._running
            or self._browser_stream is None
            or not self._browser_ready.is_set()
            or task is None
            or task.done()
        ):
            raise RuntimeError("browser preparation 未 ready")
        if self._capture_owner is not None:
            raise RuntimeError("会议采集租约仍存在")
        self._browser_prepared = None
        self._browser_capture_active = True
        self._browser_active.set()
        self._state = SubtitleProxyState.CONNECTED

    async def abort_browser_capture(self, preparation: SubtitlePreparation) -> None:
        """消费并关闭尚未提交的普通字幕 preparation。"""
        if self._browser_prepared is not preparation:
            raise RuntimeError("无效或已消费的 browser preparation")
        await self._close_browser_connection()

    async def deactivate_browser_capture(self) -> None:
        """停用普通字幕，关闭任务与流并清空待发 PCM。"""
        await self._close_browser_connection()

    async def prepare_capture(
        self,
        owner: str,
        *,
        timeout_secs: float,
        speaker_count_hint: int | None = None,
    ) -> CapturePreparation:
        """建立会议流并等待 ready，但不接收 PCM。"""
        owner = owner.strip()
        if not owner:
            raise ValueError("capture owner 不能为空")
        if timeout_secs <= 0:
            raise ValueError("timeout_secs 必须大于 0")
        if speaker_count_hint is not None and not 1 <= speaker_count_hint <= 8:
            raise ValueError("speaker_count_hint 必须在 1 到 8 之间")
        if self._capture_owner is not None:
            raise RuntimeError("已有会议采集租约")
        if not self._running:
            await self.start()

        self._snapshot_signature = None
        self._last_event_at = None
        self._capture_generation += 1
        preparation = CapturePreparation(
            owner=owner,
            generation=self._capture_generation,
        )
        self._capture_prepared = preparation
        self._capture_epoch += 1
        self._capture_offset_ms = 0
        self._capture_speaker_count_hint = speaker_count_hint
        self._capture_audio_ms = 0
        self._capture_input_ms = 0
        self._capture_owner = owner
        self._capture_accept_audio = False
        self._capture_active.clear()
        self._capture_ready.clear()
        self._capture_stream_available.clear()
        self._capture_ready_to_stop.clear()
        self._capture_last_window = None
        self._state = SubtitleProxyState.CONNECTING

        try:
            stream = self._create_transcriber(
                ASRSessionContext(
                    source_epoch=self._capture_epoch,
                    offset_ms=self._capture_offset_ms,
                    purpose="meeting",
                    speaker_count_hint=self._capture_speaker_count_hint,
                ),
            )
            self._capture_stream = stream
            async with asyncio.timeout(timeout_secs):
                await stream.connect()
                self._state = SubtitleProxyState.CONNECTED
                self._capture_stream_available.set()
                self._capture_event_task = asyncio.create_task(
                    self._capture_event_loop(stream)
                )
                self._capture_send_task = asyncio.create_task(
                    self._capture_send_loop(stream)
                )
                await self._capture_ready.wait()
        except TimeoutError as exc:
            await self._close_capture()
            raise RuntimeError("SpeechRail 未发送 ready event") from exc
        except BaseException:
            await self._close_capture()
            raise
        return preparation

    def commit_capture(self, preparation: CapturePreparation) -> None:
        """同步提升 ready 的会议 preparation。"""
        if self._capture_prepared is not preparation:
            raise RuntimeError("无效或已消费的 capture preparation")
        if (
            self._capture_stream is None
            or not self._capture_ready.is_set()
            or self._capture_event_task is None
            or self._capture_event_task.done()
            or self._capture_send_task is None
            or self._capture_send_task.done()
        ):
            raise RuntimeError("capture preparation 未 ready")
        if self._browser_capture_active:
            raise RuntimeError("普通字幕仍处于活动状态")
        self._capture_prepared = None
        self._capture_accept_audio = True
        self._capture_active.set()
        self._state = SubtitleProxyState.CONNECTED

    async def abort_prepared_capture(self, preparation: CapturePreparation) -> None:
        """消费并关闭尚未提交的会议 preparation。"""
        if self._capture_prepared is not preparation:
            raise RuntimeError("无效或已消费的 capture preparation")
        await self._close_capture()

    async def finish_capture(self, *, timeout_secs: float) -> TranscriptWindow:
        """发送空 PCM EOF，等待最终快照和 ready_to_stop，再关闭 epoch。"""
        stream = self._capture_stream
        if (
            self._capture_owner is None
            or stream is None
            or not self._capture_accept_audio
        ):
            raise RuntimeError("没有活动的会议采集租约")
        if timeout_secs <= 0:
            raise ValueError("timeout_secs 必须大于 0")
        loop = asyncio.get_running_loop()
        start_time = loop.time()
        deadline = start_time + timeout_secs
        logger.info("开始会议 ASR 优雅停机冲刷 (EOF)... 租约: %s", self._capture_owner)
        self._capture_accept_audio = False
        try:
            async with asyncio.timeout_at(deadline):
                await self._audio_buffer.join()
                self._capture_active.clear()
                final_window = await stream.finish()
                self._capture_last_window = final_window
            elapsed_ms = (loop.time() - start_time) * 1000
            logger.info("会议 ASR 优雅冲刷完成，耗时 %.1f ms", elapsed_ms)
        except TimeoutError as exc:
            elapsed_ms = (loop.time() - start_time) * 1000
            logger.warning(
                "会议 ASR 优雅冲刷超时 (%.1f ms > %.1fs)，执行强制截断封存",
                elapsed_ms,
                timeout_secs,
            )
            last_window = self._capture_last_window
            await self._close_capture()
            raise FinalizationTimeoutError(last_window) from exc
        except BaseException:
            await self._close_capture()
            raise
        result = self._capture_last_window or TranscriptWindow(source_epoch=self._capture_epoch)
        await self._close_capture()
        return result

    async def abort_capture(self) -> None:
        """取消会议采集，不发送 EOF；已确认窗口仍由上层保留。"""
        self._capture_accept_audio = False
        self._capture_active.clear()
        await self._close_capture()

    async def _open_subtitle_epoch(self) -> int:
        """关闭旧边界并建立一个不复用时间轴的新普通字幕 epoch。"""
        await self._close_subtitle_epoch()
        self._subtitle_epoch += 1
        self._subtitle_epoch_open = True
        self._archived = False
        self._last_payload = None
        self._snapshot_signature = None
        self._persisted_confirmed_signature = None
        self._session_has_confirmed = False
        self._browser_ready.clear()
        self._last_event_at = None
        return self._subtitle_epoch

    async def _close_subtitle_epoch(self) -> None:
        """归档并原子清空当前普通字幕 epoch，随后通知合法客户端。"""
        if not self._subtitle_epoch_open:
            return
        epoch = self._subtitle_epoch
        self._archive_current_srt()
        self._atomic_clear_current_srt()
        self._last_payload = None
        self._snapshot_signature = None
        self._persisted_confirmed_signature = None
        self._session_has_confirmed = False
        self._browser_ready.clear()
        self._last_event_at = None
        self._subtitle_epoch_open = False
        if epoch > 0 and self._clients:
            await self._broadcast_untracked(
                {"type": "reset", "source_epoch": epoch}
            )

    async def _close_browser_connection(self) -> None:
        self._browser_capture_active = False
        self._browser_prepared = None
        self._browser_active.clear()
        task = self._supervisor_task
        self._supervisor_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        stream = self._browser_stream
        self._browser_stream = None
        if stream is not None:
            with contextlib.suppress(Exception):
                await stream.close()
        await self._close_subtitle_epoch()
        self._drain_audio_buffer()
        if self._running:
            self._state = (
                SubtitleProxyState.CONNECTED
                if self._capture_accept_audio
                else SubtitleProxyState.PAUSED
            )

    async def _capture_event_loop(self, stream: StreamingTranscriber) -> None:
        active_stream = stream
        try:
            while self._capture_owner is not None:
                try:
                    async for event in active_stream.events():
                        await self._handle_capture_event(event)
                    if not self._capture_accept_audio:
                        return
                    next_stream = await self._reconnect_capture(active_stream)
                    if next_stream is None:
                        return
                    active_stream = next_stream
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    if not self._capture_accept_audio:
                        return
                    next_stream = await self._reconnect_capture(active_stream)
                    if next_stream is None:
                        return
                    active_stream = next_stream
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"

    async def _capture_send_loop(self, stream: StreamingTranscriber) -> None:
        del stream
        pending: bytes | None = None
        try:
            while self._capture_owner is not None:
                if pending is None:
                    await self._capture_active.wait()
                    if self._capture_owner is None:
                        return
                    await self._capture_stream_available.wait()
                    if self._capture_owner is None:
                        return
                    if self._capture_stream is None:
                        self._capture_stream_available.clear()
                        continue
                    pending = await self._audio_buffer.get()
                active_stream = self._capture_stream
                if active_stream is None:
                    self._capture_stream_available.clear()
                    await self._capture_stream_available.wait()
                    continue
                chunk = pending
                if chunk is None:
                    continue
                try:
                    await active_stream.send_audio(chunk)
                except Exception as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    if self._capture_stream is active_stream:
                        self._capture_stream = None
                    self._capture_stream_available.clear()
                    with contextlib.suppress(Exception):
                        await active_stream.close()
                    gap_start_ms = self._capture_offset_ms + self._capture_audio_ms
                    self._capture_audio_ms += len(chunk) // 32
                    await self._notify_capture_gap(
                        gap_start_ms,
                        self._capture_offset_ms + self._capture_audio_ms,
                    )
                    self._audio_buffer.task_done()
                    pending = None
                    await asyncio.sleep(0)
                    continue
                self._capture_audio_ms += len(chunk) // 32
                self._audio_buffer.task_done()
                pending = None
        finally:
            if pending is not None:
                self._audio_buffer.task_done()

    async def _notify_capture_gap(self, start_ms: int, end_ms: int) -> None:
        if end_ms <= start_ms:
            return
        self._gap_count += 1
        gap = TranscriptionGap(
            source_epoch=self._capture_epoch,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        for listener in tuple(self._gap_listeners):
            with contextlib.suppress(Exception):
                await listener(gap)

    async def _reconnect_capture(
        self, old_stream: StreamingTranscriber
    ) -> StreamingTranscriber | None:
        """建立新 ASR epoch，并显式通知无法补录的间隔。"""
        self._reconnect_count += 1
        with contextlib.suppress(Exception):
            await old_stream.close()
        if self._capture_stream is old_stream:
            self._capture_stream = None
        self._capture_stream_available.clear()
        self._capture_offset_ms += self._capture_audio_ms
        self._capture_audio_ms = 0
        self._capture_epoch += 1
        gap_start_ms = self._capture_offset_ms
        self._state = SubtitleProxyState.BACKOFF
        for delay in self._backoff_delays:
            if self._capture_owner is None or not self._running:
                return None
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                return None
            try:
                resume_offset_ms = self._capture_input_ms
                await self._notify_capture_gap(gap_start_ms, resume_offset_ms)
                self._capture_offset_ms = resume_offset_ms
                gap_start_ms = resume_offset_ms
                self._state = SubtitleProxyState.CONNECTING
                stream = self._create_transcriber(
                    ASRSessionContext(
                        source_epoch=self._capture_epoch,
                        offset_ms=self._capture_offset_ms,
                        purpose="meeting",
                        speaker_count_hint=self._capture_speaker_count_hint,
                    )
                )
                await stream.connect()
                self._capture_stream = stream
                self._state = SubtitleProxyState.CONNECTED
                self._capture_stream_available.set()
                self._capture_ready.clear()
                return stream
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._drain_audio_buffer()
                self._state = SubtitleProxyState.BACKOFF
        await self._notify_capture_gap(gap_start_ms, self._capture_input_ms)
        self._capture_offset_ms = self._capture_input_ms
        return None

    async def _handle_capture_event(self, event: ASREvent) -> None:
        self._last_event_at = self._clock()
        if event.kind == "ready":
            self._capture_ready.set()
            return
        if event.kind == "error":
            self._last_error = event.error_message
            await self._broadcast_payload(
                {"type": "error", "error": event.error_message or "ASR error"},
                persist=False,
            )
            return
        window = event.window
        if window is None:
            return
        self._capture_last_window = window
        if event.kind == "final":
            self._capture_ready_to_stop.set()
            return
        for listener in tuple(self._event_listeners):
            try:
                await listener(window)
            except Exception:
                logger.exception("SubtitleProxy: 会议转录监听器失败")
        await self._broadcast_payload(legacy_subtitle_payload(window), persist=False)

    async def _supervise_connection(
        self, initial_stream: StreamingTranscriber
    ) -> None:
        attempt = 0
        stream: StreamingTranscriber | None = initial_stream
        while self._running and (
            self._browser_prepared is not None or self._browser_capture_active
        ):
            try:
                if stream is None:
                    self._reconnect_count += 1
                    epoch = await self._open_subtitle_epoch()
                    self._state = SubtitleProxyState.CONNECTING
                    stream = self._create_transcriber(
                        ASRSessionContext(
                            source_epoch=epoch,
                            offset_ms=0,
                            purpose="subtitles",
                        )
                    )
                    self._browser_stream = stream
                    await stream.connect()
                    if not self._running or not self._browser_capture_active:
                        break
                    self._last_error = None
                    self._state = SubtitleProxyState.CONNECTED
                attempt = 0
                logger.info("SubtitleProxy: 已连接 SpeechRail %s", stream.uri)
                await self._serve_connection(stream)
                if self._running:
                    raise ConnectionError("SpeechRail 字幕连接已关闭")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("SubtitleProxy: SpeechRail 连接中断，将自动重连: %s", exc)
            finally:
                if stream is not None:
                    with contextlib.suppress(Exception):
                        await stream.close()
                if self._browser_stream is stream:
                    self._browser_stream = None
                await self._close_subtitle_epoch()
                self._drain_audio_buffer()

            if not self._running or not self._browser_capture_active:
                if self._running and not self._capture_accept_audio:
                    self._state = SubtitleProxyState.PAUSED
                break
            delay = self._backoff_delays[min(attempt, len(self._backoff_delays) - 1)]
            attempt += 1
            self._state = SubtitleProxyState.BACKOFF
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
        while self._running and self._browser_stream is stream:
            await self._browser_active.wait()
            if not self._running or self._browser_stream is not stream:
                return
            chunk = await self._audio_buffer.get()
            try:
                if self._browser_capture_active:
                    await stream.send_audio(chunk)
            finally:
                self._audio_buffer.task_done()

    async def _event_recv_loop(self, stream: StreamingTranscriber | None = None) -> None:
        active_stream = stream or self._browser_stream
        if active_stream is None:
            return
        async for event in active_stream.events():
            await self._handle_stream_event(event)

    async def _process_loop(self) -> None:
        """兼容既有测试/调用者的接收循环入口。"""
        await self._event_recv_loop()

    async def _handle_stream_event(self, event: ASREvent) -> None:
        """只消费后端无关事件，并按完整领域窗口广播。"""
        self._last_event_at = self._clock()
        if event.kind == "ready":
            self._browser_ready.set()
            await self._broadcast_payload(legacy_ready_payload())
            return
        if event.kind == "error":
            self._last_error = event.error_message
            await self._broadcast_payload(
                {"type": "error", "error": event.error_message or "ASR error"}
            )
            return
        if event.window is not None:
            await self._broadcast_payload(legacy_subtitle_payload(event.window))

    async def _broadcast_payload(
        self, payload: dict[str, Any], *, persist: bool = True
    ) -> None:
        """广播发生变化的完整快照，并在 confirmed 变化时原子写入 SRT。"""
        self._last_payload = payload
        signature = self._snapshot_key(payload)
        if signature == self._snapshot_signature:
            return
        self._snapshot_signature = signature
        if persist:
            self._persist_confirmed_snapshot(payload, signature[0])

        await self._broadcast_untracked(payload)

    async def _broadcast_untracked(self, payload: dict[str, Any]) -> None:
        """广播控制或快照 payload，但不让它成为可回放字幕状态。"""
        if not self._clients:
            return
        text = json.dumps(payload, ensure_ascii=False)
        for channel in tuple(self._clients.values()):
            if channel.queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    channel.queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                channel.queue.put_nowait(text)
        await asyncio.sleep(0)

    async def clear_subtitles(self) -> None:
        """清空当前字幕快照并重置服务端会话。"""
        empty_payload = {"lines": [], "buffer_transcription": ""}
        self._last_payload = empty_payload
        self._snapshot_signature = None
        self._persisted_confirmed_signature = None
        self._session_has_confirmed = False
        self._atomic_clear_current_srt()
        stream = self._browser_stream
        if stream is not None:
            self._browser_stream = None
            self._browser_ready.clear()
            with contextlib.suppress(Exception):
                await stream.close()
        text = json.dumps(empty_payload, ensure_ascii=False)
        for channel in tuple(self._clients.values()):
            if channel.queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    channel.queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                channel.queue.put_nowait(text)

    async def push_audio(self, data: bytes) -> None:
        """接收 s16le 音频；会议租约存在时不依赖浏览器订阅。"""
        if self._capture_accept_audio:
            for listener in list(self._audio_listeners):
                try:
                    listener(data)
                except Exception as exc:
                    logger.warning("SubtitleProxy: 音频监听器处理失败: %s", exc)
            duration_ms = len(data) // 32
            start_ms = self._capture_input_ms
            self._capture_input_ms += duration_ms
            if (
                self._capture_stream is None
                and self._state is SubtitleProxyState.BACKOFF
            ):
                return
            if self._audio_buffer.full():
                await self._notify_capture_gap(start_ms, self._capture_input_ms)
                return
            with contextlib.suppress(asyncio.QueueFull):
                self._audio_buffer.put_nowait(data)
            return
        if (
            not self._running
            or not self._browser_capture_active
            or self._browser_stream is None
            or not self._browser_ready.is_set()
        ):
            return
        if self._audio_buffer.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._audio_buffer.get_nowait()
                self._audio_buffer.task_done()
                self._dropped_chunks += 1
        with contextlib.suppress(asyncio.QueueFull):
            self._audio_buffer.put_nowait(data)

    async def _push_audio_batch(self) -> None:
        """兼容性批量发送入口；只发送当前订阅期内已排队的音频。"""
        stream = self._browser_stream
        if stream is None or not self._browser_capture_active:
            self._drain_audio_buffer()
            return
        while True:
            try:
                data = self._audio_buffer.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await stream.send_audio(data)
            finally:
                self._audio_buffer.task_done()

    async def _client_send_loop(
        self, callback: ClientSender, queue: asyncio.Queue[str]
    ) -> None:
        try:
            while True:
                await callback(await queue.get())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("SubtitleProxy: 浏览器客户端已断开", exc_info=True)
        finally:
            current = self._clients.get(callback)
            if current is not None and current.task is asyncio.current_task():
                self._clients.pop(callback, None)
                if (
                    not self._clients
                    and not self._browser_capture_active
                    and self._capture_owner is None
                ):
                    self._drain_audio_buffer()

    @staticmethod
    def _consume_client_task_result(task: asyncio.Task[None]) -> None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()

    def _drain_audio_buffer(self) -> None:
        while True:
            try:
                self._audio_buffer.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._audio_buffer.task_done()

    @staticmethod
    def _confirmed_lines(payload: dict[str, Any]) -> list[dict[str, Any]]:
        lines = payload.get("lines")
        if not isinstance(lines, list):
            return []
        return [
            line
            for line in lines
            if isinstance(line, dict)
            and str(line.get("text") or "").strip()
            and line.get("speaker") != -2
        ]

    @classmethod
    def _snapshot_key(
        cls, payload: dict[str, Any]
    ) -> tuple[tuple[tuple[str, ...], ...], str]:
        confirmed = tuple(
            (
                str(line.get("start") or ""),
                str(line.get("end") or ""),
                str(line.get("speaker") if line.get("speaker") is not None else ""),
                str(line.get("text") or ""),
            )
            for line in cls._confirmed_lines(payload)
        )
        partial = str(payload.get("buffer_transcription") or "")
        return confirmed, partial

    def _persist_confirmed_snapshot(
        self,
        payload: dict[str, Any],
        signature: tuple[tuple[str, ...], ...],
    ) -> None:
        if not signature or signature == self._persisted_confirmed_signature:
            return
        output = self._render_srt(self._confirmed_lines(payload))
        output_dir = self._settings.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        temporary = output_dir / "current.srt.tmp"
        current = output_dir / "current.srt"
        temporary.write_text(output, encoding="utf-8")
        temporary.replace(current)
        self._persisted_confirmed_signature = signature
        self._session_has_confirmed = True

    def _atomic_clear_current_srt(self) -> None:
        output_dir = self._settings.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        temporary = output_dir / "current.srt.tmp"
        current = output_dir / "current.srt"
        temporary.write_text("", encoding="utf-8")
        temporary.replace(current)

    def _archive_current_srt(self) -> None:
        if self._archived or not self._session_has_confirmed:
            return
        current = self._settings.output_dir / "current.srt"
        if not current.is_file() or not current.stat().st_size:
            return
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive = self._settings.output_dir / f"session-{timestamp}.srt"
        suffix = 2
        while archive.exists():
            archive = self._settings.output_dir / f"session-{timestamp}-{suffix}.srt"
            suffix += 1
        shutil.copy2(current, archive)
        self._archived = True

    @classmethod
    def _render_srt(cls, lines: list[dict[str, Any]]) -> str:
        blocks = []
        for index, line in enumerate(lines, start=1):
            start = cls._srt_timestamp(line.get("start"))
            end = cls._srt_timestamp(line.get("end"))
            text = str(line.get("text") or "").strip()
            blocks.append(f"{index}\n{start} --> {end}\n{text}")
        return "\n\n".join(blocks) + "\n"

    @staticmethod
    def _srt_timestamp(value: object) -> str:
        raw = str(value or "00:00:00").strip().replace(".", ",")
        clock, separator, fraction = raw.partition(",")
        parts = clock.split(":")
        if len(parts) == 3:
            hours, minutes, seconds = parts
        else:
            hours, minutes, seconds = "0", "0", "0"
        millis = (fraction if separator else "0").ljust(3, "0")[:3]
        return f"{hours.zfill(2)}:{minutes.zfill(2)}:{seconds.zfill(2)},{millis}"

    async def _close_capture(self) -> None:
        self._capture_accept_audio = False
        self._capture_prepared = None
        self._capture_active.clear()
        event_task = self._capture_event_task
        send_task = self._capture_send_task
        self._capture_event_task = None
        self._capture_send_task = None
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
        stream = self._capture_stream
        self._capture_stream = None
        self._capture_ready.clear()
        self._capture_ready_to_stop.clear()
        self._capture_stream_available.clear()
        if stream is not None:
            with contextlib.suppress(Exception):
                await stream.close()
        self._capture_owner = None
        self._drain_audio_buffer()
        if self._running:
            self._state = (
                SubtitleProxyState.CONNECTED
                if self._browser_capture_active
                else SubtitleProxyState.PAUSED
            )
