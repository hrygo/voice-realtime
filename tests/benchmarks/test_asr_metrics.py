"""ASR 科学对比的基础指标口径测试。"""

from __future__ import annotations

import pytest

from voice_realtime.benchmarks.asr.metrics import (
    MetricStatus,
    TimedText,
    character_error_rate,
    cluster_bootstrap_difference,
    commit_latency,
    hotword_scores,
    normalize_primary_text,
    percentile,
    realtime_factor,
    revision_burden,
    word_error_rate,
)
from voice_realtime.benchmarks.asr.replay import compare_hypotheses, score_hypotheses


def test_cer_reports_substitution_deletion_insertion_counts() -> None:
    result = character_error_rate("你好世界", "你号世呀")

    assert result.status is MetricStatus.SUPPORTED
    assert result.value == pytest.approx(0.5)
    assert result.substitutions == 2
    assert result.deletions == 0
    assert result.insertions == 0
    assert result.reference_tokens == 4


def test_wer_uses_whitespace_tokens() -> None:
    result = word_error_rate("one two three", "one too three now")

    assert result.value == pytest.approx(2 / 3)
    assert result.substitutions == 1
    assert result.insertions == 1


def test_empty_reference_is_not_a_zero_error_rate() -> None:
    result = character_error_rate("", "幻觉")

    assert result.status is MetricStatus.NOT_APPLICABLE
    assert result.value is None


def test_primary_normalization_is_nfkc_casefold_and_punctuation_symmetric() -> None:
    assert normalize_primary_text(" ＱＷＥＮ，你 好！ ") == "qwen你好"


def test_hotword_precision_recall_and_f1_count_occurrences() -> None:
    result = hotword_scores("Qwen Qwen FunASR", "Qwen FunASR FunASR", ("qwen", "funasr"))

    assert result.precision.value == pytest.approx(2 / 3)
    assert result.recall.value == pytest.approx(2 / 3)
    assert result.f1.value == pytest.approx(2 / 3)


def test_revision_burden_counts_adjacent_partial_edits() -> None:
    result = revision_burden(("你", "你好", "您好"), final_text="您好")

    assert result.value == pytest.approx(1.0)


def test_revision_burden_can_be_explicitly_unsupported() -> None:
    result = revision_burden(None, final_text="完成")

    assert result.status is MetricStatus.UNSUPPORTED
    assert result.value is None


def test_commit_latency_uses_first_confirmation_that_never_rolls_back() -> None:
    events = (
        TimedText(arrival_ms=1000, text="世界"),
        TimedText(arrival_ms=1100, text=""),
        TimedText(arrival_ms=1300, text="你好世界"),
        TimedText(arrival_ms=1400, text="你好世界"),
    )

    result = commit_latency("世界", reference_end_ms=900, confirmed=events)

    assert result.value == 400


def test_rtf_and_percentile_have_explicit_missing_semantics() -> None:
    assert realtime_factor(wall_time_ms=250, audio_duration_ms=1000).value == 0.25
    assert percentile((1.0, 2.0, 3.0, 4.0), 95).value == pytest.approx(3.85)
    assert percentile((), 95).status is MetricStatus.MISSING


def test_cluster_bootstrap_difference_is_paired_and_deterministic() -> None:
    baseline = {"a": 0.20, "b": 0.30, "c": 0.40}
    candidate = {"a": 0.10, "b": 0.20, "c": 0.30}

    result = cluster_bootstrap_difference(
        baseline,
        candidate,
        iterations=1000,
        seed=20260824,
    )

    assert result.paired_samples == 3
    assert result.mean_difference == pytest.approx(-0.1)
    assert result.ci_low == pytest.approx(-0.1)
    assert result.ci_high == pytest.approx(-0.1)


def test_summary_macro_cer_weights_scenarios_equally() -> None:
    rows = [
        {
            "sample_id": "clear-1",
            "scenario": "clear",
            "cer_status": "supported",
            "cer": 0.0,
            "S": 0,
            "D": 0,
            "I": 0,
            "N": 10,
            "rtf": 0.1,
            "error_status": None,
        },
        {
            "sample_id": "clear-2",
            "scenario": "clear",
            "cer_status": "supported",
            "cer": 0.0,
            "S": 0,
            "D": 0,
            "I": 0,
            "N": 10,
            "rtf": 0.1,
            "error_status": None,
        },
        {
            "sample_id": "noise-1",
            "scenario": "noise",
            "cer_status": "supported",
            "cer": 1.0,
            "S": 10,
            "D": 0,
            "I": 0,
            "N": 10,
            "rtf": 0.2,
            "error_status": None,
        },
    ]

    summary = score_hypotheses(rows)

    assert summary["sample_macro_cer"] == pytest.approx(1 / 3)
    assert summary["macro_cer"] == pytest.approx(0.5)


def test_comparison_bootstraps_equal_weight_scenario_macro() -> None:
    baseline = [
        {"sample_id": "clear-1", "scenario": "clear", "cer_status": "supported", "cer": 0.0},
        {"sample_id": "clear-2", "scenario": "clear", "cer_status": "supported", "cer": 0.0},
        {"sample_id": "noise-1", "scenario": "noise", "cer_status": "supported", "cer": 0.0},
    ]
    candidate = [
        {"sample_id": "clear-1", "scenario": "clear", "cer_status": "supported", "cer": 0.0},
        {"sample_id": "clear-2", "scenario": "clear", "cer_status": "supported", "cer": 0.0},
        {"sample_id": "noise-1", "scenario": "noise", "cer_status": "supported", "cer": 1.0},
    ]

    comparison = compare_hypotheses(baseline, candidate, iterations=100, seed=7)

    assert comparison["mean_cer_difference"] == pytest.approx(0.5)
    assert comparison["sample_mean_cer_difference"] == pytest.approx(1 / 3)
    assert comparison["ci_low"] == comparison["ci_high"] == pytest.approx(0.5)
