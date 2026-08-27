import json
from pathlib import Path

from voice_realtime.benchmarks.inner_os.dataset import load_dataset
from voice_realtime.benchmarks.inner_os.runner import (
    build_payload,
    evaluate_question,
    load_ratings,
    report_for_dataset,
    run_evaluation,
)

ROOT = Path(__file__).parents[1] / "fixtures" / "inner_os"


def test_payload_uses_native_chat_without_transcript_or_response_chain() -> None:
    dataset = load_dataset(ROOT)
    payload = build_payload(
        dataset.questions[0], [dataset.meetings[0].segments[0]], model="m"
    )
    assert payload["model"] == "m"
    assert payload["stream"] is True
    assert payload["store"] is False
    assert "system_prompt" in payload and "input" in payload
    assert "previous_response_id" not in payload
    assert "messages" not in payload


def test_report_is_aggregate_only() -> None:
    report = report_for_dataset(load_dataset(ROOT))
    encoded = json.dumps(report, ensure_ascii=False)
    assert report["question_count"] == 40
    assert "产品评审结论" not in encoded
    assert "11111111-1111-4111-8111-111111111111" not in encoded


async def test_runner_consumes_only_message_deltas() -> None:
    class FakeClient:
        async def stream_chat(self, request):
            from voice_realtime.lm_studio import NativeChatEvent

            yield NativeChatEvent(type="chat.start")
            yield NativeChatEvent(type="reasoning.delta", content="secret")
            yield NativeChatEvent(type="message.delta", content="answer")
            yield NativeChatEvent(type="chat.end", result={"stats": {}})

    dataset = load_dataset(ROOT)
    answer = await evaluate_question(
        FakeClient(), dataset.questions[0], [dataset.meetings[0].segments[0]]
    )
    assert answer == "answer"


async def test_real_evaluation_returns_only_transport_aggregates() -> None:
    class FakeClient:
        async def stream_chat(self, request):
            from voice_realtime.lm_studio import NativeChatEvent

            yield NativeChatEvent(type="message.delta", content="answer")
            yield NativeChatEvent(type="chat.end")

    result = await run_evaluation(FakeClient(), load_dataset(ROOT), model="m")
    assert result["completed_count"] == 40
    assert "answer" not in json.dumps(result)


def test_load_ratings_rejects_content_bearing_fields(tmp_path: Path) -> None:
    path = tmp_path / "ratings.json"
    path.write_text(json.dumps([{"question_id": "q1", "answer": "secret"}]), encoding="utf-8")
    try:
        load_ratings(path)
    except ValueError as exc:
        assert "unknown" in str(exc)
    else:
        raise AssertionError("content-bearing rating field was accepted")
