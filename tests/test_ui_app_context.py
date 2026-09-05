"""UIAppContext 组合根：类型暴露与 app.state 同步契约。"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI

from sona.config import Settings
from sona.meeting.events import MeetingEventBroadcaster
from sona.meeting.models import StorageHealth
from sona.ui.app_context import UIAppContext, initialize_meeting_backend, sync_app_state


def test_context_module_exposes_typed_context() -> None:
    assert UIAppContext.__name__ == "UIAppContext"


def test_sync_app_state_mirrors_context_dependencies_to_app_state() -> None:
    """503 回归测试：会议/内心 OS API 的处理器读取 app.state 旧契约键。"""
    context = UIAppContext(
        settings=object(),  # type: ignore[arg-type]
        meeting_events=MeetingEventBroadcaster(),
        accepted_control_tasks=set(),
    )
    app = FastAPI()
    sync_app_state(app, context)

    assert app.state.settings is context.settings
    assert app.state.runtime is context.runtime
    assert app.state.meeting_runtime is context.runtime
    assert app.state.meeting_events is context.meeting_events
    assert app.state.accepted_control_tasks is context.accepted_control_tasks
    assert app.state.meeting_repository is context.meeting_repository
    assert app.state.meeting_summary_service is context.meeting_summary_service
    assert app.state.meeting_session is context.meeting_session
    assert app.state.inference_scheduler is context.inference_scheduler
    assert app.state.inner_os_service is context.inner_os_service
    assert app.state.inner_os_exchange_repository is context.inner_os_exchange_repository
    assert app.state.meeting_backend_error is context.meeting_backend_error


@pytest.mark.asyncio
async def test_failed_summary_start_does_not_publish_closed_meeting_dependencies() -> None:
    """摘要 worker 启动失败后，下一次初始化仍应能提交完整依赖。"""
    context = UIAppContext(
        settings=Settings(_env_file=None),
        meeting_events=MeetingEventBroadcaster(),
        accepted_control_tasks=set(),
    )
    runtime = Mock()
    runtime.subtitle_proxy = Mock()
    runtime.configure_meeting = Mock()
    runtime.set_storage_health = Mock()
    context.runtime = runtime
    app = FastAPI()

    repository_first = Mock()
    repository_first.start = AsyncMock()
    repository_first.recover_stale = AsyncMock()
    repository_first.close = AsyncMock()
    repository_second = Mock()
    repository_second.start = AsyncMock()
    repository_second.recover_stale = AsyncMock()
    repository_second.close = AsyncMock()

    journal_first = Mock()
    journal_first.replay = AsyncMock()
    journal_second = Mock()
    journal_second.replay = AsyncMock()

    scheduler_first = Mock()
    scheduler_first.close = AsyncMock()
    scheduler_second = Mock()
    scheduler_second.close = AsyncMock()

    summary_first = Mock()
    summary_first.start = AsyncMock(side_effect=RuntimeError("summary unavailable"))
    summary_first.stop = AsyncMock()
    summary_second = Mock()
    summary_second.start = AsyncMock()
    summary_second.stop = AsyncMock()

    session_first = Mock()
    session_second = Mock()

    with (
        patch("sona.ui.app_context.run_migrations", new_callable=AsyncMock),
        patch(
            "sona.ui.app_context.PostgresMeetingRepository",
            side_effect=[repository_first, repository_second],
        ),
        patch("sona.ui.app_context.RecoveryJournal", side_effect=[journal_first, journal_second]),
        patch(
            "sona.ui.app_context.LocalInferenceScheduler",
            side_effect=[scheduler_first, scheduler_second],
        ),
        patch(
            "sona.ui.app_context.MeetingSummaryClient",
            side_effect=[Mock(), Mock()],
        ),
        patch(
            "sona.ui.app_context.MeetingSummaryService",
            side_effect=[summary_first, summary_second],
        ),
        patch("sona.ui.app_context.MeetingSession", side_effect=[session_first, session_second]),
    ):
        assert await initialize_meeting_backend(context) is False

        runtime.configure_meeting.assert_not_called()
        assert context.meeting_repository is None
        assert context.meeting_summary_service is None
        assert context.meeting_session is None
        assert context.inference_scheduler is None
        sync_app_state(app, context)
        assert app.state.meeting_repository is None
        assert app.state.meeting_summary_service is None
        assert app.state.meeting_session is None
        assert app.state.meeting_backend_error == "RuntimeError"
        repository_first.close.assert_awaited_once()
        summary_first.stop.assert_awaited_once()
        scheduler_first.close.assert_awaited_once()
        assert context.meeting_backend_error == "RuntimeError"
        runtime.set_storage_health.assert_called_once_with(StorageHealth.UNAVAILABLE)

        assert await initialize_meeting_backend(context) is True

    runtime.configure_meeting.assert_called_once_with(session_second)
    assert context.meeting_repository is repository_second
    assert context.meeting_summary_service is summary_second
    assert context.meeting_session is session_second
    assert context.inference_scheduler is scheduler_second
    assert context.meeting_backend_error is None
    sync_app_state(app, context)
    assert app.state.meeting_repository is repository_second
    assert app.state.meeting_summary_service is summary_second
    assert app.state.meeting_session is session_second
    summary_second.start.assert_awaited_once()
    repository_second.close.assert_not_awaited()
