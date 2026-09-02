from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID

from sona.meeting.inner_os.contracts import InnerOSAnswer
from sona.meeting.inner_os.service import InnerOSQueryService
from sona.meeting.models import MeetingStatus, NormalizedSegment, TranscriptDocument

MEETING_ID = UUID("00000000-0000-0000-0000-000000000001")
QUERY_ID = UUID("00000000-0000-0000-0000-000000000002")


class FakeRepository:
    async def get_meeting(self, meeting_id: UUID) -> object:
        del meeting_id
        return SimpleNamespace(status=MeetingStatus.RECORDING)

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


class FakeModel:
    model = "m"
    prompt_version = "inner-os-v1"

    async def generate(self, *, snapshot, question, intent, ephemeral_context=None):
        del question, ephemeral_context
        evidence = snapshot.evidence[0]
        return InnerOSAnswer.model_validate(
            {
                "intent": intent,
                "evidence": [
                    {
                        "segment_id": evidence.segment_id,
                        "start_ms": evidence.start_ms,
                        "end_ms": evidence.end_ms,
                        "speaker_key": evidence.speaker_key,
                        "speaker_name": evidence.speaker_name,
                        "text": evidence.text,
                        "content_hash": f"sha256:{evidence.content_hash}",
                    }
                ],
                "facts": [],
                "judgements": [],
                "draft": None,
                "limitations": [],
            }
        )

    async def close(self) -> None:
        return None


class EmptyRepository:
    async def get_meeting(self, meeting_id: UUID) -> object:
        del meeting_id
        return SimpleNamespace(status=MeetingStatus.RECORDING)

    async def get_transcript(self, meeting_id: UUID) -> TranscriptDocument:
        return TranscriptDocument(
            meeting_id=meeting_id,
            transcript_revision=0,
            content_revision=0,
            segments=(),
        )


class NoCallModel:
    model = "m"
    prompt_version = "inner-os-v1"

    def __init__(self) -> None:
        self.called = False

    async def generate(self, **kwargs) -> InnerOSAnswer:
        del kwargs
        self.called = True
        raise AssertionError("empty evidence must not call the model")

    async def close(self) -> None:
        return None


async def test_start_query_preserves_frontend_query_id() -> None:
    service = InnerOSQueryService(FakeRepository(), FakeModel())
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
    client = NoCallModel()
    service = InnerOSQueryService(EmptyRepository(), client)
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
