"""会议录制领域服务：持久化 confirmed 转录并可靠结束 ASR 会话。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class MeetingPreparation:
    """会议记录与无 PCM capture 已准备完成的一次性凭证。"""

    record: MeetingRecord
    capture: object


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
        self._preparation: MeetingPreparation | None = None
        self._committed_preparation: MeetingPreparation | None = None
        self._listener: WindowListener | None = None
        self._last_window_signature: tuple[Any, ...] | None = None
        self._storage_degraded = False
        self._speaker_names: dict[str, str] = {}

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

    async def prepare_start(
        self, title: str | None = None, max_speakers: int | None = None
    ) -> MeetingPreparation:
        async with self._lock:
            if self._active_meeting_id is not None or self._preparation is not None:
                raise RuntimeError("meeting 已经在录制或准备")
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
                logger.info(
                    "MeetingSession: 准备创建会议 %s (title=%r, lang=%s, max_speakers=%s)",
                    record.id,
                    normalized_title,
                    self.language,
                    max_speakers,
                )
            except MeetingStorageUnavailableError:
                raise
            except Exception as exc:
                raise MeetingStorageUnavailableError(
                    "meeting storage unavailable"
                ) from exc
            self._record = record
            self._last_window_signature = None
            self._storage_degraded = False
            self._speaker_names = {}
            listener = self._on_window
            self._listener = listener
            try:
                self.gateway.add_event_listener(listener)
                add_gap_listener = getattr(self.gateway, "add_gap_listener", None)
                if add_gap_listener is not None:
                    add_gap_listener(self._on_gap)
                capture = await self.gateway.prepare_capture(
                    f"meeting:{record.id}",
                    timeout_secs=5.0,
                    speaker_count_hint=max_speakers,
                )
            except BaseException as exc:
                logger.warning(
                    "MeetingSession: 准备会议失败，执行回滚 (meeting_id=%s): %s",
                    record.id,
                    exc,
                )
                await self._release_listener()
                with contextlib.suppress(Exception):
                    interrupted = await self.repository.set_status(
                        record.id,
                        MeetingStatus.INTERRUPTED,
                        reason="mode_switch_aborted",
                    )
                    self._record = interrupted
                self._preparation = None
                self._active_meeting_id = None
                raise
            preparation = MeetingPreparation(record=record, capture=capture)
            self._preparation = preparation
            return preparation

    def commit_start(self, preparation: MeetingPreparation) -> MeetingRecord:
        self._require_current_preparation(preparation)
        self.gateway.commit_capture(preparation.capture)
        self._active_meeting_id = preparation.record.id
        self._record = preparation.record
        self._preparation = None
        self._committed_preparation = preparation
        logger.info(
            "MeetingSession: 会议录制已启动 (meeting_id=%s, title=%r)",
            preparation.record.id,
            preparation.record.title,
        )
        return preparation.record

    async def publish_started(self, preparation: MeetingPreparation) -> None:
        async with self._lock:
            if (
                self._committed_preparation is not preparation
                or self._active_meeting_id != preparation.record.id
            ):
                raise RuntimeError("无效或已消费的 meeting preparation")
            requeue = getattr(self.summary_service, "requeue_for_recording", None)
            if requeue is not None:
                with contextlib.suppress(Exception):
                    result = requeue()
                    if asyncio.iscoroutine(result):
                        await result
            await self._emit(
                "meeting_state_changed",
                preparation.record.id,
                self._meeting_state_payload(preparation.record),
            )
            self._committed_preparation = None

    async def abort_start(self, preparation: MeetingPreparation) -> None:
        async with self._lock:
            self._require_current_preparation(preparation)
            self._preparation = None
            logger.warning(
                "MeetingSession: 放弃准备会议 (meeting_id=%s)", preparation.record.id
            )
            try:
                with contextlib.suppress(Exception):
                    await self.gateway.abort_prepared_capture(preparation.capture)
            finally:
                await self._release_listener()
                try:
                    record = await self.repository.set_status(
                        preparation.record.id,
                        MeetingStatus.INTERRUPTED,
                        reason="mode_switch_aborted",
                    )
                    self._record = record
                finally:
                    self._active_meeting_id = None
                    self._committed_preparation = None

    async def stop(self) -> MeetingRecord:
        async with self._lock:
            meeting_id = self._active_meeting_id
            if meeting_id is None:
                raise RuntimeError("meeting not active")
            logger.info("MeetingSession: 收到停止会议请求 (meeting_id=%s)", meeting_id)
            capture_closed = False
            try:
                record = await self.repository.set_status(
                    meeting_id, MeetingStatus.FINALIZING
                )
                self._record = record
                await self._emit(
                    "meeting_state_changed",
                    meeting_id,
                    self._meeting_state_payload(record),
                )
                timed_out = False
                timeout_window: TranscriptWindow | None = None
                try:
                    timeout_window = await self.gateway.finish_capture(
                        timeout_secs=self.finalization_timeout_secs
                    )
                    capture_closed = True
                except TimeoutError as exc:
                    timed_out = True
                    capture_closed = True
                    timeout_window = getattr(exc, "last_window", None)
                if timeout_window is not None:
                    await self._persist_window(meeting_id, timeout_window)
                if await self._replay_pending_journal(meeting_id):
                    self._storage_degraded = False

                if timeout_window is not None and timeout_window.speaker_remap:
                    apply_remap = getattr(self.repository, "apply_speaker_remapping", None)
                    if apply_remap is None:
                        raise RuntimeError("meeting repository does not support speaker remapping")
                    record = await apply_remap(meeting_id, dict(timeout_window.speaker_remap))
                    self._record = record

                record = await self.repository.finalize_transcript(
                    meeting_id,
                    final_status=(
                        MeetingStatus.INTERRUPTED if timed_out else MeetingStatus.COMPLETED
                    ),
                    reason="finalization_timeout" if timed_out else None,
                )
                self._record = record
                logger.info(
                    "MeetingSession: 会议录制已封存 (meeting_id=%s, status=%s, timed_out=%s)",
                    meeting_id,
                    record.status.value,
                    timed_out,
                )
                minutes = await self.repository.create_minutes(
                    meeting_id,
                    idempotency_key=f"meeting:{meeting_id}:minutes:v1",
                )
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
            except BaseException:
                failure_cleanup = asyncio.create_task(
                    self._settle_failed_stop(meeting_id, capture_closed=capture_closed)
                )
                await asyncio.shield(failure_cleanup)
                raise
            finally:
                final_cleanup = asyncio.create_task(self._release_stopped_session())
                await asyncio.shield(final_cleanup)

    async def _settle_failed_stop(
        self, meeting_id: UUID, *, capture_closed: bool
    ) -> None:
        if not capture_closed:
            with contextlib.suppress(Exception):
                await self.gateway.abort_capture()
        await self._mark_stop_interrupted(meeting_id)

    async def _release_stopped_session(self) -> None:
        await self._release_listener()
        self._active_meeting_id = None
        self._preparation = None
        self._committed_preparation = None
        self._speaker_names = {}
        await self._resume_summary_worker()

    @staticmethod
    async def _await_cleanup(
        final_cleanup: asyncio.Task[None],
        *,
        initial_cancellation: asyncio.CancelledError | None = None,
    ) -> None:
        """延迟调用方取消；cleanup 失败优先并保留取消为 cause。"""
        cancellation = initial_cancellation
        while not final_cleanup.done():
            try:
                await asyncio.shield(final_cleanup)
            except asyncio.CancelledError as exc:
                if not final_cleanup.cancelled() and cancellation is None:
                    cancellation = exc
            except BaseException:
                break
        try:
            final_cleanup.result()
        except BaseException as cleanup_error:
            if cancellation is not None:
                raise cleanup_error from cancellation
            raise
        if cancellation is not None:
            raise cancellation

    async def _mark_stop_interrupted(self, meeting_id: UUID) -> None:
        """尽力持久化 stop 失败；DB 失败时仍收敛本地稳定状态。"""
        current = self._record
        if current is not None and current.status is MeetingStatus.COMPLETED:
            return
        if current is not None:
            self._record = current.model_copy(
                update={
                    "status": MeetingStatus.INTERRUPTED,
                    "interruption_reason": "meeting_stop_failed",
                }
            )
        try:
            record = await self.repository.set_status(
                meeting_id,
                MeetingStatus.INTERRUPTED,
                reason="meeting_stop_failed",
            )
        except Exception:
            self._storage_degraded = True
        else:
            self._record = record

    async def interrupt(self, reason: str) -> MeetingRecord | None:
        async with self._lock:
            meeting_id = self._active_meeting_id
            if meeting_id is None:
                return self._record
            interrupted_reason = reason[:128]
            logger.warning(
                "MeetingSession: 会议被中断 (meeting_id=%s, reason=%s)",
                meeting_id,
                interrupted_reason,
            )
            initial_cancellation: asyncio.CancelledError | None = None
            result: MeetingRecord | None = None
            try:
                with contextlib.suppress(Exception):
                    await self.gateway.abort_capture()
                current = self._record
                if current is not None:
                    self._record = current.model_copy(
                        update={
                            "status": MeetingStatus.INTERRUPTED,
                            "interruption_reason": interrupted_reason,
                        }
                    )
                try:
                    record = await self.repository.set_status(
                        meeting_id,
                        MeetingStatus.INTERRUPTED,
                        reason=interrupted_reason,
                    )
                except Exception:
                    self._storage_degraded = True
                    raise
                self._record = record
                await self._emit(
                    "meeting_state_changed",
                    meeting_id,
                    self._meeting_state_payload(record),
                )
                result = cast(MeetingRecord, record)
            except asyncio.CancelledError as exc:
                initial_cancellation = exc
            finally:
                final_cleanup = asyncio.create_task(self._release_stopped_session())
                await self._await_cleanup(
                    final_cleanup,
                    initial_cancellation=initial_cancellation,
                )
            if initial_cancellation is not None:
                raise initial_cancellation
            return result

    async def recover_stale(self) -> int:
        recover = getattr(self.repository, "recover_stale", None)
        if recover is None:
            return 0
        result = await recover()
        return int(result or 0)

    async def _replay_pending_journal(self, meeting_id: UUID) -> int:
        journal = self.recovery_journal
        if journal is None:
            return 0
        replay = getattr(journal, "replay_meeting", None)
        if replay is None:
            return 0
        result = replay(self.repository, meeting_id)
        if asyncio.iscoroutine(result):
            result = await result
        count = int(result or 0)
        if count > 0:
            logger.info(
                "MeetingSession: 重放未处理 Journal 记录 (meeting_id=%s, count=%d)",
                meeting_id,
                count,
            )
        return count

    async def _load_speaker_names(self, meeting_id: UUID) -> dict[str, str]:
        """从 Repository 读取自定义说话人名称映射并更新本地缓存。"""
        try:
            get_speakers = getattr(self.repository, "get_speakers", None)
            if get_speakers is None:
                return self._speaker_names
            speakers = await get_speakers(meeting_id)
            names: dict[str, str] = {}
            for spk in speakers:
                key = str(getattr(spk, "speaker_key", ""))
                display = str(getattr(spk, "display_name", ""))
                default = str(getattr(spk, "default_label", ""))
                if key and display and display != default:
                    names[key] = display
            self._speaker_names = names
            return names
        except Exception:
            return self._speaker_names

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
                {
                    "text": window.partial,
                    "speaker_key": window.partial_speaker_key,
                    "speaker_name": self._partial_speaker_name(
                        window, self._speaker_names
                    ),
                },
            )
        if not window.segments:
            return
        result = await self._persist_window(meeting_id, window)
        if result is None:
            return
        speaker_names = await self._load_speaker_names(meeting_id)
        await self._emit(
            "transcript_reconciled",
            meeting_id,
            {
                "transcript_revision": int(getattr(result, "transcript_revision", 0)),
                "content_revision": int(getattr(result, "content_revision", 0)),
                "replace_from_ms": int(getattr(result, "replace_from_ms", 0)),
                "segments": [
                    self._segment_payload(segment, speaker_names)
                    for segment in window.segments
                ],
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
            await self._replay_pending_journal(meeting_id)
            result = await self.repository.reconcile_window(meeting_id, window)
        except Exception as exc:
            self._storage_degraded = True
            logger.warning(
                "MeetingSession: 数据库对账失败，已降级至 RecoveryJournal (meeting_id=%s): %s",
                meeting_id,
                exc,
                exc_info=True,
            )
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
    def _segment_payload(
        segment: Any, speaker_names: dict[str, str] | None = None
    ) -> dict[str, object | None]:
        speaker_key = str(segment.speaker_key)
        payload = segment.model_dump(mode="json")
        if speaker_names and speaker_key in speaker_names:
            payload["speaker_name"] = speaker_names[speaker_key]
        else:
            payload["speaker_name"] = MeetingSession._speaker_name_from_key(speaker_key)
        return cast(dict[str, object | None], payload)

    @staticmethod
    def _speaker_name_from_key(speaker_key: str) -> str:
        raw = speaker_key.rsplit(":", 1)[-1].removeprefix("s")
        return f"说话人 {raw}" if raw.isdigit() else speaker_key

    @classmethod
    def _partial_speaker_name(
        cls,
        window: TranscriptWindow,
        speaker_names: dict[str, str] | None = None,
    ) -> str | None:
        if window.partial_speaker_key is None:
            return window.partial_speaker_name
        if window.partial_speaker_name:
            return window.partial_speaker_name
        speaker_key = window.partial_speaker_key
        if speaker_names and speaker_key in speaker_names:
            return speaker_names[speaker_key]
        raw = speaker_key.rsplit(":", 1)[-1].removeprefix("s")
        return f"说话人 {raw}" if raw.isdigit() else None

    def _require_current_preparation(self, preparation: MeetingPreparation) -> None:
        if self._preparation is not preparation:
            raise RuntimeError("无效或已消费的 meeting preparation")

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
