from pathlib import Path

from voice_realtime.benchmarks.inner_os.dataset import load_dataset

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "inner_os"


def test_loads_three_synthetic_meetings_and_forty_questions() -> None:
    dataset = load_dataset(FIXTURE_ROOT)
    assert {case.meeting_type for case in dataset.meetings} == {
        "product_review",
        "technical_review",
        "requirements_clarification",
    }
    assert len(dataset.questions) == 40
    assert sum(q.expected_insufficient for q in dataset.questions) == 10
    assert all(q.meeting_id in dataset.meeting_ids for q in dataset.questions)


def test_dataset_rejects_sensitive_fields() -> None:
    dataset = load_dataset(FIXTURE_ROOT)
    assert_no_sensitive_fields(dataset)


def assert_no_sensitive_fields(dataset: object) -> None:
    serialized = repr(dataset).lower()
    for forbidden in ("email", "phone", "password", "api_key", "token"):
        assert forbidden not in serialized
