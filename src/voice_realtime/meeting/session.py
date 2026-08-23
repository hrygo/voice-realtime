"""会议录制领域服务：持久化 confirmed 转录并可靠结束 ASR 会话。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from voice_realtime.meeting.diarization_smoother import DiarizationSmoother
from voice_realtime.meeting.models import (
    MeetingRecord,
    MeetingStatus,
    StorageHealth,
    TranscriptWindow,
)

WindowListener = Callable[[TranscriptWindow], Awaitable[None]]
EventPublisher = Callable[[str, UUID, object], Awaitable[None]]

logger = logging.getLogger(__name__)


class MeetingStorageUnavailableError(RuntimeError):
    """会议开始前 PostgreSQL 不可写。"""

    code = "storage_unavailable"


MeetingStorageUnavailable = MeetingStorageUnavailableError


class MeetingSession:
    """会议生命周期与 Repository/Gateway 之间的适配器。"""

    def __init__(
        self,
        repository: Any,
        gateway: Any | None = None,
        summary_service: Any | None = None,
        *,
        subtitle_proxy: Any | None = None,
        language: str = "Chinese",
        audio_source: str = "microphone",
        finalization_timeout_secs: float = 8.0,
        recovery_journal: Any | None = None,
        event_publisher: EventPublisher | None = None,
        diarization_smoother: DiarizationSmoother | None = None,
    ) -> None:
        if gateway is None:
            gateway = subtitle_proxy
        if gateway is None:
            raise ValueError("subtitle proxy/gateway 不能为空")
        if finalization_timeout_secs <= 0:
            raise ValueError("finalization_timeout_secs 必须大于 0")
        self.repository = repository
        self.gateway = gateway
        self.summary_service = summary_service
        self.language = language
        self.audio_source = audio_source
        self.finalization_timeout_secs = finalization_timeout_secs
        self.recovery_journal = recovery_journal
        self.event_publisher = event_publisher
        self.diarization_smoother = diarization_smoother
        self._lock = asyncio.Lock()
        self._active_meeting_id: UUID | None = None
        self._record: MeetingRecord | None = None
        self._listener: WindowListener | None = None
        self._last_window_signature: tuple[Any, ...] | None = None
        self._storage_degraded = False

    @property
    def active_meeting_id(self) -> UUID | None:
        return self._active_meeting_id

    @property
    def record(self) -> MeetingRecord | None:
        return self._record

    @property
    def last_window(self) -> TranscriptWindow | None:
        value = getattr(self.gateway, "_capture_last_window", None)
        return value if isinstance(value, TranscriptWindow) else None

    @property
    def storage_health(self) -> StorageHealth:
        return StorageHealth.DEGRADED if self._storage_degraded else StorageHealth.OK

    async def start(self, title: str | None = None) -> MeetingRecord:
        async with self._lock:
            if self._active_meeting_id is not None:
                raise RuntimeError("meeting 已经在录制")
            normalized_title = (title or "").strip()
            if not normalized_title:
                normalized_title = datetime.now(UTC).strftime("会议-%Y%m%d-%H%M%S")
            try:
                writable = await self.repository.check_writable()
                if not writable:
                    raise MeetingStorageUnavailableError("meeting storage unavailable")
                record = await self.repository.create_meeting(
                    normalized_title,
                    language=self.language,
                    audio_source=self.audio_source,
                )
            except MeetingStorageUnavailableError:
                raise
            except Exception as exc:
                raise MeetingStorageUnavailableError(
                    "meeting storage unavailable"
                ) from exc
            self._record = record
            self._active_meeting_id = record.id
            self._last_window_signature = None
            self._storage_degraded = False
            listener = self._on_window
            self._listener = listener
            self.gateway.add_event_listener(listener)
            add_gap_listener = getattr(self.gateway, "add_gap_listener", None)
            if add_gap_listener is not None:
                add_gap_listener(self._on_gap)
            try:
                await self.gateway.begin_capture(f"meeting:{record.id}")
            except Exception:
                with contextlib.suppress(Exception):
                    self.gateway.remove_event_listener(listener)
                with contextlib.suppress(Exception):
                    await self.repository.set_status(
                        record.id,
                        MeetingStatus.INTERRUPTED,
                        reason="capture_start_failed",
                    )
                self._listener = None
                self._record = None
                self._active_meeting_id = None
                raise
            requeue = getattr(self.summary_service, "requeue_for_recording", None)
            if requeue is not None:
                with contextlib.suppress(Exception):
                    result = requeue()
                    if asyncio.iscoroutine(result):
                        await result
            await self._emit(
                "meeting_state_changed",
                record.id,
                self._meeting_state_payload(record),
            )
            return cast(MeetingRecord, record)

    async def stop(self) -> MeetingRecord:
        async with self._lock:
            meeting_id = self._active_meeting_id
            if meeting_id is None:
                raise RuntimeError("meeting not active")
            record = await self.repository.set_status(meeting_id, MeetingStatus.FINALIZING)
            await self._emit(
                "meeting_state_changed",
                meeting_id,
                self._meeting_state_payload(record),
            )
            timed_out = False
            timeout_window: TranscriptWindow | None = None
            try:
                try:
                    timeout_window = await self.gateway.finish_capture(
                        timeout_secs=self.finalization_timeout_secs
                    )
                except TimeoutError as exc:
                    timed_out = True
                    timeout_window = getattr(exc, "last_window", None)
                if timeout_window is not None:
                    await self._persist_window(meeting_id, timeout_window)
                record = await self.repository.finalize_transcript(
                    meeting_id,
                    final_status=(
                        MeetingStatus.INTERRUPTED if timed_out else MeetingStatus.COMPLETED
                    ),
                    reason="finalization_timeout" if timed_out else None,
                )
                minutes = await self.repository.create_minutes(
                    meeting_id,
                    idempotency_key=f"meeting:{meeting_id}:minutes:v1",
                )
                self._record = record
                minutes_status = getattr(minutes, "status", "queued")
                await self._emit(
                    "meeting_state_changed",
                    meeting_id,
                    self._meeting_state_payload(record),
                )
                await self._emit(
                    "minutes_state_changed",
                    meeting_id,
                    {
                        "minutes_id": str(getattr(minutes, "id", "")) or None,
                        "version": int(getattr(minutes, "version", 1)),
                        "status": str(getattr(minutes_status, "value", minutes_status)),
                        "error_code": None,
                        "error_message": None,
                        "minutes": None,
                    },
                )
                return cast(MeetingRecord, record)
            except Exception:
                with contextlib.suppress(Exception):
                    await self.repository.set_status(
                        meeting_id,
                        MeetingStatus.INTERRUPTED,
                        reason="meeting_stop_failed",
                    )
                raise
            finally:
                await self._release_listener()
                self._active_meeting_id = None
                await self._resume_summary_worker()

    async def interrupt(self, reason: str) -> MeetingRecord | None:
        async with self._lock:
            meeting_id = self._active_meeting_id
            if meeting_id is None:
                return self._record
            with contextlib.suppress(Exception):
                await self.gateway.abort_capture()
            record = await self.repository.set_status(
                meeting_id,
                MeetingStatus.INTERRUPTED,
                reason=reason[:128],
            )
            self._record = record
            await self._emit(
                "meeting_state_changed",
                meeting_id,
                self._meeting_state_payload(record),
            )
            await self._release_listener()
            self._active_meeting_id = None
            await self._resume_summary_worker()
            return cast(MeetingRecord, record)

    async def recover_stale(self) -> int:
        recover = getattr(self.repository, "recover_stale", None)
        if recover is None:
            return 0
        result = await recover()
        return int(result or 0)

    async def _on_window(self, window: TranscriptWindow) -> None:
        meeting_id = self._active_meeting_id
        if meeting_id is None:
            return
        if self.diarization_smoother is not None:
            window = self.diarization_smoother.smooth_window(window)
        if window.partial:
            await self._emit(
                "transcript_partial",
                meeting_id,
                {"text": window.partial, "speaker_key": None, "speaker_name": None},
            )
        if not window.segments:
            return
        result = await self._persist_window(meeting_id, window)
        if result is None:
            return
        await self._emit(
            "transcript_reconciled",
            meeting_id,
            {
                "transcript_revision": int(getattr(result, "transcript_revision", 0)),
                "content_revision": int(getattr(result, "content_revision", 0)),
                "replace_from_ms": int(getattr(result, "replace_from_ms", 0)),
                "segments": [self._segment_payload(segment) for segment in window.segments],
            },
        )

    async def _persist_window(self, meeting_id: UUID, window: TranscriptWindow) -> Any | None:
        signature = (
            window.source_epoch,
            tuple(
                (
                    segment.id,
                    segment.start_ms,
                    segment.end_ms,
                    segment.speaker_key,
                    segment.text,
                )
                for segment in window.segments
            ),
        )
        if signature == self._last_window_signature:
            return None
        try:
            result = await self.repository.reconcile_window(meeting_id, window)
        except Exception:
            self._storage_degraded = True
            journal = self.recovery_journal
            if journal is None:
                with contextlib.suppress(Exception):
                    await self.gateway.abort_capture()
                raise
            try:
                append = getattr(journal, "append", None)
                if append is None:
                    raise RuntimeError("recovery journal unavailable")
                result = append(meeting_id, window)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                with contextlib.suppress(Exception):
                    await self.gateway.abort_capture()
                raise
            return None
        self._last_window_signature = signature
        self._storage_degraded = False
        return result

    async def _on_gap(self, gap: Any) -> None:
        meeting_id = self._active_meeting_id
        if meeting_id is None:
            return
        await self._emit(
            "transcription_gap",
            meeting_id,
            {
                "start_ms": int(getattr(gap, "start_ms", 0)),
                "end_ms": int(getattr(gap, "end_ms", 0)),
                "reason": "asr_reconnect",
            },
        )

    async def _emit(self, event_type: str, meeting_id: UUID, payload: object) -> None:
        publisher = self.event_publisher
        if publisher is None:
            return
        try:
            await publisher(event_type, meeting_id, payload)
        except Exception:
            logger.warning("会议实时事件广播失败: %s", event_type, exc_info=True)

    @staticmethod
    def _meeting_state_payload(record: MeetingRecord) -> dict[str, object | None]:
        return {
            "status": record.status.value,
            "started_at": record.started_at.isoformat(),
            "ended_at": record.ended_at.isoformat() if record.ended_at else None,
            "interruption_reason": record.interruption_reason,
        }

    @staticmethod
    def _segment_payload(segment: Any) -> dict[str, object | None]:
        speaker_key = str(segment.speaker_key)
        raw = speaker_key.rsplit(":", 1)[-1].removeprefix("s")
        speaker_name = f"说话人 {raw}" if raw.isdigit() else speaker_key
        payload = segment.model_dump(mode="json")
        payload["speaker_name"] = speaker_name
        return cast(dict[str, object | None], payload)

    async def _release_listener(self) -> None:
        listener = self._listener
        self._listener = None
        if listener is not None:
            with contextlib.suppress(Exception):
                self.gateway.remove_event_listener(listener)
        remove_gap_listener = getattr(self.gateway, "remove_gap_listener", None)
        if remove_gap_listener is not None:
            with contextlib.suppress(Exception):
                remove_gap_listener(self._on_gap)

    async def _resume_summary_worker(self) -> None:
        resume = getattr(self.summary_service, "resume_after_recording", None)
        if resume is None:
            return
        with contextlib.suppress(Exception):
            result = resume()
            if asyncio.iscoroutine(result):
                await result
