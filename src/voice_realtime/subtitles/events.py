"""WhisperLiveKit 字幕事件协议：WS 客户端 + 事件规范化。

事件形状对齐 WhisperLiveKit FrontData.to_dict()：
partial = buffer_transcription；confirmed = lines 中已定稿段（带时间戳/说话人）。
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

import websockets

EventKind = Literal["config", "partial", "confirmed", "error", "ready_to_stop", "other"]


@dataclass
class SubtitleEvent:
    """规范化后的字幕事件。"""

    kind: EventKind
    text: str = ""
    start: str = ""
    end: str = ""
    speaker: int | None = None
    translation: str | None = None
    detected_language: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def parse_events(payload: dict[str, Any]) -> list[SubtitleEvent]:
    """将一个 WhisperLiveKit 全量快照展开为全部新增候选事件。

    ``lines`` 是服务端累计 confirmed 历史，而 ``buffer_transcription`` 是当前
    partial。两者可以同时存在，因此不能用 confirmed 的存在与否决定是否忽略 partial。
    去重由消费方的 :class:`SubtitleEventTracker` 完成。
    """
    msg_type = payload.get("type")
    if msg_type == "config":
        return [SubtitleEvent(kind="config", raw=payload)]
    if msg_type == "error":
        return [SubtitleEvent(kind="error", text=str(payload.get("error") or ""), raw=payload)]
    if msg_type == "ready_to_stop":
        return [SubtitleEvent(kind="ready_to_stop", raw=payload)]

    events: list[SubtitleEvent] = []
    if "buffer_transcription" in payload or "lines" in payload:
        partial = str(payload.get("buffer_transcription") or "").strip()
        raw_lines = payload.get("lines") or []
        lines = raw_lines if isinstance(raw_lines, list) else []
        confirmed = [
            line
            for line in lines
            if isinstance(line, dict)
            and str(line.get("text") or "").strip()
            and line.get("speaker") != -2
        ]
        events.extend(
            SubtitleEvent(
                kind="confirmed",
                text=str(line.get("text") or ""),
                start=str(line.get("start") or ""),
                end=str(line.get("end") or ""),
                speaker=line.get("speaker") if isinstance(line.get("speaker"), int) else None,
                translation=(
                    str(line["translation"]) if line.get("translation") is not None else None
                ),
                detected_language=(
                    str(line["detected_language"])
                    if line.get("detected_language") is not None
                    else None
                ),
                raw=payload,
            )
            for line in confirmed
        )
        if partial:
            events.append(SubtitleEvent(kind="partial", text=partial, raw=payload))

    return events or [SubtitleEvent(kind="other", raw=payload)]


def parse_event(payload: dict[str, Any]) -> SubtitleEvent:
    """兼容旧的单事件接口；存在 partial 时优先返回当前 partial。"""
    return parse_events(payload)[-1]


class SubtitleEventTracker:
    """增量跟踪：从全量快照流中提取「新」事件。

    WhisperLiveKit 周期性推送整份 FrontData（confirmed 段重复出现），
    本类按「confirmed 段起点+文本」与「partial 当前文本」去重。
    """

    def __init__(self, max_seen: int = 1024) -> None:
        if max_seen < 1:
            raise ValueError("max_seen 必须大于 0")
        self._max_seen: int = max_seen
        self._confirmed_seen: set[tuple[str, str, int | None, str]] = set()
        self._confirmed_order: deque[tuple[str, str, int | None, str]] = deque()
        self._last_partial = ""

    def track(self, event: SubtitleEvent) -> bool:
        """返回 True 表示该事件是新的（应对外发出）。"""
        if event.kind == "confirmed":
            key = (event.start, event.end, event.speaker, event.text)
            if key in self._confirmed_seen:
                return False
            if len(self._confirmed_seen) >= self._max_seen:
                oldest = self._confirmed_order.popleft()
                self._confirmed_seen.remove(oldest)
            self._confirmed_seen.add(key)
            self._confirmed_order.append(key)
            # 一旦产生新的 confirmed，下一段 partial 即使文本碰巧相同也应重新发出。
            self._last_partial = ""
            return True
        if event.kind == "partial":
            text = event.text.strip()
            if not text or text == self._last_partial:
                return False
            self._last_partial = text
            return True
        return True


class SubtitleStream:
    """WhisperLiveKit /asr WebSocket 客户端。

    用法：connect() 后持续 send_audio() 推音频字节，
    async for event in stream.events() 消费规范化事件。
    """

    def __init__(
        self,
        url: str,
        language: str = "Chinese",
        token: str | None = None,
    ) -> None:
        params = [f"language={language}", "mode=full"]
        query = "&".join(params)
        self._uri = f"{url}/asr?{query}"
        if token:
            self._uri += f"&token={token}"
        self._ws: websockets.ClientConnection | None = None

    @property
    def uri(self) -> str:
        """返回当前字幕 WebSocket 地址。"""
        return self._uri

    async def connect(self) -> None:
        self._ws = await websockets.connect(self._uri)

    async def send_audio(self, chunk: bytes) -> None:
        if self._ws is None:
            raise RuntimeError("SubtitleStream 未连接")
        await self._ws.send(chunk)

    async def events(self) -> AsyncIterator[SubtitleEvent]:
        if self._ws is None:
            raise RuntimeError("SubtitleStream 未连接")
        async for raw in self._ws:
            if isinstance(raw, bytes):
                continue
            payload = json_loads(raw)
            for event in parse_events(payload):
                yield event

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None


def json_loads(raw: str) -> dict[str, Any]:
    """容错 JSON 解析（消息异常时降级为 other 事件）。"""
    import json

    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}
