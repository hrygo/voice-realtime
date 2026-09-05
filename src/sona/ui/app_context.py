"""Typed dependency ownership and lifecycle for the UI composition root."""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import FastAPI

from sona.config import Settings
from sona.inference.scheduler import LocalInferenceScheduler
from sona.lm_studio import LMStudioClient
from sona.meeting.diarization_overlay import MeetingDiarizationOverlay
from sona.meeting.diarization_smoother import DiarizationSmoother
from sona.meeting.events import MeetingEventBroadcaster
from sona.meeting.inner_os.model_client import InnerOSModelClient
from sona.meeting.inner_os.repository import InnerOSExchangeRepository
from sona.meeting.inner_os.service import InnerOSQueryService
from sona.meeting.migrations import run_migrations
from sona.meeting.models import StorageHealth
from sona.meeting.ports import MeetingRepository
from sona.meeting.recovery import RecoveryJournal
from sona.meeting.repository import PostgresMeetingRepository
from sona.meeting.runtime_mode import MeetingWorkload
from sona.meeting.session import MeetingSession
from sona.meeting.summary import MeetingSummaryClient, MeetingSummaryService
from sona.speechrail.batch_transcriber import SpeechRailBatchTranscriber
from sona.ui.runtime import UIRuntime

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class UIAppContext:
    settings: Settings
    meeting_events: MeetingEventBroadcaster
    accepted_control_tasks: set[Any]
    runtime: UIRuntime | None = None
    meeting_repository: MeetingRepository | None = None
    meeting_session: MeetingSession | MeetingWorkload | None = None
    meeting_summary_service: MeetingSummaryService | None = None
    inference_scheduler: LocalInferenceScheduler | None = None
    inner_os_service: InnerOSQueryService | None = None
    inner_os_exchange_repository: InnerOSExchangeRepository | None = None
    meeting_backend_error: str | None = None

    async def close(self) -> None:
        runtime = self.runtime
        if runtime is not None:
            await runtime.stop()
            self.runtime = None
        service = self.inner_os_service
        if service is not None:
            await service.close()
            self.inner_os_service = None
        summary = self.meeting_summary_service
        if summary is not None:
            await summary.stop()
            self.meeting_summary_service = None
        scheduler = self.inference_scheduler
        if scheduler is not None:
            await scheduler.close()
            self.inference_scheduler = None
        repository = self.meeting_repository
        if repository is not None:
            await repository.close()
            self.meeting_repository = None


def attach_app_context(app: FastAPI, context: UIAppContext) -> None:
    app.state.sona_context = context


def sync_app_state(app: FastAPI, context: UIAppContext) -> None:
    """将 composition root 依赖镜像到 ``app.state``。

    meeting/api.py 与 inner_os/api.py 的请求处理器仍通过
    ``request.app.state.<key>`` 兼容读取（历史契约），因此服务装配完成后
    必须同步一次，否则 ``/api/v1/meetings`` 等端点会因依赖缺失返回 503。
    """
    app.state.settings = context.settings
    app.state.runtime = context.runtime
    app.state.meeting_runtime = context.runtime
    app.state.meeting_events = context.meeting_events
    app.state.accepted_control_tasks = context.accepted_control_tasks
    app.state.meeting_repository = context.meeting_repository
    app.state.meeting_summary_service = context.meeting_summary_service
    app.state.meeting_session = context.meeting_session
    app.state.inference_scheduler = context.inference_scheduler
    app.state.inner_os_service = context.inner_os_service
    app.state.inner_os_exchange_repository = context.inner_os_exchange_repository
    app.state.meeting_backend_error = context.meeting_backend_error


def get_app_context(app: FastAPI) -> UIAppContext:
    context = getattr(app.state, "sona_context", None)
    if not isinstance(context, UIAppContext):
        raise RuntimeError("UI application context is unavailable")
    return context


async def initialize_meeting_backend(context: UIAppContext) -> bool:
    repository: PostgresMeetingRepository | None = None
    summary_service: MeetingSummaryService | None = None
    scheduler: LocalInferenceScheduler | None = None
    inner_os_service: InnerOSQueryService | None = None
    inner_os_exchange_repository: InnerOSExchangeRepository | None = None
    settings = context.settings
    runtime = context.runtime
    if runtime is None:
        context.meeting_backend_error = "RuntimeUnavailable"
        return False
    try:
        await run_migrations(settings.meeting.database_url, schema=settings.meeting.schema_name)
        repository = PostgresMeetingRepository(settings.meeting)
        await repository.start()
        journal = RecoveryJournal(settings.meeting.recovery_dir)
        await journal.replay(repository)
        await repository.recover_stale()
        scheduler = LocalInferenceScheduler()
        summary_client = MeetingSummaryClient(
            settings.meeting,
            base_url=settings.lm_studio.base_url,
            api_key=settings.lm_studio.api_key,
            scheduler=scheduler,
        )
        summary_service = MeetingSummaryService(
            repository,
            summary_client,
            settings.meeting,
            event_publisher=context.meeting_events.publish_event,
        )
        if settings.meeting.inner_os_enabled:
            inner_os_model = InnerOSModelClient(
                LMStudioClient(
                    base_url=settings.lm_studio.base_url,
                    api_key=settings.lm_studio.api_key,
                ),
                scheduler,
                model=settings.meeting.summary_model,
                max_output_chars=settings.meeting.inner_os_max_output_chars,
                fact_timeout_secs=settings.meeting.inner_os_fact_timeout_secs,
                analysis_timeout_secs=settings.meeting.inner_os_analysis_timeout_secs,
                acquire_timeout_secs=settings.meeting.inner_os_cancel_timeout_secs,
            )
            inner_os_service = InnerOSQueryService(
                repository,
                inner_os_model,
                cache_ttl_secs=settings.meeting.inner_os_cache_ttl_secs,
                cache_max_entries=settings.meeting.inner_os_max_cache_entries,
                cache_max_bytes=settings.meeting.inner_os_max_cache_bytes,
                max_context_chars=settings.meeting.inner_os_max_context_chars,
                recent_context_chars=settings.meeting.inner_os_recent_context_chars,
            )
            # 组合根直接装配交易所仓库，避免请求路径上的惰性构造
            inner_os_exchange_repository = InnerOSExchangeRepository(repository)

        async def publish_meeting_event(
            event_type: str, meeting_id: str | UUID, payload: Any
        ) -> None:
            service = context.inner_os_service
            state = payload.get("status") if isinstance(payload, dict) else None
            if event_type == "meeting_state_changed" and state == "finalizing" and service:
                await service.cancel_meeting(
                    UUID(str(meeting_id)),
                    timeout_secs=settings.meeting.inner_os_cancel_timeout_secs,
                )
            await context.meeting_events.publish_event(event_type, meeting_id, payload)

        diarization_overlay: MeetingDiarizationOverlay | None = None
        if settings.meeting.diarization_overlay_enabled:
            batch_transcriber = SpeechRailBatchTranscriber(
                url=settings.subtitles.speechrail_url,
                api_key=settings.subtitles.speechrail_api_key,
                language=settings.subtitles.language,
            )
            diarization_overlay = MeetingDiarizationOverlay(
                transcriber=batch_transcriber,
                max_buffer_seconds=settings.meeting.diarization_overlay_max_buffer_secs,
            )

        meeting_session = MeetingSession(
            repository,
            runtime.subtitle_proxy,
            summary_service,
            finalization_timeout_secs=settings.meeting.finalization_timeout_secs,
            recovery_journal=journal,
            event_publisher=publish_meeting_event,
            diarization_smoother=DiarizationSmoother(
                enabled=settings.meeting.diarization_smoothing_enabled,
                min_duration_ms=settings.meeting.diarization_min_duration_ms,
                hangover_gap_ms=settings.meeting.diarization_hangover_gap_ms,
            ),
            diarization_overlay=diarization_overlay,
        )
        await summary_service.start()

        # 依赖全部启动成功后再一次性发布到 runtime/context；失败路径不会留下
        # 已关闭的 repository/session，也不会污染下一次初始化。
        runtime.configure_meeting(meeting_session)
        context.meeting_repository = repository
        context.meeting_summary_service = summary_service
        context.inference_scheduler = scheduler
        context.meeting_session = meeting_session
        context.inner_os_service = inner_os_service
        context.inner_os_exchange_repository = inner_os_exchange_repository
        context.meeting_backend_error = None
        return True
    except Exception as exc:
        if inner_os_service is not None:
            with contextlib.suppress(Exception):
                await inner_os_service.close()
        if summary_service is not None:
            with contextlib.suppress(Exception):
                await summary_service.stop()
        if scheduler is not None:
            with contextlib.suppress(Exception):
                await scheduler.close()
        if repository is not None:
            with contextlib.suppress(Exception):
                await repository.close()
        context.meeting_backend_error = type(exc).__name__
        runtime.set_storage_health(StorageHealth.UNAVAILABLE)
        return False
