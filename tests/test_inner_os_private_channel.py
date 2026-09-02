from __future__ import annotations

import json
from uuid import UUID

import pytest

from sona.meeting.inner_os.private_channel import (
    InnerOSChannelError,
    InnerOSConnectionSession,
    InnerOSQueryCommand,
    parse_inner_os_command,
)

MEETING_ID = UUID("00000000-0000-0000-0000-000000000001")
QUERY_ID = UUID("00000000-0000-0000-0000-000000000002")


def _query(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "contract_version": "1",
        "request_id": "req-1",
        "cmd": "query",
        "query_id": str(QUERY_ID),
        "meeting_id": str(MEETING_ID),
        "question": "  什么时候发布？  ",
        "intent": "fact",
        "context_version": 1,
        "focus_segment_ids": [],
    }
    payload.update(overrides)
    return payload


def test_query_command_is_strict_and_normalizes_user_text() -> None:
    command = parse_inner_os_command(json.dumps(_query()))

    assert isinstance(command, InnerOSQueryCommand)
    assert command.question == "什么时候发布？"
    assert command.meeting_id == MEETING_ID


@pytest.mark.parametrize(
    "overrides",
    [
        {"contract_version": "2"},
        {"extra": "forbidden"},
        {"request_id": ""},
        {"focus_segment_ids": [str(QUERY_ID), str(QUERY_ID)]},
        {"context_version": -1},
    ],
)
def test_query_command_rejects_contract_drift(overrides: dict[str, object]) -> None:
    with pytest.raises(InnerOSChannelError) as exc_info:
        parse_inner_os_command(json.dumps(_query(**overrides)))

    assert exc_info.value.code == "inner_os_invalid_request"


class FakeService:
    def __init__(self) -> None:
        self.started: list[dict[str, object]] = []
        self.cancelled: list[UUID] = []

    async def start_query(self, **kwargs):
        self.started.append(kwargs)
        await kwargs["emit"](
            "inner_os_query_accepted", kwargs["query_id"], {"status": "accepted"}
        )
        return kwargs["query_id"]

    async def cancel(self, query_id: UUID, **kwargs) -> bool:
        del kwargs
        self.cancelled.append(query_id)
        return True


async def test_connection_session_correlates_ids_and_resets_on_terminal_event() -> None:
    service = FakeService()
    sent: list[dict[str, object]] = []
    session = InnerOSConnectionSession(
        meeting_id=MEETING_ID,
        service=service,
        send=sent.append,
        analysis_enabled=False,
        cancel_timeout_secs=2.0,
    )

    await session.handle_text(json.dumps(_query()))

    assert session.active_query == QUERY_ID
    assert sent[0]["query_id"] == str(QUERY_ID)
    assert sent[0]["request_id"] == "req-1"
    await service.started[0]["emit"](
        "inner_os_answer_completed", QUERY_ID, {"answer": "done"}
    )
    assert session.active_query is None


async def test_connection_session_rejects_second_active_query_and_wrong_meeting() -> None:
    session = InnerOSConnectionSession(
        meeting_id=MEETING_ID,
        service=FakeService(),
        send=lambda event: None,
        analysis_enabled=False,
        cancel_timeout_secs=2.0,
    )
    await session.handle_text(json.dumps(_query()))

    with pytest.raises(InnerOSChannelError) as busy:
        await session.handle_text(json.dumps(_query(request_id="req-2")))
    assert busy.value.code == "inner_os_busy"

    fresh = InnerOSConnectionSession(
        meeting_id=MEETING_ID,
        service=FakeService(),
        send=lambda event: None,
        analysis_enabled=False,
        cancel_timeout_secs=2.0,
    )
    with pytest.raises(InnerOSChannelError) as mismatch:
        await fresh.handle_text(
            json.dumps(_query(meeting_id="00000000-0000-0000-0000-000000000099"))
        )
    assert mismatch.value.code == "inner_os_context_unavailable"


async def test_connection_session_cancel_only_targets_active_query() -> None:
    service = FakeService()
    session = InnerOSConnectionSession(
        meeting_id=MEETING_ID,
        service=service,
        send=lambda event: None,
        analysis_enabled=False,
        cancel_timeout_secs=2.0,
    )
    await session.handle_text(json.dumps(_query()))
    await session.handle_text(
        json.dumps(
            {
                "contract_version": "1",
                "request_id": "req-cancel",
                "cmd": "cancel",
                "query_id": str(QUERY_ID),
            }
        )
    )

    assert service.cancelled == [QUERY_ID]
