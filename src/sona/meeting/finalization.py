"""会议 stop 后的业务顺序 finalization use case。

固化 ``finish capture → persist final window → replay journal → speaker remap
→ finalize transcript → create minutes`` 的顺序；清理路径最多执行一次。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from uuid import UUID

from .diarization_overlay import (
    MeetingDiarizationOverlay,
    assign_speakers_by_overlap,
)
from .models import (
    MeetingRecord,
    MeetingStatus,
    MinutesRecord,
    NormalizedSegment,
    TranscriptWindow,
)
from .persistence import TranscriptPersistence
from .ports import (
    CaptureFinalizationTimeoutError,
    MeetingCaptureGateway,
    MinutesStore,
    SpeakerStore,
    TranscriptStore,
)

logger = logging.getLogger(__name__)


def _with_speaker(segment: NormalizedSegment, speaker_key: str) -> NormalizedSegment:
    """返回一个替换 speaker_key 后的 segmement 拷贝（保持其余字段不变）。"""
    if speaker_key == segment.speaker_key:
        return segment
    return NormalizedSegment(
        id=segment.id,
        order=segment.order,
        source_epoch=segment.source_epoch,
        speaker_key=speaker_key,
        start_ms=segment.start_ms,
        end_ms=segment.end_ms,
        text=segment.text,
        translation=segment.translation,
        detected_language=segment.detected_language,
    )


@dataclass(frozen=True, slots=True)
class MeetingFinalizationResult:
    """finalize 的稳定产出；不携带 cleanup/listener/event publisher。"""

    record: MeetingRecord
    minutes: MinutesRecord
    final_window: TranscriptWindow | None
    timed_out: bool


class MeetingFinalizer:
    """按固定顺序封存会议，并保证 capture cleanup 至多一次。"""

    def __init__(
        self,
        *,
        gateway: MeetingCaptureGateway,
        persistence: TranscriptPersistence,
        speakers: SpeakerStore,
        transcripts: TranscriptStore,
        minutes_store: MinutesStore,
        timeout_secs: float,
        diarization_overlay: MeetingDiarizationOverlay | None = None,
    ) -> None:
        self._gateway = gateway
        self._persistence = persistence
        self._speakers = speakers
        self._transcripts = transcripts
        self._minutes_store = minutes_store
        self._timeout_secs = timeout_secs
        self._diarization_overlay = diarization_overlay
        self._capture_closed = False
        self._finalized_record: MeetingRecord | None = None

    @property
    def capture_closed(self) -> bool:
        """finalizer 是否已接管并关闭 capture（finish 成功/超时或已 abort）。"""
        return self._capture_closed

    @property
    def finalized_record(self) -> MeetingRecord | None:
        """finalize_transcript 已成功的记录；下游失败时供上层保持终态。"""
        return self._finalized_record

    async def finalize(self, meeting_id: UUID) -> MeetingFinalizationResult:
        timed_out = False
        final_window: TranscriptWindow | None = None
        try:
            try:
                final_window = await self._gateway.finish_capture(
                    timeout_secs=self._timeout_secs
                )
            except CaptureFinalizationTimeoutError as exc:
                timed_out = True
                final_window = exc.last_window
            self._capture_closed = True
            if final_window is not None:
                await self._persistence.reconcile(meeting_id, final_window)
            await self._apply_diarization_overlay(meeting_id)
            if final_window is not None and final_window.speaker_remap:
                await self._speakers.apply_speaker_remapping(
                    meeting_id, dict(final_window.speaker_remap)
                )
            record = await self._persistence.finalize(
                meeting_id,
                final_status=(
                    MeetingStatus.INTERRUPTED if timed_out else MeetingStatus.COMPLETED
                ),
                reason="finalization_timeout" if timed_out else None,
            )
            self._finalized_record = record
            minutes = await self._minutes_store.create_minutes(
                meeting_id,
                idempotency_key=f"meeting:{meeting_id}:minutes:v1",
            )
            return MeetingFinalizationResult(
                record=record,
                minutes=minutes,
                final_window=final_window,
                timed_out=timed_out,
            )
        except BaseException:
            if not self._capture_closed:
                cleanup = asyncio.create_task(self._abort_capture())
                await asyncio.shield(cleanup)
                self._capture_closed = True
            raise

    async def _abort_capture(self) -> None:
        with contextlib.suppress(Exception):
            await self._gateway.abort_capture()

    async def _apply_diarization_overlay(self, meeting_id: UUID) -> None:
        # 流式 confirmed 段说话人恒为 speaker:0；此处经非流式 diarize 按时间重叠
        # 修正其 speaker_key。分人是增强项，任何失败都不得中断会议封存。
        overlay = self._diarization_overlay
        if overlay is None:
            return
        try:
            spans = await overlay.finish()
        except Exception:
            logger.warning("MeetingFinalizer: diarization overlay 失败，跳过分人", exc_info=True)
            return
        if not spans:
            return
        try:
            document = await self._transcripts.get_transcript(meeting_id)
        except Exception:
            logger.warning("MeetingFinalizer: 读取全量转录失败，跳过分人", exc_info=True)
            return
        segments = document.segments
        if not segments:
            return
        mapping = assign_speakers_by_overlap(segments, spans)
        if not mapping:
            return
        corrected = tuple(
            _with_speaker(segment, mapping.get(str(segment.id), segment.speaker_key))
            for segment in segments
        )
        if corrected == segments:
            return
        corrected_window = TranscriptWindow(
            source_epoch=max((segment.source_epoch for segment in segments), default=0),
            segments=corrected,
        )
        try:
            await self._persistence.reconcile(meeting_id, corrected_window)
        except Exception:
            logger.warning("MeetingFinalizer: 说话人修正写入失败，保留流式结果", exc_info=True)


__all__ = ["MeetingFinalizationResult", "MeetingFinalizer"]
