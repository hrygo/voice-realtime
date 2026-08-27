from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID

from voice_realtime.meeting.inner_os.service import InnerOSQueryService
from voice_realtime.meeting.inner_os.workload import LocalLLMWorkloadGate
from voice_realtime.meeting.models import NormalizedSegment, TranscriptDocument

MEETING_ID = UUID("00000000-0000-0000-0000-000000000001")
QUERY_ID = UUID("00000000-0000-0000-0000-000000000002")


class FakeRepository:
    async def get_transcript(self, meeting_id: UUID) -> TranscriptDocument:
        return TranscriptDocument(
            meeting_id=meeting_id,
            transcript_revision=1,
            content_revision=1,
            segments=(
                NormalizedSegment(
                    id=UUID("00000000-0000-0000-0000-000000000003"),
                    order=0,
                    source_epoch=0,
                    speaker_key="s1",
                    start_ms=0,
                    end_ms=1000,
                    text="项目将在周五发布。",
                ),
            ),
        )


class FakeClient:
    async def stream_chat(self, request) -> AsyncIterator[object]:
        del request
        yield type(
            "Event",
            (),
            {
                "type": "message.delta",
                "content": (
                    '{"intent":"fact","evidence":[],"facts":[],'
                    '"judgements":[],"draft":null,"limitations":[]}'
                ),
            },
        )()
        yield type("Event", (), {"type": "chat.end", "content": None})()


class EmptyRepository:
    async def get_transcript(self, meeting_id: UUID) -> TranscriptDocument:
        return TranscriptDocument(
            meeting_id=meeting_id,
            transcript_revision=0,
            content_revision=0,
            segments=(),
        )


class NoCallClient:
    def __init__(self) -> None:
        self.called = False

    async def stream_chat(self, request) -> AsyncIterator[object]:
        del request
        self.called = True
        raise AssertionError("empty evidence must not call the model")
        yield  # pragma: no cover


async def test_start_query_preserves_frontend_query_id() -> None:
    service = InnerOSQueryService(
        FakeRepository(), FakeClient(), LocalLLMWorkloadGate()
    )
    events: list[tuple[str, UUID]] = []

    async def emit(event_type: str, query_id: UUID, payload: dict) -> None:
        del payload
        events.append((event_type, query_id))

    query_id = await service.start_query(
        meeting_id=MEETING_ID,
        query_id=QUERY_ID,
        question="发布是什么时候？",
        intent="fact",
        focus_segment_ids=(),
        emit=emit,
    )
    assert query_id == QUERY_ID
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert events[0] == ("inner_os_query_accepted", QUERY_ID)


async def test_empty_evidence_completes_without_waiting_for_model() -> None:
    client = NoCallClient()
    service = InnerOSQueryService(EmptyRepository(), client, LocalLLMWorkloadGate())
    events: list[tuple[str, UUID]] = []
    completed = asyncio.Event()

    async def emit(event_type: str, query_id: UUID, payload: dict) -> None:
        del payload
        events.append((event_type, query_id))
        if event_type == "inner_os_answer_completed":
            completed.set()

    await service.start_query(
        meeting_id=MEETING_ID,
        query_id=QUERY_ID,
        question="当前会议已经确认了哪些内容？",
        intent="fact",
        focus_segment_ids=(),
        emit=emit,
    )
    await asyncio.wait_for(completed.wait(), timeout=1)

    exchange = service.peek_completed(QUERY_ID)
    assert exchange is not None
    answer = exchange["answer"]
    assert answer.limitations[0].code == "insufficient_evidence"
    assert client.called is False
    assert events == [
        ("inner_os_query_accepted", QUERY_ID),
        ("inner_os_answer_started", QUERY_ID),
        ("inner_os_answer_completed", QUERY_ID),
    ]
