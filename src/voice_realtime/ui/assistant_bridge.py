"""StatusBridgeObserver：Pipecat 管道帧 → `/ws/assistant` WS 事件桥。

非侵入式观测：注册为 `PipelineWorker(observers=[...])`，由 `WorkerObserver`
异步队列分发（不阻塞管道）。捕获结构化帧并序列化为《Voice Studio UI 设计
方案》§5 协议事件，multi-cast 到浏览器客户端。

覆盖帧：
- `InterimTranscriptionFrame` / `TranscriptionFrame` → `stt`（interim/final）
- `LLMTextFrame` (增量) / `LLMFullResponseEndFrame` → `llm` + `turn_id`
- `TTSTextFrame` / `TTSStartedFrame` / `TTSStoppedFrame` → `tts`
- `TTSAudioRawFrame` → `tts`（仅计数，时间窗节流，不逐帧广播）
- `UserStartedSpeakingFrame` / `UserStoppedSpeakingFrame` → `vad`
- `InterruptionFrame` → `interruption`
- 管道启停 → `system`

注意：`on_push_frame` 对每个 source→destination 传输都触发，同一帧对象
会被推送多次，必须按 `frame.id` 去重后再序列化。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Any

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
    StartFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

logger = logging.getLogger(__name__)

# on_push_frame 按 frame.id 去重的集合上限；超限整体清空（旧的 frame 不会再出现，安全）
_MAX_SEEN_IDS = 10000


@dataclass(slots=True)
class _ClientState:
    send: Callable[[str], Awaitable[None]]
    queue: asyncio.Queue[str]
    task: asyncio.Task[None]
    dropped: int = 0


@dataclass(frozen=True, slots=True)
class TTSSourceDiagnostics:
    first_chunk_ms: float | None
    chunk_count: int
    max_source_chunk_gap_ms: float | None
    median_source_chunk_gap_ms: float | None
    source_chunk_gaps_over_200ms: int


class StatusBridgeObserver(BaseObserver):
    """管道状态观测器：帧 → 协议事件 → 浏览器 WS 广播。

    用法：
        observer = StatusBridgeObserver()
        observer.add_client(ws_send)          # 浏览器订阅
        worker = PipelineWorker(pipeline, observers=[observer])
    """

    def __init__(
        self,
        tts_throttle_secs: float = 0.5,
        client_queue_size: int = 32,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._ws_clients: dict[Callable[[str], Awaitable[None]], _ClientState] = {}
        self._client_queue_size = max(1, client_queue_size)
        self._client_counter = 0
        self._seen_ids: set[int] = set()
        self._tts_throttle_secs = tts_throttle_secs
        self._monotonic = monotonic
        self._tts_chunks = 0
        self._tts_last_broadcast: float = 0.0
        self._tts_source_started_at: float | None = None
        self._tts_source_first_chunk_ms: float | None = None
        self._tts_source_chunk_count = 0
        self._tts_source_last_chunk_at: float | None = None
        self._tts_source_chunk_gaps_ms: list[float] = []
        self._turn_id = 0
        self._current_sentence = ""
        # Turn-level 耗时度量时间戳
        self._t_speaking_start: float | None = None
        self._t_silence: float | None = None
        self._t_stt_final: float | None = None
        self._t_llm_first: float | None = None
        self._t_tts_first: float | None = None
        self._tts_active = False
        self._metrics_turn_id = 0

    # ---------- 客户端管理（与 SubtitleProxy 同接口） ----------

    def add_client(self, ws_send: Callable[[str], Awaitable[None]]) -> str:
        """注册浏览器 WS 客户端，返回 client_id（用于移除）。"""
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self._client_queue_size)
        task = asyncio.create_task(self._client_sender(ws_send, queue))
        self._ws_clients[ws_send] = _ClientState(send=ws_send, queue=queue, task=task)
        self._client_counter += 1
        client_id = f"asst_{self._client_counter}"
        logger.info("StatusBridgeObserver: 新浏览器订阅 (共 %d 个)", len(self._ws_clients))
        return client_id

    def remove_client(self, ws_send: Callable[[str], Awaitable[None]]) -> None:
        """移除浏览器 WS 客户端。"""
        state = self._ws_clients.pop(ws_send, None)
        if state is not None:
            state.task.cancel()
            logger.info("StatusBridgeObserver: 浏览器取消订阅 (剩余 %d 个)", len(self._ws_clients))

    @property
    def has_clients(self) -> bool:
        return bool(self._ws_clients)

    @property
    def tts_source_diagnostics(self) -> TTSSourceDiagnostics:
        """返回最近一轮 TTS 的源音频块节奏快照。"""
        gaps = self._tts_source_chunk_gaps_ms
        return TTSSourceDiagnostics(
            first_chunk_ms=self._tts_source_first_chunk_ms,
            chunk_count=self._tts_source_chunk_count,
            max_source_chunk_gap_ms=max(gaps) if gaps else None,
            median_source_chunk_gap_ms=median(gaps) if gaps else None,
            source_chunk_gaps_over_200ms=sum(gap > 200.0 for gap in gaps),
        )

    # ---------- BaseObserver 回调 ----------

    async def on_pipeline_started(self) -> None:
        """管道完全启动后广播 system 事件。"""
        await self._emit_event({"type": "system", "state": "pipeline_started"})

    async def on_push_frame(self, data: FramePushed) -> None:
        """捕获帧传输：去重后按类型序列化为协议事件。"""
        frame = data.frame
        if not self._dedupe(frame):
            return
        try:
            await self._handle_frame(frame)
        except Exception:
            # 观测不可阻塞管道：任何序列化/广播失败只记录，不外溢
            logger.warning("StatusBridgeObserver: 处理帧异常 %s", frame.__class__.__name__,
                           exc_info=True)

    # ---------- 帧处理 ----------

    async def _handle_frame(self, frame: Frame) -> None:
        now_ts = self._monotonic()
        if isinstance(frame, InterimTranscriptionFrame):
            await self._emit_event(
                {"type": "stt", "state": "interim", "text": frame.text, "t": self._now()}
            )
        elif isinstance(frame, TranscriptionFrame):
            self._t_stt_final = now_ts
            await self._emit_event(
                {"type": "stt", "state": "final", "text": frame.text, "t": self._now()}
            )
        elif isinstance(frame, LLMTextFrame):
            if self._t_llm_first is None:
                self._t_llm_first = now_ts
            await self._emit_event(
                {
                    "type": "llm",
                    "state": "streaming",
                    "text": frame.text,
                    "turn_id": self._turn_id,
                }
            )
        elif isinstance(frame, LLMFullResponseEndFrame):
            await self._emit_event(
                {"type": "llm", "state": "final", "turn_id": self._turn_id, "text": ""}
            )
            self._turn_id += 1
        elif isinstance(frame, TTSTextFrame):
            # 记录当前句子，供 TTSStartedFrame 携带
            self._current_sentence = frame.text
        elif isinstance(frame, (TTSStartedFrame, BotStartedSpeakingFrame)):
            if not self._tts_active:
                self._tts_active = True
                self._reset_tts_source_diagnostics(now_ts)
                await self._emit_event(
                    {"type": "tts", "state": "started", "sentence": self._current_sentence}
                )
        elif isinstance(frame, (TTSStoppedFrame, BotStoppedSpeakingFrame)):
            self._tts_active = False
            await self._emit_event({"type": "tts", "state": "stopped"})
            self._tts_chunks = 0
        elif isinstance(frame, TTSAudioRawFrame):
            self._record_tts_source_chunk(now_ts)
            if self._t_tts_first is None:
                self._t_tts_first = now_ts
                await self._emit_turn_metrics()
            await self._handle_tts_audio(now_ts)
        elif isinstance(frame, UserStartedSpeakingFrame):
            self._reset_turn_metrics()
            self._t_speaking_start = now_ts
            self._metrics_turn_id = self._turn_id
            await self._emit_event(
                {"type": "vad", "state": "user_speaking", "t": self._now()}
            )
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._t_silence = now_ts
            await self._emit_event(
                {"type": "vad", "state": "user_silence", "t": self._now()}
            )
        elif isinstance(frame, (InterruptionFrame, CancelFrame, ErrorFrame)):
            self._tts_active = False
            await self._emit_event({"type": "tts", "state": "stopped"})
            await self._emit_event(
                {"type": "interruption", "state": "detected", "t": self._now()}
            )
        elif isinstance(frame, EndFrame):
            await self._emit_event({"type": "system", "state": "pipeline_stopped"})
        elif isinstance(frame, StartFrame):
            # StartFrame 系统帧在 on_push_frame 也能捕获；on_pipeline_started 兜底
            await self._emit_event({"type": "system", "state": "pipeline_started"})

    async def _emit_turn_metrics(self) -> None:
        """以用户说话结束为起点，下发一轮对话的端到端耗时瀑布。"""
        if (self._t_silence is None and self._t_stt_final is None) or self._t_tts_first is None:
            return

        if self._t_stt_final is not None and self._t_silence is not None:
            turn_ready = max(self._t_stt_final, self._t_silence)
        elif self._t_stt_final is not None:
            turn_ready = self._t_stt_final
        else:
            assert self._t_silence is not None
            turn_ready = self._t_silence

        stt_ms = (
            max(0.0, round((self._t_stt_final - self._t_silence) * 1000, 1))
            if self._t_stt_final is not None and self._t_silence is not None
            else None
        )

        llm_ttft_ms = (
            max(0.0, round((self._t_llm_first - turn_ready) * 1000, 1))
            if self._t_llm_first is not None and turn_ready is not None
            else None
        )
        tts_ttfb_ms = (
            max(0.0, round((self._t_tts_first - self._t_llm_first) * 1000, 1))
            if self._t_tts_first is not None and self._t_llm_first is not None
            else None
        )
        e2e_ms: float | None
        if stt_ms is not None and llm_ttft_ms is not None and tts_ttfb_ms is not None:
            # 总耗时按已展示的一位小数求和，避免独立四舍五入后出现 0.1ms 视觉不一致。
            e2e_ms = round(stt_ms + llm_ttft_ms + tts_ttfb_ms, 1)
        else:
            e2e_ms = (
                max(0.0, round((self._t_tts_first - self._t_silence) * 1000, 1))
                if self._t_silence is not None
                else None
            )
        await self._emit_event(
            {
                "type": "metrics",
                "turn_id": self._metrics_turn_id,
                "stt_ms": stt_ms,
                "llm_ttft_ms": llm_ttft_ms,
                "tts_ttfb_ms": tts_ttfb_ms,
                "e2e_ms": e2e_ms,
            }
        )

    def _reset_turn_metrics(self) -> None:
        self._t_speaking_start = None
        self._t_silence = None
        self._t_stt_final = None
        self._t_llm_first = None
        self._t_tts_first = None

    def _reset_tts_source_diagnostics(self, started_at: float) -> None:
        self._tts_source_started_at = started_at
        self._tts_source_first_chunk_ms = None
        self._tts_source_chunk_count = 0
        self._tts_source_last_chunk_at = None
        self._tts_source_chunk_gaps_ms = []

    def _record_tts_source_chunk(self, received_at: float) -> None:
        if self._tts_source_chunk_count == 0 and self._tts_source_started_at is not None:
            self._tts_source_first_chunk_ms = max(
                0.0,
                round((received_at - self._tts_source_started_at) * 1000, 1),
            )
        if self._tts_source_last_chunk_at is not None:
            self._tts_source_chunk_gaps_ms.append(
                max(0.0, round((received_at - self._tts_source_last_chunk_at) * 1000, 1))
            )
        self._tts_source_chunk_count += 1
        self._tts_source_last_chunk_at = received_at

    async def _handle_tts_audio(self, now: float) -> None:
        """TTS 音频帧：仅计数，超节流窗口才广播一次 synthesizing。"""
        self._tts_chunks += 1
        if now - self._tts_last_broadcast < self._tts_throttle_secs:
            return
        self._tts_last_broadcast = now
        await self._emit_event(
            {"type": "tts", "state": "synthesizing", "chunks": self._tts_chunks}
        )

    # ---------- 内部工具 ----------

    def _dedupe(self, frame: Frame) -> bool:
        """同一帧对象经多跳传输会触发多次 on_push_frame；按 id 去重。"""
        if frame.id in self._seen_ids:
            return False
        self._seen_ids.add(frame.id)
        if len(self._seen_ids) > _MAX_SEEN_IDS:
            self._seen_ids.clear()
        return True

    async def _emit_event(self, payload: dict[str, Any]) -> None:
        """序列化为 JSON 并 multi-cast 给所有浏览器客户端。"""
        if not self._ws_clients:
            return
        text = json.dumps(payload, ensure_ascii=False)
        for state in list(self._ws_clients.values()):
            if state.queue.full():
                try:
                    state.queue.get_nowait()
                    state.queue.task_done()
                except asyncio.QueueEmpty:
                    pass
                state.dropped += 1
            state.queue.put_nowait(text)
        await asyncio.sleep(0)
        logger.debug("StatusBridgeObserver: 广播 %s", text)

    async def _client_sender(
        self,
        send: Callable[[str], Awaitable[None]],
        queue: asyncio.Queue[str],
    ) -> None:
        try:
            while True:
                text = await queue.get()
                try:
                    await send(text)
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("StatusBridgeObserver: 浏览器发送失败", exc_info=True)
        finally:
            state = self._ws_clients.get(send)
            if state is not None and state.task is asyncio.current_task():
                self._ws_clients.pop(send, None)

    @staticmethod
    def _now() -> str:
        """事件时间戳 `HH:MM:SS.mmm`。"""
        return datetime.now().strftime("%H:%M:%S.%f")[:-3]
