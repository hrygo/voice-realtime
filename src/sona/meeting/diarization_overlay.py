"""会议分人 overlay：用非流式 diarize 修正流式转录的说话人归属。

背景：流式 WS 路径（SpeechRail 缺陷未修前）无法下发带 speaker 的 segment 事件，
会议转录的说话人恒为 ``speaker:0``。本模块在流式之外，用
:class:`~sona.speechrail.batch_transcriber.SpeechRailBatchTranscriber` 把会议期间
缓冲的 PCM 提交到非流式 diarize 端点，拿到**词级/短句级带 speaker 的结果**，
再按**时间戳重叠**把说话人标签归属到已确认的流式转录段上。

关键取舍：采用**标签归属（label-mapping）而非文本替换**。流式段已含用户实时看到
的文本，且其时间戳来自流式窗口；非流式 diarize 的文本与流式可能存在轻微 ASR 差异。
按时间重叠仅修正 speaker_key，保留流式段文本，避免两路 ASR 不一致造成文本跳变。
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sona.meeting.models import NormalizedSegment

logger = logging.getLogger(__name__)

# 16 kHz mono s16le = 32_000 bytes/sec
_BYTES_PER_SEC = 32_000

# 最小可提交分人的音频时长（避免空/静音片段浪费一次推理，约 0.8s）
_MIN_FLUSH_BYTES = int(0.8 * _BYTES_PER_SEC)


@dataclass(frozen=True, slots=True)
class SpeakerLabelledSpan:
    """一段已归属说话人的时间区间（用于往后端段映射）。"""

    speaker_key: str
    # 原始匿名标签（如 ``spk_01``）；仅诊断用途。
    raw_speaker: str
    start_ms: int
    end_ms: int


class BatchTranscriber(Protocol):
    """非流式 diarize 的窄端口；便于测试注入假实现。"""

    async def transcribe_diarize(self, pcm: bytes) -> object: ...


def meeting_diarization_group_id(owner: str | None) -> str:
    """把应用层会议 owner 映射到协议层匿名 group id。

    与流式路径（``subtitles/sessions.py::_diarization_group_id``）保持完全一致：
    ``sha256(owner)``，保证 overlay 修正的 speaker_key 与流式 confirmed 段落在
    **同一个命名空间**，否则 rename/remap 的 ``speaker_key`` 精确匹配会失效。
    """
    if not owner:
        raise ValueError("meeting diarization requires a capture owner")
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()


def meeting_speaker_key(group_id: str, raw_speaker: str) -> str:
    """把 SpeechRail 匿名标签映射到会议命名空间。

    与会话流式路径（``transcriber._speaker_key``）保持一致：``spk_01`` →
    ``group:{group_id}:speaker:spk_01``。``group_id`` 必须传
    :func:`meeting_diarization_group_id` 推导出的协议层 group id。
    """
    label = raw_speaker.strip()
    if not label:
        label = "0"
    return f"group:{group_id}:speaker:{label}"


def assign_speakers_by_overlap(
    segments: Sequence[NormalizedSegment],
    spans: Sequence[SpeakerLabelledSpan],
) -> dict[str, str]:
    """按时间重叠把说话人 span 归属到 confirmed 转录段。

    对每个 confirmed 段，在与之重叠的 span 中取**重叠时长最大**的那个说话人；
    无重叠时保守地归到无说话人（``speaker:0`` 不变）。返回
    ``{segment.id: speaker_key}`` 映射，只包含确实改动的段。
    """
    if not spans:
        return {}
    # 归一化：跨批量 span 时保证同一说话人 label 稳定（span 已映射，这里直接按 key）。
    result: dict[str, str] = {}
    for segment in segments:
        candidates: dict[str, int] = {}
        for span in spans:
            overlap = min(segment.end_ms, span.end_ms) - max(segment.start_ms, span.start_ms)
            if overlap > 0:
                candidates[span.speaker_key] = candidates.get(span.speaker_key, 0) + overlap
        if not candidates:
            continue
        best_key, best_overlap = max(candidates.items(), key=lambda item: (item[1], item[0]))
        if best_overlap <= 0:
            continue
        if best_key != segment.speaker_key:
            result[str(segment.id)] = best_key
    return result


class MeetingDiarizationOverlay:
    """会议分人 overlay：缓冲 PCM，周期/会末提交非流式 diarize。

    线程安全模型：所有状态都在 asyncio 单线程事件循环中访问（PCM 由
    ``push_pcm`` 同步回调、flush 为 async 任务），无需额外锁。
    """

    def __init__(
        self,
        *,
        transcriber: BatchTranscriber | None = None,
        group_id: str | None = None,
        max_buffer_seconds: int = 1800,
    ) -> None:
        self._transcriber = transcriber
        self._group_id = group_id or ""
        self._max_bytes = max(1, max_buffer_seconds) * _BYTES_PER_SEC
        self._buffer = b""
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def start(self, *, group_id: str | None = None) -> None:
        self._group_id = group_id or self._group_id or ""
        self._buffer = b""
        self._active = True

    def stop(self) -> None:
        self._active = False
        self._buffer = b""

    def push_pcm(self, data: bytes) -> None:
        """同步接收一个 PCM chunk（从 SubtitleProxy 音频监听器调用）。"""
        if not self._active or not data:
            return
        self._buffer += data
        if len(self._buffer) > self._max_bytes:
            # 丢弃最旧音频，避免长会议内存无界增长。
            self._buffer = self._buffer[-self._max_bytes :]

    def buffered_pcm(self) -> bytes:
        return self._buffer

    def reset_buffer(self) -> None:
        self._buffer = b""

    def clear(self) -> None:
        self.stop()

    async def flush(self) -> list[SpeakerLabelledSpan]:
        """把缓冲 PCM 提交到非流式 diarize，返回说话人 span。

        内容不足最小阈值时返回空；失败时吞掉异常并返回空（不中断会议）。
        """
        if not self._active or self._transcriber is None:
            return []
        if len(self._buffer) < _MIN_FLUSH_BYTES:
            return []
        pcm = self._buffer
        self._buffer = b""
        try:
            result = await self._transcriber.transcribe_diarize(pcm)
        except Exception as exc:
            logger.warning("MeetingDiarizationOverlay: 非流式分人失败: %s", exc)
            return []
        return _to_spans(result, self._group_id)

    async def finish(self) -> list[SpeakerLabelledSpan]:
        """会末冲刷：清空缓冲并返回最后一段说话人 span（调用方负责整体归属）。"""
        if not self._active:
            return []
        try:
            return await self.flush()
        finally:
            self._active = False
            self._buffer = b""


def _to_spans(result: object, group_id: str) -> list[SpeakerLabelledSpan]:
    """把 batch transcriber 返回结果归一化为说话人 span。"""
    segments = getattr(result, "segments", None)
    if not segments:
        return []
    spans: list[SpeakerLabelledSpan] = []
    for seg in segments:
        raw_speaker = getattr(seg, "speaker", None)
        start_ms = int(getattr(seg, "start_ms", 0) or 0)
        end_ms = int(getattr(seg, "end_ms", 0) or 0)
        if not isinstance(raw_speaker, str) or not raw_speaker.strip():
            continue
        if end_ms <= start_ms:
            continue
        spans.append(
            SpeakerLabelledSpan(
                speaker_key=meeting_speaker_key(group_id, raw_speaker),
                raw_speaker=raw_speaker,
                start_ms=start_ms,
                end_ms=end_ms,
            )
        )
    return spans


__all__ = [
    "BatchTranscriber",
    "MeetingDiarizationOverlay",
    "SpeakerLabelledSpan",
    "assign_speakers_by_overlap",
    "meeting_diarization_group_id",
    "meeting_speaker_key",
]
