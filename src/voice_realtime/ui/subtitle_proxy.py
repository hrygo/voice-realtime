"""WhisperLiveKit 字幕代理：可重连 PCM 上行、快照广播与 SRT 落盘。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from voice_realtime.config import SubtitleSettings
from voice_realtime.subtitles.events import SubtitleEvent, SubtitleStream

logger = logging.getLogger(__name__)

ClientSender = Callable[[str], Awaitable[None]]
StreamFactory = Callable[..., SubtitleStream]


class SubtitleProxyState(StrEnum):
    """字幕代理对外可观察状态。"""

    STOPPED = "stopped"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    BACKOFF = "backoff"
    ERROR = "error"


@dataclass
class _ClientChannel:
    queue: asyncio.Queue[str]
    task: asyncio.Task[None]


class SubtitleProxy:
    """在一个后台 supervisor 中维持 WLK 连接并广播全量字幕快照。"""

    _CLIENT_QUEUE_SIZE = 8
    _AUDIO_QUEUE_SIZE = 512
    _DEFAULT_BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)

    def __init__(
        self,
        settings: SubtitleSettings,
        *,
        stream_factory: StreamFactory | None = None,
        backoff_delays: Sequence[float] = _DEFAULT_BACKOFF,
    ) -> None:
        if not backoff_delays or any(delay <= 0 for delay in backoff_delays):
            raise ValueError("backoff_delays 必须包含正数")
        self._settings = settings
        self._stream_factory = stream_factory or SubtitleStream
        self._backoff_delays = tuple(backoff_delays)
        self._stream: SubtitleStream | None = None
        self._clients: dict[ClientSender, _ClientChannel] = {}
        self._client_sequence = 0
        self._running = False
        self._state = SubtitleProxyState.STOPPED
        self._last_error: str | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._audio_buffer: asyncio.Queue[bytes] = asyncio.Queue(maxsize=self._AUDIO_QUEUE_SIZE)
        self._snapshot_signature: tuple[tuple[tuple[str, ...], ...], str] | None = None
        self._persisted_confirmed_signature: tuple[tuple[str, ...], ...] | None = None
        self._archived = False
        self._session_has_confirmed = False

    @property
    def state(self) -> str:
        """返回当前状态机状态。"""
        return self._state.value

    @property
    def last_error(self) -> str | None:
        """返回最近一次 WLK 连接错误，仅用于本机诊断。"""
        return self._last_error

    def add_client(self, ws_send: ClientSender) -> str:
        """注册浏览器发送端；每个客户端拥有独立有界队列。"""
        if ws_send not in self._clients:
            queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self._CLIENT_QUEUE_SIZE)
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
        if not self._clients:
            self._drain_audio_buffer()

    @property
    def has_clients(self) -> bool:
        return bool(self._clients)

    @property
    def is_paused(self) -> bool:
        return not self.has_clients

    async def start(self) -> None:
        """启动可取消的连接 supervisor；WLK 离线时在后台持续退避重连。"""
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
        self._stop_event.clear()
        self._state = SubtitleProxyState.CONNECTING
        self._supervisor_task = asyncio.create_task(self._supervise_connection())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def stop(self) -> None:
        """停止重连、关闭流和客户端 worker，并归档本次 SRT。"""
        self._running = False
        self._stop_event.set()
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor_task
            self._supervisor_task = None
        stream = self._stream
        if stream is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(stream.close(), timeout=2.0)
        self._stream = None
        self._drain_audio_buffer()
        channels = list(self._clients.values())
        self._clients.clear()
        for channel in channels:
            channel.task.cancel()
        if channels:
            await asyncio.gather(*(channel.task for channel in channels), return_exceptions=True)
        self._archive_current_srt()
        self._state = SubtitleProxyState.STOPPED
        logger.info("SubtitleProxy: 已停止")

    async def _supervise_connection(self) -> None:
        attempt = 0
        while self._running:
            self._state = SubtitleProxyState.CONNECTING
            stream: SubtitleStream | None = None
            try:
                stream = self._stream_factory(
                    url=f"ws://{self._settings.host}:{self._settings.port}",
                    language=self._settings.language,
                )
                self._stream = stream
                await stream.connect()
                if not self._running:
                    break
                self._last_error = None
                self._state = SubtitleProxyState.CONNECTED
                attempt = 0
                logger.info("SubtitleProxy: 已连接 wlk %s", stream.uri)
                await self._serve_connection(stream)
                if self._running:
                    raise ConnectionError("WLK 字幕连接已关闭")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("SubtitleProxy: WLK 连接中断，将自动重连: %s", exc)
            finally:
                if stream is not None:
                    with contextlib.suppress(Exception):
                        await stream.close()
                if self._stream is stream:
                    self._stream = None
                self._drain_audio_buffer()

            if not self._running:
                break
            delay = self._backoff_delays[min(attempt, len(self._backoff_delays) - 1)]
            attempt += 1
            self._state = SubtitleProxyState.BACKOFF
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            except TimeoutError:
                continue
            break

    async def _serve_connection(self, stream: SubtitleStream) -> None:
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

    async def _audio_send_loop(self, stream: SubtitleStream) -> None:
        while self._running and self._stream is stream:
            chunk = await self._audio_buffer.get()
            if self.has_clients:
                await stream.send_audio(chunk)

    async def _event_recv_loop(self, stream: SubtitleStream | None = None) -> None:
        active_stream = stream or self._stream
        if active_stream is None:
            return
        async for event in active_stream.events():
            await self._broadcast_event(event)

    async def _process_loop(self) -> None:
        """兼容既有测试/调用者的接收循环入口。"""
        await self._event_recv_loop()

    async def _broadcast_event(self, event: SubtitleEvent) -> None:
        """按快照而非单事件广播，避免 confirmed 历史遮蔽新 partial。"""
        await self._broadcast_payload(event.raw)

    async def _broadcast_payload(self, payload: dict[str, Any]) -> None:
        """广播发生变化的完整快照，并在 confirmed 变化时原子写入 SRT。"""
        signature = self._snapshot_key(payload)
        if signature == self._snapshot_signature:
            return
        self._snapshot_signature = signature
        self._persist_confirmed_snapshot(payload, signature[0])

        if self._clients:
            text = json.dumps(payload, ensure_ascii=False)
            for channel in tuple(self._clients.values()):
                if channel.queue.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        channel.queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    channel.queue.put_nowait(text)
            await asyncio.sleep(0)

    async def push_audio(self, data: bytes) -> None:
        """接收 s16le 音频；无浏览器订阅时立即丢弃，不产生历史积压。"""
        if (
            not self.has_clients
            or not self._running
            or self._state is not SubtitleProxyState.CONNECTED
        ):
            return
        if self._audio_buffer.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._audio_buffer.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self._audio_buffer.put_nowait(data)

    async def _push_audio_batch(self) -> None:
        """兼容性批量发送入口；只发送当前订阅期内已排队的音频。"""
        stream = self._stream
        if stream is None or not self.has_clients:
            self._drain_audio_buffer()
            return
        while True:
            try:
                data = self._audio_buffer.get_nowait()
            except asyncio.QueueEmpty:
                return
            await stream.send_audio(data)

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
                if not self._clients:
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
