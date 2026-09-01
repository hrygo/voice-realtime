"""SpeechRail 字幕代理：可重连 PCM 上行、快照广播与 SRT 落盘。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from voice_realtime.asr.adapters.speechrail_realtime import (
    ConnectionFactory,
    SpeechRailRealtimeClient,
    SpeechRailStreamingTranscriber,
)
from voice_realtime.asr.contracts import ASRSessionContext, StreamingTranscriber
from voice_realtime.config import SubtitleSettings
from voice_realtime.meeting.asr_mapping import to_transcript_window
from voice_realtime.meeting.models import PCMOwner, TranscriptWindow
from voice_realtime.ui.subtitle_archive import SrtArchive
from voice_realtime.ui.subtitle_clients import ClientSender, SubtitleClientHub
from voice_realtime.ui.subtitle_sessions import (
    CapturePreparation,
    FinalizationTimeoutError,
    MeetingCaptureSession,
    StandardSubtitleSession,
    SubtitlePreparation,
    SubtitleSessionState,
    TranscriptionGap,
)

logger = logging.getLogger(__name__)

TranscriberFactory = Callable[[ASRSessionContext], StreamingTranscriber]
CaptureListener = Callable[[TranscriptWindow], Awaitable[None]]
GapListener = Callable[[TranscriptionGap], Awaitable[None]]
AudioListener = Callable[[bytes], None]


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


class SubtitleProxy:
    """显式准备并激活普通字幕或会议 SpeechRail 流。"""

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
        self._client_hub = SubtitleClientHub(on_channel_closed=self._on_hub_channel_closed)
        self._running = False
        self._state = SubtitleProxyState.STOPPED
        self._last_error: str | None = None
        self._stop_event = asyncio.Event()
        self._audio_buffer: asyncio.Queue[bytes] = asyncio.Queue(maxsize=self._AUDIO_QUEUE_SIZE)
        self._subtitle_session = StandardSubtitleSession(
            audio_queue=self._audio_buffer,
            transcriber_factory=self._transcriber_factory,
            backoff_delays=self._backoff_delays,
            stop_event=self._stop_event,
            running=lambda: self._running,
            capture_active=lambda: self._capture_session.accepting,
            on_payload=self._broadcast_payload,
            on_state=self._on_session_state,
            on_epoch_opened=self._on_session_epoch_opened,
            on_epoch_closed=self._on_session_epoch_closed,
            on_reconnect=self._on_session_reconnect,
            on_last_event=self._on_session_last_event,
            on_last_error=self._on_session_last_error,
            on_dropped_chunk=self._on_session_dropped_chunk,
        )
        self._capture_session = MeetingCaptureSession(
            audio_queue=self._audio_buffer,
            transcriber_factory=self._transcriber_factory,
            backoff_delays=self._backoff_delays,
            stop_event=self._stop_event,
            running=lambda: self._running,
            to_transcript_window=to_transcript_window,
            on_payload=lambda payload, persist: self._broadcast_payload(
                payload, persist=persist
            ),
            on_state=self._on_session_state,
            on_gap=self._on_session_gap,
            on_reconnect=self._on_session_reconnect,
            on_last_event=self._on_session_last_event,
            on_last_error=self._on_session_last_error,
        )
        self._last_payload: dict[str, Any] | None = None
        self._snapshot_signature: tuple[tuple[tuple[str, ...], ...], str] | None = None
        self._archive = SrtArchive(settings.output_dir)
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
                    api_key=self._settings.speechrail_api_key,
                    connection_factory=connection_factory,
                ),
                context=context,
                language=self._profile.language,
                finish_timeout_secs=self._profile.final_timeout_secs,
            )

        return create

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
        return self._capture_session.owner

    @property
    def browser_capture_active(self) -> bool:
        """普通字幕是否已提交并可接收 PCM。"""
        return self._subtitle_session.committed

    @property
    def subtitle_epoch(self) -> int:
        return self._subtitle_session.epoch

    @property
    def capture_epoch(self) -> int:
        return self._capture_session.epoch

    @property
    def last_window(self) -> TranscriptWindow | None:
        """会议采集当前已知的最后确认窗口；普通字幕路径不适用。"""
        return self._capture_session.last_window

    def add_event_listener(self, listener: CaptureListener) -> None:
        self._capture_session.add_event_listener(listener)

    def remove_event_listener(self, listener: CaptureListener) -> None:
        self._capture_session.remove_event_listener(listener)

    def add_gap_listener(self, listener: GapListener) -> None:
        self._capture_session.add_gap_listener(listener)

    def remove_gap_listener(self, listener: GapListener) -> None:
        self._capture_session.remove_gap_listener(listener)

    def add_audio_listener(self, listener: AudioListener) -> None:
        if listener not in self._audio_listeners:
            self._audio_listeners.append(listener)

    def remove_audio_listener(self, listener: AudioListener) -> None:
        with contextlib.suppress(ValueError):
            self._audio_listeners.remove(listener)

    def add_client(self, ws_send: ClientSender) -> str:
        """注册浏览器发送端；每个客户端拥有独立有界队列并立即接收当前最新快照。"""
        return self._client_hub.add(ws_send, snapshot=self._last_payload)

    def remove_client(self, ws_send: ClientSender) -> None:
        """移除浏览器发送端，并在最后一个客户端离开时丢弃待发音频。"""
        self._client_hub.remove(ws_send)
        self._on_hub_channel_closed()

    @property
    def has_clients(self) -> bool:
        return self._client_hub.has_clients

    def _on_hub_channel_closed(self) -> None:
        """最后一个浏览器客户端离开且无任何 capture 时丢弃待发 PCM。"""
        if (
            not self._client_hub.has_clients
            and not self._subtitle_session.committed
            and self._capture_session.owner is None
        ):
            self._drain_audio_buffer()

    def _on_session_state(self, state: SubtitleSessionState) -> None:
        self._state = SubtitleProxyState(state.value)

    def _on_session_epoch_opened(self) -> None:
        self._archive.reset_epoch()
        self._last_payload = None
        self._snapshot_signature = None
        self._last_event_at = None

    async def _on_session_epoch_closed(self, epoch: int) -> None:
        self._archive.close_epoch()
        self._archive.clear_current()
        self._last_payload = None
        self._snapshot_signature = None
        self._last_event_at = None
        if epoch > 0 and self._client_hub.has_clients:
            await self._broadcast_untracked({"type": "reset", "source_epoch": epoch})

    def _on_session_reconnect(self) -> None:
        self._reconnect_count += 1

    def _on_session_last_event(self) -> None:
        self._last_event_at = self._clock()

    def _on_session_last_error(self, message: str | None) -> None:
        self._last_error = message

    def _on_session_dropped_chunk(self) -> None:
        self._dropped_chunks += 1

    def _on_session_gap(self) -> None:
        self._gap_count += 1

    @property
    def is_paused(self) -> bool:
        return not self._subtitle_session.committed and not self._capture_session.accepting

    def diagnostics(self, expected_owner: PCMOwner) -> SubtitleProxyDiagnostics:
        """返回不以静音时长推断降级的原始工作负载诊断。"""
        active_owner = expected_owner in {PCMOwner.SUBTITLES, PCMOwner.MEETING}
        if not active_owner:
            workload = "paused"
            ws_state = "paused"
        else:
            ws_state = self._diagnostic_ws_state(expected_owner)
            committed = (
                self._subtitle_session.committed
                if expected_owner is PCMOwner.SUBTITLES
                else self._capture_session.accepting
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
                self._subtitle_session.has_stream
                and self._subtitle_session.ready.is_set()
                and self._state is SubtitleProxyState.CONNECTED
            )
        return (
            self._capture_session.has_stream
            and self._capture_session.ready.is_set()
            and self._capture_session.stream_available.is_set()
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
        self._snapshot_signature = None
        self._archive.reset_epoch()
        self._last_error = None
        self._last_event_at = None
        self._stop_event.clear()
        self._subtitle_session.reset_flags()
        self._state = SubtitleProxyState.PAUSED

    async def stop(self) -> None:
        """停止重连、关闭流和客户端 worker，并归档本次 SRT。"""
        self._running = False
        self._stop_event.set()
        if self._capture_session.owner is not None:
            await self._close_capture()
        await self._close_browser_connection()
        await self._client_hub.close()
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
        if self._capture_session.owner is not None:
            raise RuntimeError("会议采集租约存在，无法准备普通字幕")
        return await self._subtitle_session.prepare(timeout_secs=timeout_secs)

    def commit_browser_capture(self, preparation: SubtitlePreparation) -> None:
        """同步提升 ready 的普通字幕 preparation。"""
        if self._capture_session.owner is not None:
            raise RuntimeError("会议采集租约仍存在")
        self._subtitle_session.commit(preparation)

    async def abort_browser_capture(self, preparation: SubtitlePreparation) -> None:
        """消费并关闭尚未提交的普通字幕 preparation。"""
        await self._subtitle_session.abort_prepared(preparation)

    async def deactivate_browser_capture(self) -> None:
        """停用普通字幕，关闭任务与流并清空待发 PCM。"""
        await self._subtitle_session.close_stream()

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
        if self._capture_session.owner is not None:
            raise RuntimeError("已有会议采集租约")
        if not self._running:
            await self.start()

        self._snapshot_signature = None
        self._last_event_at = None
        self._state = SubtitleProxyState.CONNECTING
        try:
            preparation = await self._capture_session.prepare(
                owner,
                timeout_secs=timeout_secs,
                speaker_count_hint=speaker_count_hint,
            )
        except TimeoutError as exc:
            await self._close_capture()
            raise RuntimeError("SpeechRail 未发送 ready event") from exc
        except BaseException:
            await self._close_capture()
            raise
        return preparation

    def commit_capture(self, preparation: CapturePreparation) -> None:
        """同步提升 ready 的会议 preparation。"""
        if self._subtitle_session.committed:
            raise RuntimeError("普通字幕仍处于活动状态")
        self._capture_session.commit(preparation)
        self._state = SubtitleProxyState.CONNECTED

    async def abort_prepared_capture(self, preparation: CapturePreparation) -> None:
        """消费并关闭尚未提交的会议 preparation。"""
        await self._capture_session.abort_prepared(preparation)
        await self._close_capture()

    async def finish_capture(self, *, timeout_secs: float) -> TranscriptWindow:
        """发送空 PCM EOF，等待最终快照和 ready_to_stop，再关闭 epoch。"""
        if (
            self._capture_session.owner is None
            or not self._capture_session.accepting
        ):
            raise RuntimeError("没有活动的会议采集租约")
        if timeout_secs <= 0:
            raise ValueError("timeout_secs 必须大于 0")
        loop = asyncio.get_running_loop()
        start_time = loop.time()
        logger.info("开始会议 ASR 优雅停机冲刷 (EOF)... 租约: %s", self._capture_session.owner)
        try:
            result = await self._capture_session.finish(timeout_secs=timeout_secs)
            elapsed_ms = (loop.time() - start_time) * 1000
            logger.info("会议 ASR 优雅冲刷完成，耗时 %.1f ms", elapsed_ms)
        except FinalizationTimeoutError as exc:
            elapsed_ms = (loop.time() - start_time) * 1000
            logger.warning(
                "会议 ASR 优雅冲刷超时 (%.1f ms > %.1fs)，执行强制截断封存",
                elapsed_ms,
                timeout_secs,
            )
            last_window = exc.last_window
            await self._close_capture()
            raise FinalizationTimeoutError(last_window) from exc
        except BaseException:
            await self._close_capture()
            raise
        await self._close_capture()
        return result

    async def abort_capture(self) -> None:
        """取消会议采集，不发送 EOF；已确认窗口仍由上层保留。"""
        await self._capture_session.abort()
        await self._close_capture()

    async def _close_browser_connection(self) -> None:
        await self._subtitle_session.close_stream()
        if self._running:
            self._state = (
                SubtitleProxyState.CONNECTED
                if self._capture_session.accepting
                else SubtitleProxyState.PAUSED
            )

    async def _process_loop(self) -> None:
        """兼容既有测试/调用者的接收循环入口。"""
        await self._subtitle_session.process_loop()

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
            self._archive.persist_confirmed(payload)

        await self._broadcast_untracked(payload)

    async def _broadcast_untracked(self, payload: dict[str, Any]) -> None:
        """广播控制或快照 payload，但不让它成为可回放字幕状态。"""
        await self._client_hub.publish(payload)

    async def clear_subtitles(self) -> None:
        """清空当前字幕快照并重置服务端会话。"""
        empty_payload = {"lines": [], "buffer_transcription": ""}
        self._last_payload = empty_payload
        self._snapshot_signature = None
        self._archive.clear_current()
        await self._subtitle_session.reset_stream()
        await self._client_hub.publish(empty_payload)

    async def push_audio(self, data: bytes) -> None:
        """接收 s16le 音频；会议租约存在时不依赖浏览器订阅。"""
        if self._capture_session.accepting:
            for listener in list(self._audio_listeners):
                try:
                    listener(data)
                except Exception as exc:
                    logger.warning("SubtitleProxy: 音频监听器处理失败: %s", exc)
            await self._capture_session.push_audio(data)
            return
        await self._subtitle_session.push_audio(data)

    async def _push_audio_batch(self) -> None:
        """兼容性批量发送入口；只发送当前订阅期内已排队的音频。"""
        await self._subtitle_session.push_audio_batch()

    def _drain_audio_buffer(self) -> None:
        while True:
            try:
                self._audio_buffer.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._audio_buffer.task_done()

    def _snapshot_key(
        self, payload: dict[str, Any]
    ) -> tuple[tuple[tuple[str, ...], ...], str]:
        confirmed = SrtArchive.confirmed_signature(payload)
        partial = str(payload.get("buffer_transcription") or "")
        return confirmed, partial

    async def _close_capture(self) -> None:
        await self._capture_session.close()
        self._drain_audio_buffer()
        if self._running:
            self._state = (
                SubtitleProxyState.CONNECTED
                if self._subtitle_session.committed
                else SubtitleProxyState.PAUSED
            )
