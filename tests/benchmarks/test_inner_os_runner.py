import json
from pathlib import Path

from voice_realtime.benchmarks.inner_os.dataset import load_dataset
from voice_realtime.benchmarks.inner_os.runner import build_payload, report_for_dataset

ROOT = Path(__file__).parents[1] / "fixtures" / "inner_os"


def test_payload_uses_native_chat_without_transcript_or_response_chain() -> None:
    dataset = load_dataset(ROOT)
    payload = build_payload(dataset.questions[0], [dataset.meetings[0].segments[0]])
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
