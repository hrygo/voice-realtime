from voice_realtime.benchmarks.inner_os.metrics import (
    EvaluationSummary,
    HumanRating,
    summarize_ratings,
)


def test_metrics_are_bounded_and_use_fixed_formulas() -> None:
    ratings = [
        HumanRating(
            question_id="q1",
            evidence_valid=True,
            evidence_covered=True,
            safe_insufficiency=True,
            draft_usable=True,
            usefulness=5,
            unsupported_claim=False,
        ),
        HumanRating(
            question_id="q2",
            evidence_valid=False,
            evidence_covered=False,
            safe_insufficiency=True,
            draft_usable=False,
            usefulness=3,
            unsupported_claim=False,
        ),
    ]
    result = summarize_ratings(ratings, insufficient_count=1, draft_count=1)
    assert isinstance(result, EvaluationSummary)
    assert result.evidence_validity == 0.5
    assert result.evidence_coverage == 1.0
    assert result.safe_insufficiency == 1.0
    assert result.draft_usable == 1.0
    assert result.effective_answer == 0.5


def test_usefulness_must_be_one_to_five() -> None:
    try:
        HumanRating(
            question_id="q1",
            evidence_valid=True,
            evidence_covered=True,
            safe_insufficiency=True,
            draft_usable=True,
            usefulness=6,
            unsupported_claim=False,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("out-of-range usefulness was accepted")
