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

from .models import MeetingRecord, MeetingStatus, MinutesRecord, TranscriptWindow
from .persistence import TranscriptPersistence
from .ports import (
    CaptureFinalizationTimeoutError,
    MeetingCaptureGateway,
    MinutesStore,
    SpeakerStore,
    TranscriptStore,
)

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._gateway = gateway
        self._persistence = persistence
        self._speakers = speakers
        self._transcripts = transcripts
        self._minutes_store = minutes_store
        self._timeout_secs = timeout_secs
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
            await self._persistence.replay_pending(meeting_id)
            if final_window is not None and final_window.speaker_remap:
                await self._speakers.apply_speaker_remapping(
                    meeting_id, dict(final_window.speaker_remap)
                )
            record = await self._transcripts.finalize_transcript(
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


__all__ = ["MeetingFinalizationResult", "MeetingFinalizer"]
