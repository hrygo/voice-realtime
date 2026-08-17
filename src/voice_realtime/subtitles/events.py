"""WhisperLiveKit 字幕事件协议：WS 客户端 + 事件规范化。

事件形状对齐 WhisperLiveKit FrontData.to_dict()：
partial = buffer_transcription；confirmed = lines 中已定稿段（带时间戳/说话人）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

import websockets

EventKind = Literal["config", "partial", "confirmed", "error", "ready_to_stop", "other"]

DEFAULT_WAV_HEADER = b"RIFF"  # 客户端可先发送 WAV 头声明格式（WhisperLiveKit 支持）


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


def parse_event(payload: dict[str, Any]) -> SubtitleEvent:
    """将 WhisperLiveKit 消息归一化为 SubtitleEvent。"""
    msg_type = payload.get("type")
    if msg_type == "config":
        return SubtitleEvent(kind="config", raw=payload)
    if msg_type == "error":
        return SubtitleEvent(kind="error", text=payload.get("error") or "", raw=payload)
    if msg_type == "ready_to_stop":
        return SubtitleEvent(kind="ready_to_stop", raw=payload)

    if "buffer_transcription" in payload:
        partial = (payload.get("buffer_transcription") or "").strip()
        lines = payload.get("lines") or []
        confirmed = [
            line for line in lines if (line.get("text") or "").strip() and line.get("speaker") != -2
        ]
        if confirmed:
            last = confirmed[-1]
            return SubtitleEvent(
                kind="confirmed",
                text=last.get("text", ""),
                start=last.get("start", ""),
                end=last.get("end", ""),
                speaker=last.get("speaker"),
                translation=last.get("translation"),
                detected_language=last.get("detected_language"),
                raw=payload,
            )
        if partial:
            return SubtitleEvent(kind="partial", text=partial, raw=payload)

    return SubtitleEvent(kind="other", raw=payload)


class SubtitleEventTracker:
    """增量跟踪：从全量快照流中提取「新」事件。

    WhisperLiveKit 周期性推送整份 FrontData（confirmed 段重复出现），
    本类按「confirmed 段起点+文本」与「partial 当前文本」去重。
    """

    def __init__(self) -> None:
        self._confirmed_seen: set[tuple[str, str]] = set()
        self._last_partial = ""

    def track(self, event: SubtitleEvent) -> bool:
        """返回 True 表示该事件是新的（应对外发出）。"""
        if event.kind == "confirmed":
            key = (event.start, event.text)
            if key in self._confirmed_seen:
                return False
            self._confirmed_seen.add(key)
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
        pcm_input: bool = False,
    ) -> None:
        params = [f"language={language}", "mode=full"]
        if pcm_input:
            params.append("pcm_input=true")
        query = "&".join(params)
        self._uri = f"{url}/asr?{query}"
        if token:
            self._uri += f"&token={token}"
        self._ws: websockets.ClientConnection | None = None

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
            yield parse_event(payload)

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
