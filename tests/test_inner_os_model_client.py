from __future__ import annotations

from collections import deque
from uuid import UUID

import pytest

from voice_realtime.inference.scheduler import LocalInferenceScheduler
from voice_realtime.lm_studio import (
    LMStudioOutputLimitError,
    NativeChatCompletion,
    NativeChatRequest,
)
from voice_realtime.meeting.inner_os.context import (
    EvidenceSnapshot,
    InnerOSContextSnapshot,
)
from voice_realtime.meeting.inner_os.model_client import (
    InnerOSModelClient,
    InnerOSModelError,
)

MEETING_ID = UUID("00000000-0000-0000-0000-000000000001")
SEGMENT_ID = UUID("00000000-0000-0000-0000-000000000003")


def _snapshot() -> InnerOSContextSnapshot:
    from datetime import UTC, datetime

    return InnerOSContextSnapshot(
        meeting_id=MEETING_ID,
        transcript_revision=1,
        content_revision=1,
        captured_at=datetime.now(UTC),
        evidence=(
            EvidenceSnapshot(
                alias="S0001",
                segment_id=SEGMENT_ID,
                start_ms=0,
                end_ms=1_000,
                speaker_key="s1",
                speaker_name="发言人 1",
                text="项目将在周五发布。",
                content_hash="a" * 64,
            ),
        ),
        total_segment_count=1,
        included_segment_count=1,
        cropped=False,
        selection_strategy="question_relevance_then_timeline",
    )


def _valid_answer() -> str:
    return (
        '{"intent":"fact","evidence":[],"facts":['
        '{"text":"项目将在周五发布。","evidence_segment_ids":["S0001"]}],'
        '"judgements":[],"draft":null,"limitations":[]}'
    )


class FakeNativeClient:
    def __init__(self, *responses: NativeChatCompletion | BaseException) -> None:
        self.responses = deque(responses)
        self.requests: list[NativeChatRequest] = []
        self.closed = False

    async def complete_chat(
        self,
        request: NativeChatRequest,
        *,
        max_output_chars: int | None = None,
        on_text_delta=None,
    ) -> NativeChatCompletion:
        del max_output_chars, on_text_delta
        self.requests.append(request)
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response

    async def aclose(self) -> None:
        self.closed = True


async def test_model_client_builds_native_request_and_maps_aliases() -> None:
    native = FakeNativeClient(NativeChatCompletion(_valid_answer(), None, {}))
    client = InnerOSModelClient(
        native,
        LocalInferenceScheduler(),
        model="local/kat-coder-2.5",
    )

    answer = await client.generate(
        snapshot=_snapshot(),
        question="什么时候发布？",
        intent="fact",
    )

    request = native.requests[0]
    payload = request.to_payload()
    assert payload["model"] == "local/kat-coder-2.5"
    assert payload["reasoning"] == "off"
    assert payload["stream"] is True
    assert payload["store"] is False
    assert "messages" not in payload
    assert "previous_response_id" not in payload
    assert "S0001" in payload["input"]
    assert answer.facts[0].evidence_segment_ids == (SEGMENT_ID,)
    assert answer.evidence[0].segment_id == SEGMENT_ID


async def test_model_client_repairs_invalid_structure_once() -> None:
    native = FakeNativeClient(
        NativeChatCompletion("not json", None, {}),
        NativeChatCompletion(_valid_answer(), None, {}),
    )
    client = InnerOSModelClient(native, LocalInferenceScheduler(), model="m")

    answer = await client.generate(
        snapshot=_snapshot(), question="什么时候发布？", intent="fact"
    )

    assert answer.facts[0].evidence_segment_ids == (SEGMENT_ID,)
    assert len(native.requests) == 2
    assert native.requests[1].store is False
    assert native.requests[1].reasoning == "off"
    assert "low、medium、high" in (native.requests[1].system_prompt or "")


async def test_model_client_normalizes_plain_string_draft_to_canonical_object() -> None:
    raw = (
        _valid_answer()
        .replace('"intent":"fact"', '"intent":"draft"')
        .replace('"draft":null', '"draft":"项目将在周五发布。"')
    )
    native = FakeNativeClient(NativeChatCompletion(raw, None, {}))
    client = InnerOSModelClient(native, LocalInferenceScheduler(), model="m")

    answer = await client.generate(
        snapshot=_snapshot(), question="起草一段发布说明", intent="draft"
    )

    assert answer.draft is not None
    assert answer.draft.text == "项目将在周五发布。"
    assert len(native.requests) == 1


async def test_model_client_does_not_repair_output_limit() -> None:
    native = FakeNativeClient(LMStudioOutputLimitError("too long"))
    client = InnerOSModelClient(native, LocalInferenceScheduler(), model="m")

    with pytest.raises(InnerOSModelError) as exc_info:
        await client.generate(
            snapshot=_snapshot(), question="什么时候发布？", intent="fact"
        )

    assert exc_info.value.code == "inner_os_output_limit"
    assert len(native.requests) == 1


async def test_model_client_rejects_unknown_evidence_alias_after_one_repair() -> None:
    invalid = _valid_answer().replace("S0001", "S9999")
    native = FakeNativeClient(
        NativeChatCompletion(invalid, None, {}),
        NativeChatCompletion(invalid, None, {}),
    )
    client = InnerOSModelClient(native, LocalInferenceScheduler(), model="m")

    with pytest.raises(InnerOSModelError) as exc_info:
        await client.generate(
            snapshot=_snapshot(), question="什么时候发布？", intent="fact"
        )

    assert exc_info.value.code == "inner_os_invalid_answer"
    assert len(native.requests) == 2


async def test_model_client_close_does_not_close_shared_scheduler() -> None:
    native = FakeNativeClient()
    scheduler = LocalInferenceScheduler()
    client = InnerOSModelClient(native, scheduler, model="m")

    await client.close()

    assert native.closed is True
    assert await scheduler.try_acquire("summary") is True
    scheduler.release()
