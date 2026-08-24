"""Fun-ASR-Nano 官方实时 WebSocket 协议适配器。"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Protocol, cast
from uuid import NAMESPACE_URL, uuid5

import websockets
from websockets.exceptions import ConnectionClosed

from voice_realtime.asr.contracts import ASRCapabilities, ASREvent, ASRSessionContext
from voice_realtime.meeting.models import NormalizedSegment, TranscriptWindow


class FunASRNanoWSConnection(Protocol):
    """适配器所需的最小 WebSocket 连接端口，便于离线协议测试。"""

    @property
    def uri(self) -> str: ...

    async def send(self, payload: str | bytes) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


FunASRNanoWSConnectFactory = Callable[[str], Awaitable[FunASRNanoWSConnection]]
FunASRNanoWSRawEventSink = Callable[[Mapping[str, object]], None]
_MAX_TRANSCRIPT_CHARS = 100_000
_MAX_SENTENCES = 10_000


def _connect(url: str) -> Awaitable[FunASRNanoWSConnection]:
    """创建默认 WebSocket 连接。"""
    return cast(Awaitable[FunASRNanoWSConnection], websockets.connect(url))


def _safe_message(value: object, default: str) -> str:
    """生成不含异常堆栈且长度受限的稳定错误消息。"""
    message = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return (message or default)[:1000]


def _timestamp(value: object) -> int | None:
    """将官方毫秒时间戳转换为整数；非法值严格拒绝。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return round(number)


def _speaker_key(source_epoch: int, raw_speaker: object) -> str:
    """在不宣称说话人能力的前提下保留服务端提供的标签。"""
    value = str(raw_speaker if raw_speaker is not None else "0").strip() or "0"
    return f"epoch:{source_epoch}:speaker:{value}"


def _transcript_text(value: object, field_name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if len(value) > _MAX_TRANSCRIPT_CHARS:
        raise ValueError(f"{field_name} exceeds the transcript size limit")
    return value.strip()


class FunASRNanoWSAdapter:
    """封装 Fun-ASR-Nano 实时 WS，并输出统一 ASR 领域事件。

    官方协议顺序为 ``START``、可选 ``LANGUAGE:<lang>`` 与
    ``HOTWORDS:<comma-separated-words>``，之后发送二进制 PCM；``STOP``
    触发一个 ``is_final=true`` 结果。服务端句段时间是 VAD 边界，因而能力
    集合不会把它们声明为会议可依赖的 segment timestamps。
    """

    backend_id = "funasr-nano-ws"
    _languages = frozenset(
        {
            "Chinese",
            "中文",
            "English",
            "英文",
            "Japanese",
            "日文",
            "日本語",
            "zh",
            "en",
            "ja",
        }
    )

    def __init__(
        self,
        *,
        url: str,
        language: str,
        context: ASRSessionContext,
        hotwords: Sequence[str] = (),
        connect_factory: FunASRNanoWSConnectFactory | None = None,
        raw_event_sink: FunASRNanoWSRawEventSink | None = None,
        handshake_timeout_secs: float = 5.0,
        finish_timeout_secs: float = 5.0,
    ) -> None:
        normalized_url = url.strip().rstrip("/")
        normalized_language = language.strip()
        if not normalized_url:
            raise ValueError("url 不能为空")
        if not normalized_language:
            raise ValueError("language 不能为空")
        if handshake_timeout_secs <= 0 or finish_timeout_secs <= 0:
            raise ValueError("timeout 必须大于 0")

        self._url = normalized_url
        self._language = normalized_language
        self._context = context
        self._hotwords = tuple(word.strip() for word in hotwords if word.strip())
        self._connect_factory = connect_factory or _connect
        self._raw_event_sink = raw_event_sink
        self._handshake_timeout_secs = handshake_timeout_secs
        self._finish_timeout_secs = finish_timeout_secs
        self._ws: FunASRNanoWSConnection | None = None
        self._connected = False
        self._ready_pending = False
        self._stop_sent = False
        self._events_active = False
        self._last_window: TranscriptWindow | None = None
        self._terminal_error: tuple[str, str] | None = None
        self._final_ready = asyncio.Event()
        self._finish_lock = asyncio.Lock()
        self.capabilities = ASRCapabilities(
            languages=self._languages | {normalized_language},
            supports_partial=True,
            # 官方 start/end 是 VAD 句段边界，并非稳定的后端时间戳契约。
            supports_segment_timestamps=False,
            supports_word_timestamps=False,
            supports_hotwords=True,
            supports_speaker_labels=False,
            supports_native_diarization=False,
            supports_eof_flush=True,
        )

    @property
    def uri(self) -> str:
        """返回当前 Fun-ASR-Nano WebSocket 地址。"""
        if self._ws is not None:
            return self._ws.uri
        return self._url

    async def connect(self) -> None:
        """建立连接并完成官方 START/LANGUAGE/HOTWORDS 握手。"""
        if self._connected:
            return

        self._reset_session_state()
        try:
            self._ws = await asyncio.wait_for(
                self._connect_factory(self._url), timeout=self._handshake_timeout_secs
            )
            await self._send_command("START")
            await self._wait_for_ack("started")
            await self._send_command(f"LANGUAGE:{self._language}")
            await self._wait_for_ack("language_set")
            if self._hotwords:
                await self._send_command(f"HOTWORDS:{','.join(self._hotwords)}")
                await self._wait_for_ack("hotwords_set")
        except TimeoutError:
            await self._close_connection()
            raise RuntimeError("FUNASR_HANDSHAKE_TIMEOUT: handshake timed out") from None
        except RuntimeError:
            await self._close_connection()
            raise
        except (ConnectionClosed, ConnectionError, OSError):
            await self._close_connection()
            raise RuntimeError("FUNASR_WS_DISCONNECTED: WebSocket disconnected") from None
        except Exception:
            await self._close_connection()
            raise RuntimeError("FUNASR_CONNECT_ERROR: unable to connect") from None

        self._connected = True
        self._ready_pending = True

    async def send_audio(self, chunk: bytes) -> None:
        """发送一帧二进制 PCM。"""
        if not self._connected or self._ws is None:
            raise RuntimeError("FUNASR_NOT_CONNECTED: adapter is not connected")
        if not isinstance(chunk, bytes):
            raise TypeError("Fun-ASR-Nano PCM chunk 必须是 bytes")
        try:
            await self._ws.send(chunk)
        except (ConnectionClosed, ConnectionError, OSError):
            raise RuntimeError("FUNASR_WS_DISCONNECTED: WebSocket disconnected") from None

    def normalize_result(self, payload: Mapping[str, object]) -> TranscriptWindow:
        """把官方 result 转换为领域窗口；非法时间戳的句段会被丢弃。"""
        window, _ = self._normalize_result(payload)
        return window

    async def events(self) -> AsyncIterator[ASREvent]:
        """持续读取并规范化服务端事件。"""
        if not self._connected or self._ws is None:
            raise RuntimeError("FUNASR_NOT_CONNECTED: adapter is not connected")
        if self._events_active:
            raise RuntimeError("FUNASR_EVENTS_ALREADY_CONSUMED: only one event reader is allowed")

        self._events_active = True
        try:
            if self._ready_pending:
                self._ready_pending = False
                yield ASREvent(kind="ready")

            while self._ws is not None:
                try:
                    raw = await self._ws.recv()
                except asyncio.CancelledError:
                    raise
                except (ConnectionClosed, ConnectionError, EOFError, OSError):
                    error = self._set_terminal_error(
                        "FUNASR_WS_DISCONNECTED", "Fun-ASR-Nano WebSocket disconnected"
                    )
                    yield error
                    return
                except Exception:
                    error = self._set_terminal_error(
                        "FUNASR_WS_ERROR", "Fun-ASR-Nano WebSocket receive failed"
                    )
                    yield error
                    return

                if isinstance(raw, bytes):
                    error = self._set_terminal_error(
                        "FUNASR_WS_PROTOCOL_ERROR", "Fun-ASR-Nano returned unexpected binary event"
                    )
                    yield error
                    return

                try:
                    payload = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    error = self._set_terminal_error(
                        "FUNASR_WS_PROTOCOL_ERROR", "Fun-ASR-Nano returned invalid JSON"
                    )
                    yield error
                    return
                if not isinstance(payload, Mapping):
                    error = self._set_terminal_error(
                        "FUNASR_WS_PROTOCOL_ERROR", "Fun-ASR-Nano returned a non-object event"
                    )
                    yield error
                    return
                normalized_payload = dict(payload)
                self._audit(normalized_payload)

                event_name = str(normalized_payload.get("event") or "").strip().lower()
                if event_name == "error" or "error" in normalized_payload:
                    error = self._set_terminal_error(
                        "FUNASR_WS_ERROR",
                        _safe_message(
                            normalized_payload.get("error"), "Fun-ASR-Nano service error"
                        ),
                    )
                    yield error
                    return
                if event_name in {"started", "language_set", "hotwords_set", "stopped"}:
                    continue

                if not self._looks_like_result(normalized_payload):
                    continue
                try:
                    window, timestamp_error = self._normalize_result(normalized_payload)
                except ValueError:
                    error = self._set_terminal_error(
                        "FUNASR_WS_PROTOCOL_ERROR",
                        "Fun-ASR-Nano result fields are invalid",
                    )
                    yield error
                    return
                self._last_window = window
                if timestamp_error:
                    yield ASREvent(
                        kind="error",
                        error_code="FUNASR_INVALID_TIMESTAMPS",
                        error_message=timestamp_error,
                    )

                is_final = normalized_payload.get("is_final") is True
                if is_final:
                    self._final_ready.set()
                    yield ASREvent(
                        kind="final",
                        window=window,
                        metadata={"backend_id": self.backend_id, "is_final": True},
                    )
                    return
                yield ASREvent(
                    kind="snapshot",
                    window=window,
                    metadata={"backend_id": self.backend_id, "is_final": False},
                )
        finally:
            self._events_active = False

    async def finish(self) -> TranscriptWindow:
        """仅发送一次 STOP，并等待官方 ``is_final=true`` 结果。"""
        async with self._finish_lock:
            if not self._connected or self._ws is None:
                raise RuntimeError("FUNASR_NOT_CONNECTED: adapter is not connected")
            if not self._stop_sent:
                try:
                    await self._send_command("STOP")
                except (ConnectionClosed, ConnectionError, OSError):
                    raise RuntimeError("FUNASR_WS_DISCONNECTED: WebSocket disconnected") from None
                self._stop_sent = True

        try:
            await asyncio.wait_for(self._final_ready.wait(), timeout=self._finish_timeout_secs)
        except TimeoutError:
            raise TimeoutError("FUNASR_FINAL_TIMEOUT: final result was not received") from None

        if self._terminal_error is not None:
            code, message = self._terminal_error
            raise RuntimeError(f"{code}: {message}")
        return self._last_window or TranscriptWindow(source_epoch=self._context.source_epoch)

    async def close(self) -> None:
        """关闭连接；重复调用安全。"""
        await self._close_connection()

    def _reset_session_state(self) -> None:
        self._connected = False
        self._ready_pending = False
        self._stop_sent = False
        self._last_window = None
        self._terminal_error = None
        self._final_ready.clear()

    async def _send_command(self, command: str) -> None:
        if self._ws is None:
            raise RuntimeError("FUNASR_NOT_CONNECTED: adapter is not connected")
        await asyncio.wait_for(self._ws.send(command), timeout=self._handshake_timeout_secs)

    async def _wait_for_ack(self, expected_event: str) -> None:
        if self._ws is None:
            raise RuntimeError("FUNASR_NOT_CONNECTED: adapter is not connected")
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=self._handshake_timeout_secs)
        except TimeoutError:
            raise RuntimeError("FUNASR_HANDSHAKE_TIMEOUT: handshake timed out") from None
        if isinstance(raw, bytes):
            raise RuntimeError("FUNASR_WS_PROTOCOL_ERROR: expected JSON handshake event")
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            raise RuntimeError("FUNASR_WS_PROTOCOL_ERROR: invalid JSON handshake event") from None
        if not isinstance(payload, Mapping):
            raise RuntimeError("FUNASR_WS_PROTOCOL_ERROR: handshake event must be an object")
        normalized_payload = dict(payload)
        self._audit(normalized_payload)
        event_name = str(normalized_payload.get("event") or "").strip().lower()
        if event_name == "error" or "error" in normalized_payload:
            raise RuntimeError(
                "FUNASR_WS_ERROR: "
                + _safe_message(normalized_payload.get("error"), "Fun-ASR-Nano service error")
            )
        if event_name != expected_event:
            raise RuntimeError(
                "FUNASR_WS_PROTOCOL_ERROR: expected "
                f"{expected_event}, received {event_name or 'unknown'}"
            )

    def _audit(self, payload: Mapping[str, object]) -> None:
        if self._raw_event_sink is not None:
            self._raw_event_sink(payload)

    def _set_terminal_error(self, code: str, message: str) -> ASREvent:
        self._terminal_error = (code, _safe_message(message, "Fun-ASR-Nano stream error"))
        self._final_ready.set()
        return ASREvent(
            kind="error",
            error_code=code,
            error_message=self._terminal_error[1],
        )

    @staticmethod
    def _looks_like_result(payload: Mapping[str, object]) -> bool:
        return any(key in payload for key in ("sentences", "partial", "is_final"))

    def _normalize_result(
        self,
        payload: Mapping[str, object],
    ) -> tuple[TranscriptWindow, str | None]:
        raw_is_final = payload.get("is_final")
        if "is_final" in payload and not isinstance(raw_is_final, bool):
            raise ValueError("is_final must be a boolean")
        partial = _transcript_text(payload.get("partial"), "partial")
        raw_sentences = payload.get("sentences")
        if raw_sentences is None:
            raw_sentences = []
        if not isinstance(raw_sentences, list):
            raise ValueError("sentences must be a list")
        if len(raw_sentences) > _MAX_SENTENCES:
            raise ValueError("sentences exceeds the result size limit")

        duration = _timestamp(payload.get("duration_ms"))
        duration_invalid = payload.get("duration_ms") is not None and duration is None
        segments: list[NormalizedSegment] = []
        previous_start = -1
        previous_end = -1
        invalid_count = 0
        for _index, raw_sentence in enumerate(raw_sentences):
            if not isinstance(raw_sentence, Mapping):
                invalid_count += 1
                continue
            text = _transcript_text(raw_sentence.get("text"), "sentence.text")
            if not text:
                continue
            start = _timestamp(raw_sentence.get("start"))
            end = _timestamp(raw_sentence.get("end"))
            valid = (
                start is not None
                and end is not None
                and start <= end
                and start >= previous_start
                and end >= previous_end
                and (duration is None or end <= duration)
            )
            if not valid:
                invalid_count += 1
                continue
            assert start is not None
            assert end is not None
            previous_start = start
            previous_end = end
            start_ms = start + self._context.offset_ms
            end_ms = end + self._context.offset_ms
            speaker = raw_sentence.get("spk")
            identity = f"{self._context.source_epoch}|{start_ms}|{end_ms}|{text}|{speaker!r}"
            segments.append(
                NormalizedSegment(
                    id=uuid5(NAMESPACE_URL, f"voice-realtime:funasr-nano:{identity}"),
                    order=len(segments),
                    source_epoch=self._context.source_epoch,
                    speaker_key=_speaker_key(self._context.source_epoch, speaker),
                    start_ms=start_ms,
                    end_ms=end_ms,
                    text=text,
                )
            )

        timestamp_error: str | None = None
        if duration_invalid:
            timestamp_error = "Fun-ASR-Nano duration_ms is invalid"
        elif invalid_count:
            timestamp_error = "Fun-ASR-Nano sentence timestamps are missing or non-monotonic"

        return (
            TranscriptWindow(
                source_epoch=self._context.source_epoch,
                partial=partial,
                segments=tuple(segments),
            ),
            timestamp_error,
        )

    async def _close_connection(self) -> None:
        self._connected = False
        self._ready_pending = False
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                return
