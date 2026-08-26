from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import BaseModel

from voice_realtime.meeting.summary import (
    MeetingSummaryClient,
    MeetingSummaryService,
    MinutesContent,
    SummaryUnavailableError,
    SummaryValidationError,
    format_transcript,
    parse_summary_output,
    render_minutes_markdown,
    validate_evidence,
)


def _document() -> SimpleNamespace:
    segment_id = UUID("11111111-1111-4111-8111-111111111111")
    return SimpleNamespace(
        meeting_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        transcript_revision=3,
        content_revision=3,
        segments=(
            SimpleNamespace(
                id=segment_id,
                order=0,
                source_epoch=1,
                speaker_key="e1:s1",
                start_ms=1000,
                end_ms=2000,
                text="决定下周一发布，负责人是张三。",
                detected_language="zh",
            ),
        ),
    )


def _multi_document() -> SimpleNamespace:
    first = _document().segments[0]
    second = SimpleNamespace(
        id=UUID("22222222-2222-4222-8222-222222222222"),
        order=1,
        source_epoch=1,
        speaker_key="e1:s1",
        start_ms=3_600_001,
        end_ms=3_601_002,
        text="风险是发布窗口可能延后。",
        detected_language="zh",
    )
    return SimpleNamespace(
        meeting_id=_document().meeting_id,
        transcript_revision=3,
        content_revision=3,
        speakers=(),
        segments=(first, second),
    )


def _content(evidence: UUID) -> MinutesContent:
    return MinutesContent.model_validate(
        {
            "overview": "确定发布计划。",
            "topics": [
                {
                    "title": "发布",
                    "summary": "下周一发布。",
                    "evidence_segment_ids": [str(evidence)],
                }
            ],
            "decisions": [
                {
                    "content": "下周一发布。",
                    "evidence_segment_ids": [str(evidence)],
                }
            ],
            "action_items": [
                {
                    "task": "准备发布",
                    "owner": "张三",
                    "due_date": "下周一",
                    "evidence_segment_ids": [str(evidence)],
                }
            ],
            "risks": [],
            "open_questions": [],
            "highlights": [],
        }
    )


class _Repository:
    def __init__(self, result: object) -> None:
        self.result = result
        self.failed_code: str | None = None
        self.failed_message: str | None = None
        self.failed_raw_output: str | None = None
        self.completed: object | None = None
        self.requeued = False
        self.claimed = 0

    async def claim_minutes(self) -> SimpleNamespace:
        self.claimed += 1
        return SimpleNamespace(
            id=uuid4(),
            meeting_id=_document().meeting_id,
            source_content_revision=3,
            model="test-model",
            prompt_version="v1",
        )

    async def get_transcript(self, _meeting_id: UUID) -> SimpleNamespace:
        return _document()

    async def get_meeting(self, _meeting_id: UUID) -> SimpleNamespace:
        return SimpleNamespace(speakers={})

    async def complete_minutes(self, _minutes_id: UUID, result: object) -> object:
        self.completed = result
        return SimpleNamespace(
            status=SimpleNamespace(value="completed"),
            content_json=result.content_json,
        )

    async def fail_minutes(
        self, _minutes_id: UUID, *, code: str, message: str, raw_output: str | None = None
    ) -> None:
        self.failed_code = code
        self.failed_message = message
        self.failed_raw_output = raw_output

    async def requeue_generating(self) -> None:
        self.requeued = True


class _Client:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[bool] = []

    async def generate(
        self, _document: object, _speakers: object, *, repair: bool = False
    ) -> object:
        self.calls.append(repair)
        return self.result


class _ClosingClient(_Client):
    def __init__(self, result: object) -> None:
        super().__init__(result)
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class _RepairClient(_Client):
    def __init__(self, repaired: object) -> None:
        super().__init__(repaired)
        self.first_result: object = {"overview": 42}

    async def generate(
        self, document: object, speakers: object, *, repair: bool = False
    ) -> object:
        self.calls.append(repair)
        return self.result if repair else self.first_result


class _ReducingClient(_RepairClient):
    def __init__(self, repaired: object, reduced: object) -> None:
        super().__init__(repaired)
        self.reduced = reduced
        self.reduce_calls: list[tuple[object, object, object]] = []

    async def reduce(
        self, results: tuple[MinutesContent, ...], document: object, speakers: object
    ) -> object:
        self.reduce_calls.append((results, document, speakers))
        return self.reduced


class _LegacyClient:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls = 0

    async def generate(self, _document: object, _speakers: object) -> object:
        self.calls += 1
        return self.result


@pytest.mark.asyncio
async def test_summary_rejects_unknown_evidence() -> None:
    unknown = uuid4()
    repository = _Repository(_content(unknown))
    service = MeetingSummaryService(
        repository, _Client(_content(unknown)), settings=SimpleNamespace()
    )

    assert await service.run_once()
    assert repository.failed_code == "invalid_evidence"


@pytest.mark.asyncio
async def test_summary_completes_with_evidence_and_markdown() -> None:
    document = _document()
    repository = _Repository(_content(document.segments[0].id))
    service = MeetingSummaryService(
        repository, _Client(_content(document.segments[0].id)), settings=SimpleNamespace()
    )

    assert await service.run_once()
    assert repository.failed_code is None
    assert repository.completed is not None
    assert "下周一发布" in repository.completed.content_markdown
    assert str(document.segments[0].id) in repository.completed.content_markdown


@pytest.mark.asyncio
async def test_summary_publishes_generating_and_completed_events() -> None:
    document = _document()
    repository = _Repository(_content(document.segments[0].id))
    events: list[tuple[str, UUID, object]] = []

    async def publish(event_type: str, meeting_id: UUID, payload: object) -> None:
        events.append((event_type, meeting_id, payload))

    service = MeetingSummaryService(
        repository,
        _Client(_content(document.segments[0].id)),
        settings=SimpleNamespace(),
        event_publisher=publish,
    )

    assert await service.run_once()
    assert [event[0] for event in events] == [
        "minutes_state_changed",
        "minutes_state_changed",
    ]
    assert [event[2]["status"] for event in events] == ["generating", "completed"]
    completed_minutes = events[-1][2]["minutes"]
    assert completed_minutes is not None
    assert completed_minutes.status.value == "completed"
    assert completed_minutes.content_json.overview == "确定发布计划。"


def test_markdown_renderer_is_deterministic() -> None:
    segment_id = _document().segments[0].id
    markdown = render_minutes_markdown(_content(segment_id))
    assert markdown.startswith("# 会议纪要")
    assert f"[{segment_id}]" in markdown


def test_summary_client_payload_is_native_and_role_free() -> None:
    client = MeetingSummaryClient(model="m", base_url="http://127.0.0.1:1234/v1")
    payload = client._build_payload("instructions", "transcript")
    assert payload["reasoning"] == "off"
    assert payload["stream"] is True
    assert "role" not in payload
    assert "max_tokens" not in payload
    assert payload["system_prompt"] == "instructions"
    assert payload["input"] == "transcript"


@pytest.mark.asyncio
async def test_summary_client_consumes_only_native_message_delta() -> None:
    client = MeetingSummaryClient(model="m", base_url="http://127.0.0.1:1234/v1")

    class _Response:
        def raise_for_status(self) -> None:
            return None

        async def aiter_lines(self) -> object:
            for line in (
                'data: {"type":"chat.start"}',
                'data: {"type":"message.delta","content":"{"}',
                'data: {"type":"message.delta","content":"\\\"overview\\\":\\\"ok\\\"}"}',
                'data: {"type":"message.complete"}',
            ):
                yield line

    @asynccontextmanager
    async def stream(*_args: object, **_kwargs: object) -> object:
        yield _Response()

    client._http.stream = stream  # type: ignore[method-assign]
    result = await client.generate(_document(), ())
    assert result.overview == "ok"


def _stream_client(
    lines: tuple[str, ...], *, error: BaseException | None = None
) -> MeetingSummaryClient:
    client = MeetingSummaryClient(model="m", base_url="http://127.0.0.1:1234/v1")

    class _Response:
        def raise_for_status(self) -> None:
            return None

        async def aiter_lines(self) -> object:
            for line in lines:
                yield line

    @asynccontextmanager
    async def stream(*_args: object, **_kwargs: object) -> object:
        if error is not None:
            raise error
        yield _Response()

    client._http.stream = stream  # type: ignore[method-assign]
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("line", "message"),
    [
        ('data: {"type":"error"}', "请求失败"),
        ('data: {"type":"response.error"}', "请求失败"),
        ('data: {"type":"message.delta","content":42}', "delta 不是文本"),
        ("data: []", "事件格式无效"),
    ],
)
async def test_summary_client_rejects_native_stream_errors(line: str, message: str) -> None:
    client = _stream_client((line,))

    with pytest.raises(SummaryUnavailableError, match=message):
        await client._stream_text({"model": "m"})


@pytest.mark.asyncio
async def test_summary_client_ignores_malformed_non_data_lines_but_requires_content() -> None:
    client = _stream_client(("event: message", "data: not-json", "data: [DONE]"))

    with pytest.raises(SummaryUnavailableError, match="未返回纪要内容"):
        await client._stream_text({"model": "m"})


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx.ConnectError("offline"), OSError("socket closed")])
async def test_summary_client_wraps_transport_errors(error: BaseException) -> None:
    client = _stream_client((), error=error)

    with pytest.raises(SummaryUnavailableError, match="暂不可用"):
        await client._stream_text({"model": "m"})


@pytest.mark.asyncio
async def test_summary_client_wraps_timeout_errors() -> None:
    client = _stream_client((), error=httpx.ReadTimeout("read timed out"))

    with pytest.raises(SummaryUnavailableError, match="超时"):
        await client._stream_text({"model": "m"})


@pytest.mark.asyncio
async def test_summary_client_enforces_total_deadline_during_active_stream() -> None:
    client = MeetingSummaryClient(
        settings=SimpleNamespace(summary_request_timeout_secs=0.01),
        model="m",
        base_url="http://127.0.0.1:1234/v1",
    )

    class _Response:
        def raise_for_status(self) -> None:
            return None

        async def aiter_lines(self) -> object:
            for _ in range(20):
                await asyncio.sleep(0.005)
                yield 'data: {"type":"message.delta","content":"x"}'

    @asynccontextmanager
    async def stream(*_args: object, **_kwargs: object) -> object:
        yield _Response()

    client._http.stream = stream  # type: ignore[method-assign]
    with pytest.raises(SummaryUnavailableError) as exc_info:
        await client._stream_text({"model": "m"})
    assert exc_info.value.code == "summary_timeout"


@pytest.mark.asyncio
async def test_summary_client_stops_degenerate_output_before_server_limit() -> None:
    client = MeetingSummaryClient(
        settings=SimpleNamespace(summary_max_output_chars=8),
        model="m",
        base_url="http://127.0.0.1:1234/v1",
    )
    client._http = _stream_client(
        ('data: {"type":"message.delta","content":"123456789"}',)
    )._http

    with pytest.raises(SummaryUnavailableError) as exc_info:
        await client._stream_text({"model": "m"})
    assert exc_info.value.code == "output_limit"


@pytest.mark.asyncio
async def test_summary_client_uses_compact_segment_refs_and_map_budget() -> None:
    segment_id = _document().segments[0].id
    model_output = {
        "title": "发布计划",
        "overview": "确定发布计划。",
        "topics": [
            {
                "title": "发布",
                "summary": "下周一发布。",
                "evidence_segment_ids": ["S0001"],
            }
        ],
        "decisions": [],
        "action_items": [],
        "risks": [],
        "open_questions": [],
        "highlights": [],
    }
    event = json.dumps(
        {"type": "message.delta", "content": json.dumps(model_output, ensure_ascii=False)},
        ensure_ascii=False,
    )
    client = _stream_client((f"data: {event}",))
    captured: list[dict[str, object]] = []
    original_build = client._build_payload

    def build_payload(
        instructions: str,
        transcript: str,
        *,
        max_output_tokens: int | None = None,
    ) -> dict[str, object]:
        payload = original_build(
            instructions,
            transcript,
            max_output_tokens=max_output_tokens,
        )
        captured.append(payload)
        return payload

    client._build_payload = build_payload  # type: ignore[method-assign]
    result = await client.generate(_document(), ())

    assert result.topics[0].evidence_segment_ids == (segment_id,)
    assert captured[0]["max_output_tokens"] == 2048
    assert "[S0001]" in str(captured[0]["input"])
    assert "SEG:" not in str(captured[0]["input"])


@pytest.mark.asyncio
async def test_summary_client_captures_chat_end_stats_without_content_logging() -> None:
    event = json.dumps(
        {
            "type": "chat.end",
            "result": {
                "output": [{"type": "message", "content": '{"overview":"ok"}'}],
                "stats": {
                    "input_tokens": 120,
                    "total_output_tokens": 18,
                    "reasoning_output_tokens": 0,
                    "tokens_per_second": 91.9,
                },
            },
        }
    )
    client = _stream_client((f"data: {event}",))

    assert await client._stream_text({"model": "m"}) == '{"overview":"ok"}'
    assert client.call_stats[-1]["input_tokens"] == 120
    assert client.call_stats[-1]["total_output_tokens"] == 18
    assert "content" not in client.call_stats[-1]


@pytest.mark.asyncio
async def test_summary_client_rejects_empty_transcript_and_adds_repair_instruction() -> None:
    client = _stream_client(
        (
            'data: {"type":"message.delta","content":"{\\"overview\\":42}"}',
        )
    )
    empty_document = SimpleNamespace(segments=())
    with pytest.raises(SummaryValidationError, match="没有可生成纪要"):
        await client.generate(empty_document, ())

    payloads: list[dict[str, object]] = []
    original_build = client._build_payload

    def build_payload(
        instructions: str,
        transcript: str,
        *,
        max_output_tokens: int | None = None,
    ) -> dict[str, object]:
        payload = original_build(
            instructions,
            transcript,
            max_output_tokens=max_output_tokens,
        )
        payloads.append(payload)
        return payload

    client._build_payload = build_payload  # type: ignore[method-assign]
    with pytest.raises(SummaryValidationError):
        await client.generate(_document(), (), repair=True)
    instructions = str(payloads[0]["system_prompt"])
    assert "只修复 JSON 结构" in instructions
    assert '"evidence_segment_ids"' in instructions
    assert '"task"' in instructions
    assert '"additionalProperties":false' in instructions
    assert "禁止使用 segments" in instructions


@pytest.mark.asyncio
async def test_summary_client_reduce_sends_only_map_results_and_closes_idempotently() -> None:
    client = _stream_client(
        (
            'data: {"type":"message.delta","content":"{\\"overview\\":\\"合并\\",'
            '\\"topics\\":[],\\"decisions\\":[],\\"action_items\\":[],'
            '\\"risks\\":[],\\"open_questions\\":[],\\"highlights\\":[]}"}',
        )
    )
    captured: list[dict[str, object]] = []
    original_build = client._build_payload

    def build_payload(
        instructions: str,
        transcript: str,
        *,
        max_output_tokens: int | None = None,
    ) -> dict[str, object]:
        payload = original_build(
            instructions,
            transcript,
            max_output_tokens=max_output_tokens,
        )
        captured.append(payload)
        return payload

    client._build_payload = build_payload  # type: ignore[method-assign]
    reduced = await client.reduce((_content(_document().segments[0].id),), _document(), ())
    assert reduced.overview == "合并"
    assert len(captured) == 1
    assert "确定发布计划。" in str(captured[0]["input"])
    assert "归并器" in str(captured[0]["system_prompt"])
    assert '"evidence_segment_ids"' in str(captured[0]["system_prompt"])
    assert '"task"' in str(captured[0]["system_prompt"])
    assert "禁止使用 segments" in str(captured[0]["system_prompt"])

    await client.close()
    await client.close()


@pytest.mark.asyncio
async def test_summary_client_reduce_repairs_only_the_invalid_json() -> None:
    client = MeetingSummaryClient(model="m", base_url="http://127.0.0.1:1234/v1")
    invalid = SummaryValidationError("invalid reduce")
    invalid.raw_output = "{invalid-reduce"

    async def stream_text(_payload: object, *, stage: str = "summary") -> str:
        assert stage == "reduce"
        raise invalid

    repair_inputs: list[tuple[str, int | None]] = []

    async def repair_output(
        raw_output: str,
        document: object,
        _speakers: object,
        *,
        max_output_tokens: int | None = None,
    ) -> MinutesContent:
        repair_inputs.append((raw_output, max_output_tokens))
        return _content(document.segments[0].id)

    client._stream_text = stream_text  # type: ignore[method-assign]
    client.repair_output = repair_output  # type: ignore[method-assign]

    result = await client.reduce((_content(_document().segments[0].id),), _document(), ())
    assert result.overview == "确定发布计划。"
    assert repair_inputs == [("{invalid-reduce", 4096)]


def test_invalid_summary_is_a_typed_validation_error() -> None:
    with pytest.raises(SummaryValidationError):
        parse_summary_output({"overview": 42})


def test_summary_parser_accepts_code_fence_and_existing_model() -> None:
    content = _content(_document().segments[0].id)
    fenced = "```json\n" + content.model_dump_json() + "\n```"

    assert parse_summary_output(fenced) == content
    assert parse_summary_output(content) is content


def test_summary_parser_converts_base_model_and_preserves_raw_invalid_output() -> None:
    class _RawSummary(BaseModel):
        overview: str
        topics: list[dict[str, object]]
        decisions: list[dict[str, object]]
        action_items: list[dict[str, object]]
        risks: list[dict[str, object]]
        open_questions: list[dict[str, object]]
        highlights: list[dict[str, object]]

    content = _content(_document().segments[0].id)
    raw_model = _RawSummary.model_validate(content.model_dump(mode="json"))
    assert parse_summary_output(raw_model) == content

    invalid = "{not-json}"
    with pytest.raises(SummaryValidationError) as exc_info:
        parse_summary_output(invalid)
    assert exc_info.value.raw_output == invalid


@pytest.mark.parametrize("raw", [None, [], 42])
def test_summary_parser_rejects_non_json_objects(raw: object) -> None:
    with pytest.raises(SummaryValidationError, match="必须是 JSON 对象"):
        parse_summary_output(raw)


def test_format_transcript_sanitizes_text_and_resolves_speaker_names() -> None:
    document = SimpleNamespace(
        segments=(
            SimpleNamespace(
                id=str(_document().segments[0].id),
                start_ms=-1,
                end_ms=3_661_234,
                speaker_key="e1:s1",
                text="  带空字节\x00的内容  ",
            ),
            SimpleNamespace(
                id=uuid4(),
                start_ms=0,
                end_ms=10,
                speaker_key="e1:s2",
                text="   ",
            ),
        ),
    )
    speakers = (
        SimpleNamespace(speaker_key="e1:s1", display_name="张三", default_label="Speaker 1"),
    )

    rendered = format_transcript(document, speakers)
    assert rendered == (
        f"[SEG:{_document().segments[0].id}][00:00:00.000–01:01:01.234][张三] "
        "带空字节 的内容"
    )
    assert "e1:s2" not in rendered

    mapping_rendered = format_transcript(document, {"e1:s1": {"default_label": "匿名"}})
    assert "[匿名]" in mapping_rendered


def test_validate_evidence_rejects_missing_ids_and_malformed_ids() -> None:
    segment_id = _document().segments[0].id
    missing = MinutesContent.model_validate(
        {
            "overview": "概览",
            "topics": [{"title": "议题", "summary": "内容"}],
        }
    )
    with pytest.raises(SummaryValidationError, match="缺少转录证据: topics"):
        validate_evidence(missing, _document())

    malformed = SimpleNamespace(
        topics=(SimpleNamespace(evidence_segment_ids=["not-a-uuid"]),),
        decisions=(),
        action_items=(),
        risks=(),
        open_questions=(),
        highlights=(),
    )
    with pytest.raises(SummaryValidationError, match="必须是 UUID 数组"):
        validate_evidence(malformed, _document())

    valid = SimpleNamespace(
        topics=(SimpleNamespace(evidence_segment_ids=[str(segment_id)]),),
        decisions=(),
        action_items=(),
        risks=(),
        open_questions=(),
        highlights=(),
    )
    assert validate_evidence(valid, _document()) is valid


def test_validate_evidence_rejects_references_when_document_has_no_segments() -> None:
    result = SimpleNamespace(
        topics=(SimpleNamespace(evidence_segment_ids=[str(uuid4())]),),
        decisions=(),
        action_items=(),
        risks=(),
        open_questions=(),
        highlights=(),
    )
    empty_document = SimpleNamespace(segments=())

    with pytest.raises(SummaryValidationError, match="不存在的转录证据"):
        validate_evidence(result, empty_document)


def test_markdown_renderer_includes_all_sections_and_optional_action_metadata() -> None:
    segment_id = _document().segments[0].id
    content = MinutesContent.model_validate(
        {
            "overview": "  总结  ",
            "topics": [
                {
                    "title": "  议题  ",
                    "summary": "  议题摘要  ",
                    "evidence_segment_ids": [str(segment_id)],
                }
            ],
            "decisions": [{"content": "决策", "evidence_segment_ids": [str(segment_id)]}],
            "action_items": [
                {
                    "task": "准备发布",
                    "owner": "张三",
                    "due_date": "下周一",
                    "evidence_segment_ids": [str(segment_id)],
                },
                {"task": "复核", "evidence_segment_ids": [str(segment_id)]},
            ],
            "risks": [{"content": "风险", "evidence_segment_ids": [str(segment_id)]}],
            "open_questions": [
                {"content": "问题", "evidence_segment_ids": [str(segment_id)]}
            ],
            "highlights": [{"content": "重点", "evidence_segment_ids": [str(segment_id)]}],
        }
    )

    markdown = render_minutes_markdown(content)
    assert markdown == render_minutes_markdown(content)
    assert "## 议题\n\n### 议题\n\n议题摘要" in markdown
    assert "## 决策\n\n- 决策" in markdown
    assert "负责人：张三；截止：下周一" in markdown
    assert "- 复核（" not in markdown
    assert "## 风险" in markdown
    assert "## 待确认问题" in markdown
    assert "## 重点" in markdown
    assert markdown.endswith("\n")


@pytest.mark.asyncio
async def test_summary_worker_repairs_one_invalid_model_response() -> None:
    document = _document()
    repaired = _content(document.segments[0].id)
    repository = _Repository(repaired)
    client = _RepairClient(repaired)
    service = MeetingSummaryService(repository, client, settings=SimpleNamespace())

    assert await service.run_once()
    assert client.calls == [False, True]
    assert repository.failed_code is None
    assert repository.completed is not None
    assert service._active is False


@pytest.mark.asyncio
async def test_summary_worker_repairs_raw_output_without_resending_transcript() -> None:
    content = _content(_document().segments[0].id)

    class _RawRepairClient:
        def __init__(self) -> None:
            self.generate_calls = 0
            self.repair_inputs: list[str] = []

        async def generate(
            self, _document: object, _speakers: object, *, repair: bool = False
        ) -> object:
            del repair
            self.generate_calls += 1
            error = SummaryValidationError("invalid")
            error.raw_output = "{invalid-json"
            raise error

        async def repair_output(
            self, raw_output: str, _document: object, _speakers: object
        ) -> object:
            self.repair_inputs.append(raw_output)
            return content

    repository = _Repository(content)
    client = _RawRepairClient()
    service = MeetingSummaryService(repository, client, settings=SimpleNamespace())

    assert await service.run_once()
    assert client.generate_calls == 1
    assert client.repair_inputs == ["{invalid-json"]
    assert repository.completed is not None


@pytest.mark.asyncio
async def test_summary_worker_enforces_job_deadline_and_persists_timeout() -> None:
    content = _content(_document().segments[0].id)

    class _SlowClient:
        async def generate(
            self, _document: object, _speakers: object, *, repair: bool = False
        ) -> object:
            del repair
            await asyncio.sleep(0.05)
            return content

    repository = _Repository(content)
    service = MeetingSummaryService(
        repository,
        _SlowClient(),
        settings=SimpleNamespace(summary_job_timeout_secs=0.01),
    )

    assert await service.run_once()
    assert repository.failed_code == "summary_timeout"
    assert repository.completed is None


@pytest.mark.asyncio
async def test_summary_worker_supports_legacy_two_argument_client() -> None:
    content = _content(_document().segments[0].id)
    repository = _Repository(content)
    client = _LegacyClient(content)
    service = MeetingSummaryService(repository, client, settings=SimpleNamespace())

    assert await service.run_once()
    assert client.calls == 1
    assert repository.completed is not None


@pytest.mark.asyncio
async def test_summary_worker_persists_schema_failure_and_raw_output() -> None:
    repository = _Repository("{still-invalid")
    client = _RepairClient("{still-invalid")
    service = MeetingSummaryService(repository, client, settings=SimpleNamespace())

    assert await service.run_once()
    assert client.calls == [False, True]
    assert repository.failed_code == "invalid_schema"
    assert repository.failed_raw_output == "{still-invalid"
    assert repository.completed is None


@pytest.mark.asyncio
async def test_summary_worker_handles_unavailable_and_internal_failures() -> None:
    class _UnavailableClient:
        async def generate(
            self, _document: object, _speakers: object, *, repair: bool = False
        ) -> object:
            del repair
            raise SummaryUnavailableError("offline")

    class _InternalClient:
        async def generate(
            self, _document: object, _speakers: object, *, repair: bool = False
        ) -> object:
            del repair
            raise RuntimeError("boom")

    unavailable_repository = _Repository(None)
    unavailable = MeetingSummaryService(
        unavailable_repository, _UnavailableClient(), settings=SimpleNamespace()
    )
    assert await unavailable.run_once()
    assert unavailable_repository.failed_code == "summary_unavailable"
    assert unavailable._active is False

    events: list[dict[str, object]] = []

    async def publish(_event_type: str, _meeting_id: UUID, payload: object) -> None:
        events.append(payload)  # type: ignore[arg-type]

    internal_repository = _Repository(None)
    internal = MeetingSummaryService(
        internal_repository,
        _InternalClient(),
        settings=SimpleNamespace(),
        event_publisher=publish,
    )
    assert await internal.run_once()
    assert internal_repository.failed_code == "internal_error"
    assert events[-1]["status"] == "failed"
    assert events[-1]["error_code"] == "internal_error"


@pytest.mark.asyncio
async def test_summary_worker_returns_false_when_no_job_is_claimed() -> None:
    class _EmptyRepository:
        async def claim_minutes(self) -> None:
            return None

    service = MeetingSummaryService(_EmptyRepository(), _Client(None), settings=SimpleNamespace())

    assert await service.run_once() is False
    assert service._active is False


class _MultiRepository(_Repository):
    async def get_transcript(self, _meeting_id: UUID) -> SimpleNamespace:
        return _multi_document()


@pytest.mark.asyncio
async def test_summary_worker_map_merges_chunks_and_deduplicates_items() -> None:
    content = _content(_document().segments[0].id)
    repository = _MultiRepository(content)
    client = _Client(content)
    settings = SimpleNamespace(summary_max_input_chars=100, summary_chunk_overlap_segments=0)
    service = MeetingSummaryService(repository, client, settings=settings)

    assert await service.run_once()
    assert client.calls == [False, False]
    artifact = repository.completed
    assert artifact is not None
    assert artifact.content_json.overview.count("确定发布计划。") == 2
    assert len(artifact.content_json.topics) == 1


@pytest.mark.asyncio
async def test_summary_worker_map_reduce_uses_reducer_for_chunk_results() -> None:
    mapped = _content(_document().segments[0].id)
    reduced = MinutesContent.model_validate(
        {
            **mapped.model_dump(mode="json"),
            "overview": "归并后的概要",
        }
    )
    repository = _MultiRepository(mapped)
    client = _ReducingClient(mapped, reduced)
    settings = SimpleNamespace(summary_max_input_chars=100, summary_chunk_overlap_segments=0)
    service = MeetingSummaryService(repository, client, settings=settings)

    assert await service.run_once()
    assert client.calls == [False, True, False, True]
    assert len(client.reduce_calls) == 1
    map_results, document, speakers = client.reduce_calls[0]
    assert len(map_results) == 2
    assert document is not None
    assert speakers == ()
    assert repository.completed.content_json.overview == "归并后的概要"


@pytest.mark.asyncio
async def test_summary_worker_lifecycle_is_idempotent_and_closes_client() -> None:
    class _EmptyRepository:
        async def claim_minutes(self) -> None:
            return None

    client = _ClosingClient(None)
    service = MeetingSummaryService(_EmptyRepository(), client, settings=SimpleNamespace())

    await service.start()
    task = service._worker_task
    await service.start()
    assert service._worker_task is task
    await service.stop()
    assert service._worker_task is None
    assert client.closed == 1


@pytest.mark.asyncio
async def test_worker_retries_after_repository_claim_failure() -> None:
    class _FlakyRepository:
        def __init__(self) -> None:
            self.claim_calls = 0

        async def claim_minutes(self) -> None:
            self.claim_calls += 1
            if self.claim_calls == 1:
                raise OSError("postgres unavailable")
            return

    repository = _FlakyRepository()
    service = MeetingSummaryService(repository, _Client(None), settings=SimpleNamespace())

    await service.start()
    await asyncio.sleep(0.55)
    await service.stop()

    assert repository.claim_calls >= 2


@pytest.mark.asyncio
async def test_summary_worker_requeues_with_compatible_repository_method_names() -> None:
    repository = _Repository(None)
    service = MeetingSummaryService(repository, _Client(None), settings=SimpleNamespace())
    await service.requeue_for_recording()
    assert repository.requeued

    class _SyncRequeueRepository:
        def __init__(self) -> None:
            self.called = False

        def requeue_active(self) -> None:
            self.called = True

    alternate = _SyncRequeueRepository()
    service = MeetingSummaryService(alternate, _Client(None), settings=SimpleNamespace())
    await service.requeue_for_recording()
    assert alternate.called


@pytest.mark.asyncio
async def test_summary_worker_survives_event_publisher_failure() -> None:
    content = _content(_document().segments[0].id)
    repository = _Repository(content)

    async def publish(_event_type: str, _meeting_id: UUID, _payload: object) -> None:
        raise RuntimeError("client disconnected")

    service = MeetingSummaryService(
        repository,
        _Client(content),
        settings=SimpleNamespace(),
        event_publisher=publish,
    )

    assert await service.run_once()
    assert repository.completed is not None


def test_markdown_renderer_with_custom_title() -> None:
    segment_id = _document().segments[0].id
    content = _content(segment_id)
    content_with_title = MinutesContent(
        title="2026年Q3产品战略规划研讨",
        overview=content.overview,
        topics=content.topics,
        decisions=content.decisions,
        action_items=content.action_items,
        risks=content.risks,
        open_questions=content.open_questions,
        highlights=content.highlights,
    )
    markdown = render_minutes_markdown(content_with_title)
    assert markdown.startswith("# 会议纪要：2026年Q3产品战略规划研讨")
    assert f"[{segment_id}]" in markdown


@pytest.mark.asyncio
async def test_summary_client_generate_title() -> None:
    class _FakeStreamResponse:
        def __init__(self, text: str) -> None:
            self.status_code = 200
            self._lines = [
                f'data: {{"type":"message.delta","content":"{text}"}}',
                "data: [DONE]",
            ]

        async def __aenter__(self) -> _FakeStreamResponse:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def aiter_lines(self):
            for line in self._lines:
                yield line

    class _FakeHttp:
        def __init__(self, response_text: str) -> None:
            self.response_text = response_text
            self.last_payload: dict[str, object] | None = None

        def stream(self, method: str, url: str, **kwargs: object):
            self.last_payload = kwargs.get("json")  # type: ignore[assignment]
            return _FakeStreamResponse(self.response_text)

    fake_http = _FakeHttp("会议标题：《语音助手与会议纪要架构评审》" + "很长的文字" * 20)
    client = MeetingSummaryClient(
        model="test-model",
        http_client=fake_http,
    )
    title = await client.generate_title(_document())
    assert len(title) <= 64
    assert title.startswith("语音助手与会议纪要架构评审")
    assert not title.startswith("《")
    assert not title.startswith("会议标题：")
    assert fake_http.last_payload is not None
    assert fake_http.last_payload.get("max_output_tokens") == 128



@pytest.mark.asyncio
async def test_summary_worker_auto_updates_default_meeting_title() -> None:
    content = MinutesContent(
        title="语音实时架构评审",
        overview="确认发布计划。",
        topics=(),
        decisions=(),
        action_items=(),
        risks=(),
        open_questions=(),
        highlights=(),
    )

    class _RepositoryWithUpdateTitle(_Repository):
        def __init__(self, result: object) -> None:
            super().__init__(result)
            self.updated_title: str | None = None

        async def claim_minutes(self) -> SimpleNamespace:
            return SimpleNamespace(
                id=uuid4(),
                meeting_id=_document().meeting_id,
                meeting=SimpleNamespace(id=_document().meeting_id, title="会议-20260826-140000"),
                source_content_revision=3,
                model="test-model",
                prompt_version="v1",
            )

        async def update_title(self, meeting_id: UUID, title: str) -> SimpleNamespace:
            self.updated_title = title
            return SimpleNamespace(id=meeting_id, title=title)

    events: list[tuple[str, object]] = []

    async def publish(event_type: str, _meeting_id: UUID, payload: object) -> None:
        events.append((event_type, payload))

    repository = _RepositoryWithUpdateTitle(content)
    service = MeetingSummaryService(
        repository,
        _Client(content),
        settings=SimpleNamespace(),
        event_publisher=publish,
    )

    assert await service.run_once()
    assert repository.updated_title == "语音实时架构评审"
    title_events = [
        payload for event_type, payload in events if event_type == "meeting_title_updated"
    ]
    assert title_events == [{"title": "语音实时架构评审"}]
