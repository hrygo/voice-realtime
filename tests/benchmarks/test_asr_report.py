"""Stage 1 Core/Reserve 序贯决策报告器契约测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from voice_realtime.benchmarks.asr.analysis_plan import AnalysisPlan, DecisionFamily
from voice_realtime.benchmarks.asr.report import (
    FamilyLookEvidence,
    evaluate_stage1_look,
)


def _plan(*, meeting_candidates: tuple[str, ...] = ("fun",)) -> AnalysisPlan:
    candidate_ids = ("qwen", "sense", *meeting_candidates)
    registered_ids = tuple(dict.fromkeys(candidate_ids))
    return AnalysisPlan(
        evidence_tier="formal",
        candidate_ids=registered_ids,
        candidate_profile_sha256=dict.fromkeys(registered_ids, "e" * 64),
        core_manifest_sha256="a" * 64,
        reserve_manifest_sha256="b" * 64,
        core_reference_sha256="c" * 64,
        reserve_reference_sha256="d" * 64,
        preflight_report_sha256="f" * 64,
        core_duration_ms=3_600_000,
        reserve_duration_ms=2_700_000,
        core_analysis_cluster_ids=("cluster:core",),
        reserve_analysis_cluster_ids=("cluster:reserve",),
        analysis_cluster_ids=("cluster:core", "cluster:reserve"),
        primary_endpoints=("macro_cer",),
        normalization_version="nfkc-casefold-punct-space-v1",
        filtering_rules=("retain_all_failures",),
        bootstrap_seeds=(2026082501, 2026082502),
        pilot_baseline_cer=0.10,
        decision_families=(
            DecisionFamily(
                family_id="meeting",
                baseline_id="qwen",
                candidate_ids=meeting_candidates,
                pilot_baseline_cer=0.10,
                required_noninferiority_gates=("latency", "failure_rate"),
            ),
            DecisionFamily(
                family_id="interaction",
                baseline_id="sense",
                candidate_ids=("fun",),
                pilot_baseline_cer=0.12,
                required_noninferiority_gates=("echo_safety", "failure_rate"),
            ),
        ),
    )


def _evidence(
    family_id: str,
    baseline_id: str,
    candidate_id: str,
    *,
    look: str = "core",
    mean: float = -0.01,
    ci_low: float = -0.014,
    ci_high: float = -0.006,
    raw_p_value: float = 0.005,
    conditional_power: float | None = 0.80,
    gates: dict[str, str] | None = None,
    hard_failures: tuple[str, ...] = (),
    paired_samples: int = 100,
    expected_paired_samples: int = 100,
    confidence: float | None = None,
    bootstrap_seed: int | None = None,
    cluster_ids: tuple[str, ...] | None = None,
) -> FamilyLookEvidence:
    is_core = look == "core"
    expected_clusters = ("cluster:core",) if is_core else (
        "cluster:core",
        "cluster:reserve",
    )
    return FamilyLookEvidence(
        family_id=family_id,
        baseline_id=baseline_id,
        candidate_id=candidate_id,
        look=look,
        mean_cer_difference=mean,
        ci_low=ci_low,
        ci_high=ci_high,
        raw_p_value=raw_p_value,
        conditional_power=conditional_power,
        noninferiority_gates=gates or {},
        hard_failures=hard_failures,
        paired_samples=paired_samples,
        expected_paired_samples=expected_paired_samples,
        paired_clusters=len(cluster_ids or expected_clusters),
        decision_confidence=confidence or (0.99 if is_core else 0.96),
        bootstrap_seed=bootstrap_seed or (2026082501 if is_core else 2026082502),
        analysis_cluster_ids=cluster_ids or expected_clusters,
        test_direction="two_sided_superiority",
    )


def test_core_look_advances_one_family_and_continues_uncertain_family() -> None:
    report = evaluate_stage1_look(
        _plan(),
        look="core",
        evidence=(
            _evidence(
                "meeting",
                "qwen",
                "fun",
                gates={"latency": "passed", "failure_rate": "passed"},
            ),
            _evidence(
                "interaction",
                "sense",
                "fun",
                mean=-0.002,
                ci_low=-0.009,
                ci_high=0.004,
                raw_p_value=0.20,
                gates={"echo_safety": "passed", "failure_rate": "passed"},
            ),
        ),
    )

    assert report.decisions[0].status == "Advance-Early"
    assert report.decisions[0].selected_candidate_id == "fun"
    assert report.decisions[1].status == "Continue"
    assert "Promote" not in report.model_dump_json()


def test_core_hard_failure_and_futility_are_explicit() -> None:
    report = evaluate_stage1_look(
        _plan(),
        look="core",
        evidence=(
            _evidence(
                "meeting",
                "qwen",
                "fun",
                hard_failures=("network_boundary",),
            ),
            _evidence(
                "interaction",
                "sense",
                "fun",
                mean=0.001,
                ci_low=-0.001,
                ci_high=0.003,
                raw_p_value=0.8,
                conditional_power=0.19,
            ),
        ),
    )

    assert report.decisions[0].status == "Reject-Hard"
    assert report.decisions[1].status == "Reject-Futility"


def test_final_look_only_emits_finalist_after_quality_and_all_required_gates() -> None:
    report = evaluate_stage1_look(
        _plan(),
        look="final",
        evidence=(
            _evidence(
                "meeting",
                "qwen",
                "fun",
                look="final",
                raw_p_value=0.02,
                conditional_power=None,
                gates={"latency": "passed", "failure_rate": "passed"},
            ),
            _evidence(
                "interaction",
                "sense",
                "fun",
                look="final",
                mean=-0.004,
                ci_low=-0.012,
                ci_high=0.003,
                raw_p_value=0.20,
                conditional_power=None,
                gates={"echo_safety": "unsupported", "failure_rate": "passed"},
            ),
        ),
    )

    assert report.decisions[0].status == "Finalist / Reliability Pending"
    assert report.decisions[1].status == "Experimental / No decision"


def test_final_formal_disadvantage_is_reject() -> None:
    report = evaluate_stage1_look(
        _plan(),
        look="final",
        evidence=(
            _evidence(
                "meeting",
                "qwen",
                "fun",
                look="final",
                mean=0.01,
                ci_low=0.004,
                ci_high=0.016,
                raw_p_value=0.01,
                conditional_power=None,
            ),
            _evidence(
                "interaction",
                "sense",
                "fun",
                look="final",
                raw_p_value=0.20,
                conditional_power=None,
            ),
        ),
    )

    assert report.decisions[0].status == "Reject"


def test_family_holm_adjustment_prevents_unadjusted_core_advance() -> None:
    plan = _plan(meeting_candidates=("fun", "other"))
    report = evaluate_stage1_look(
        plan,
        look="core",
        evidence=(
            _evidence(
                "meeting",
                "qwen",
                "fun",
                raw_p_value=0.006,
                gates={"latency": "passed", "failure_rate": "passed"},
            ),
            _evidence(
                "meeting",
                "qwen",
                "other",
                raw_p_value=0.02,
                gates={"latency": "passed", "failure_rate": "passed"},
            ),
            _evidence("interaction", "sense", "fun", raw_p_value=0.50),
        ),
    )

    meeting = report.decisions[0]
    assert meeting.status == "Continue"
    assert [item.holm_adjusted_p_value for item in meeting.candidates] == pytest.approx(
        [0.012, 0.02]
    )


def test_report_rejects_missing_pairs_or_candidate_set_drift() -> None:
    with pytest.raises(ValueError, match="paired sample count"):
        evaluate_stage1_look(
            _plan(),
            look="core",
            evidence=(
                _evidence(
                    "meeting",
                    "qwen",
                    "fun",
                    paired_samples=99,
                    expected_paired_samples=100,
                ),
                _evidence("interaction", "sense", "fun"),
            ),
        )


def test_report_rejects_exploratory_plan() -> None:
    exploratory = _plan().model_copy(update={"evidence_tier": "exploratory"})

    with pytest.raises(ValueError, match="formal analysis plan"):
        evaluate_stage1_look(exploratory, look="core", evidence=())


@pytest.mark.parametrize(
    "updates",
    [
        {"confidence": 0.95},
        {"bootstrap_seed": 999},
        {"cluster_ids": ("cluster:wrong",)},
    ],
)
def test_report_rejects_unregistered_confidence_seed_or_cluster_set(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="registered look identity"):
        evaluate_stage1_look(
            _plan(),
            look="core",
            evidence=(
                _evidence("meeting", "qwen", "fun", **updates),
                _evidence("interaction", "sense", "fun", **updates),
            ),
        )

    with pytest.raises(ValueError, match="fixed family candidate set"):
        evaluate_stage1_look(
            _plan(),
            look="core",
            evidence=(_evidence("meeting", "qwen", "fun"),),
        )


def test_analysis_plan_rejects_family_identity_outside_candidate_set() -> None:
    with pytest.raises(ValidationError):
        AnalysisPlan(
            candidate_ids=("qwen", "sense", "fun"),
            core_manifest_sha256="a" * 64,
            reserve_manifest_sha256="b" * 64,
            core_reference_sha256="c" * 64,
            reserve_reference_sha256="d" * 64,
            bootstrap_seeds=(2026082501, 2026082502),
            pilot_baseline_cer=0.10,
            decision_families=(
                DecisionFamily(
                    family_id="meeting",
                    baseline_id="qwen",
                    candidate_ids=("unknown",),
                    pilot_baseline_cer=0.10,
                ),
            ),
        )
