"""UIAppContext 组合根：类型暴露与 app.state 同步契约。"""

from __future__ import annotations

from fastapi import FastAPI

from sona.meeting.events import MeetingEventBroadcaster
from sona.ui.app_context import UIAppContext, sync_app_state


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
