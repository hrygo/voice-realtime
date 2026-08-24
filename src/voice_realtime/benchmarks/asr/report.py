"""Stage 1 Core/Final 两次查看的确定性决策报告器。"""

from __future__ import annotations

import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from voice_realtime.benchmarks.asr.analysis_plan import AnalysisPlan, DecisionFamily

Look = Literal["core", "final"]
GateStatus = Literal["passed", "failed", "unsupported", "not_applicable"]
DecisionStatus = Literal[
    "Advance-Early",
    "Reject-Hard",
    "Reject-Futility",
    "Continue",
    "Finalist / Reliability Pending",
    "Reject",
    "Experimental / No decision",
]
_MAX_EVIDENCE_BYTES = 16 * 1024 * 1024


class FamilyLookEvidence(BaseModel):
    """一个候选相对同族 baseline 的配对查看证据。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    family_id: str = Field(min_length=1)
    baseline_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    look: Look
    mean_cer_difference: float
    ci_low: float
    ci_high: float
    raw_p_value: float = Field(ge=0, le=1)
    conditional_power: float | None = Field(default=None, ge=0, le=1)
    noninferiority_gates: Mapping[str, GateStatus] = Field(default_factory=dict)
    hard_failures: tuple[str, ...] = ()
    paired_samples: int = Field(gt=0)
    expected_paired_samples: int = Field(gt=0)
    paired_clusters: int = Field(gt=0)
    decision_confidence: float = Field(gt=0, lt=1)
    bootstrap_seed: int
    bootstrap_iterations: int = Field(gt=0)
    analysis_cluster_ids: tuple[str, ...] = Field(min_length=1)
    test_direction: Literal["two_sided_superiority"]

    @field_validator("mean_cer_difference", "ci_low", "ci_high")
    @classmethod
    def _validate_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("CER difference and confidence interval must be finite")
        return value

    @field_validator("analysis_cluster_ids")
    @classmethod
    def _validate_clusters(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(cluster_id.strip() for cluster_id in value)
        if any(not cluster_id for cluster_id in normalized):
            raise ValueError("analysis cluster ID cannot be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("analysis cluster IDs must be unique")
        return normalized

    @model_validator(mode="after")
    def _validate_interval(self) -> Self:
        if self.ci_low > self.ci_high:
            raise ValueError("confidence interval lower bound exceeds upper bound")
        return self


class CandidateLookDecision(BaseModel):
    """报告中的候选级校正结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    raw_p_value: float
    holm_adjusted_p_value: float
    advance_eligible: bool
    hard_rejected: bool
    futility_rejected: bool
    required_gates_passed: bool
    reason_codes: tuple[str, ...]


class FamilyLookDecision(BaseModel):
    """一个决策族在当前查看点的唯一状态。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    family_id: str
    baseline_id: str
    status: DecisionStatus
    selected_candidate_id: str | None = None
    candidates: tuple[CandidateLookDecision, ...]


class Stage1DecisionReport(BaseModel):
    """不得产生 Promote 的 Stage 1 决策报告。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    look: Look
    alpha: float
    confidence: float
    stopped_at: Literal["core", "reserve", "completed"] | None
    decisions: tuple[FamilyLookDecision, ...]


class Stage1EvidenceBundle(BaseModel):
    """把 family comparisons 绑定到唯一 analysis plan 与 look。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    analysis_plan_sha256: str
    look: Look
    comparisons: tuple[FamilyLookEvidence, ...] = Field(min_length=1)

    @field_validator("analysis_plan_sha256")
    @classmethod
    def _plan_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("analysis plan SHA-256 must be 64 hexadecimal characters")
        return normalized


def load_family_look_evidence(
    path: Path,
    *,
    expected_plan_sha256: str,
    expected_look: Look,
) -> tuple[FamilyLookEvidence, ...]:
    """有界读取严格 evidence 数组。"""
    if path.stat().st_size > _MAX_EVIDENCE_BYTES:
        raise ValueError("Stage 1 evidence exceeds 16 MiB")
    bundle = Stage1EvidenceBundle.model_validate_json(path.read_text(encoding="utf-8"))
    if (
        bundle.analysis_plan_sha256 != expected_plan_sha256
        or bundle.look != expected_look
    ):
        raise ValueError("Stage 1 evidence bundle does not match analysis plan/look")
    return bundle.comparisons


def write_stage1_decision_report(
    output: Path,
    report: Stage1DecisionReport,
) -> None:
    """原子写入不可覆盖的 0600 决策报告。"""
    if output.exists():
        raise FileExistsError(f"Stage 1 decision report already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(report.model_dump_json(indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(output)
        output.chmod(0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _holm_adjust(raw_p_values: Sequence[float]) -> tuple[float, ...]:
    ordered = sorted(enumerate(raw_p_values), key=lambda item: item[1])
    adjusted = [0.0] * len(raw_p_values)
    previous = 0.0
    count = len(raw_p_values)
    for rank, (index, raw_p_value) in enumerate(ordered):
        current = min(1.0, (count - rank) * raw_p_value)
        previous = max(previous, current)
        adjusted[index] = previous
    return tuple(adjusted)


def _required_gates_passed(
    family: DecisionFamily,
    evidence: FamilyLookEvidence,
) -> bool:
    return all(
        evidence.noninferiority_gates.get(gate) == "passed"
        for gate in family.required_noninferiority_gates
    )


def _has_hard_failure(
    family: DecisionFamily,
    evidence: FamilyLookEvidence,
) -> bool:
    return bool(evidence.hard_failures) or any(
        evidence.noninferiority_gates.get(gate) == "failed"
        for gate in family.required_noninferiority_gates
    )


def _candidate_decisions(
    plan: AnalysisPlan,
    family: DecisionFamily,
    family_evidence: Sequence[FamilyLookEvidence],
    *,
    look: Look,
) -> tuple[CandidateLookDecision, ...]:
    alpha = plan.look_alpha[0 if look == "core" else 1]
    adjusted_p_values = _holm_adjust(
        tuple(item.raw_p_value for item in family_evidence)
    )
    decisions: list[CandidateLookDecision] = []
    for item, adjusted_p_value in zip(
        family_evidence,
        adjusted_p_values,
        strict=True,
    ):
        hard_rejected = _has_hard_failure(family, item)
        gates_passed = _required_gates_passed(family, item)
        advance_eligible = (
            not hard_rejected
            and gates_passed
            and adjusted_p_value <= alpha
            and item.ci_high < -family.minimum_detectable_effect
        )
        futility_rejected = look == "core" and not hard_rejected and (
            (
                item.conditional_power is not None
                and item.conditional_power < plan.conditional_power_futility
            )
            or item.ci_low >= -family.minimum_detectable_effect
        )
        reasons: list[str] = []
        if hard_rejected:
            reasons.append("hard_failure")
        if not gates_passed:
            reasons.append("required_gate_not_passed")
        if futility_rejected:
            reasons.append("futility")
        if advance_eligible:
            reasons.append("quality_boundary_passed")
        decisions.append(
            CandidateLookDecision(
                candidate_id=item.candidate_id,
                raw_p_value=item.raw_p_value,
                holm_adjusted_p_value=adjusted_p_value,
                advance_eligible=advance_eligible,
                hard_rejected=hard_rejected,
                futility_rejected=futility_rejected,
                required_gates_passed=gates_passed,
                reason_codes=tuple(reasons),
            )
        )
    return tuple(decisions)


def _core_family_decision(
    family: DecisionFamily,
    candidates: tuple[CandidateLookDecision, ...],
) -> FamilyLookDecision:
    eligible = tuple(item for item in candidates if item.advance_eligible)
    eliminated = tuple(
        item for item in candidates if item.hard_rejected or item.futility_rejected
    )
    selected: str | None = None
    if len(eligible) == 1 and len(eliminated) == len(candidates) - 1:
        status: DecisionStatus = "Advance-Early"
        selected = eligible[0].candidate_id
    elif all(item.hard_rejected for item in candidates):
        status = "Reject-Hard"
    elif all(item.hard_rejected or item.futility_rejected for item in candidates):
        status = (
            "Reject-Hard"
            if any(item.hard_rejected for item in candidates)
            else "Reject-Futility"
        )
    else:
        status = "Continue"
    return FamilyLookDecision(
        family_id=family.family_id,
        baseline_id=family.baseline_id,
        status=status,
        selected_candidate_id=selected,
        candidates=candidates,
    )


def _final_family_decision(
    family: DecisionFamily,
    family_evidence: Sequence[FamilyLookEvidence],
    candidates: tuple[CandidateLookDecision, ...],
    *,
    alpha: float,
) -> FamilyLookDecision:
    rejected = tuple(
        candidate
        for item, candidate in zip(family_evidence, candidates, strict=True)
        if candidate.hard_rejected
        or (
            candidate.holm_adjusted_p_value <= alpha
            and item.ci_low > 0
        )
    )
    eligible = tuple(item for item in candidates if item.advance_eligible)
    selected: str | None = None
    if len(eligible) == 1 and len(rejected) == len(candidates) - 1:
        status: DecisionStatus = "Finalist / Reliability Pending"
        selected = eligible[0].candidate_id
    elif len(rejected) == len(candidates):
        status = "Reject"
    else:
        status = "Experimental / No decision"
    return FamilyLookDecision(
        family_id=family.family_id,
        baseline_id=family.baseline_id,
        status=status,
        selected_candidate_id=selected,
        candidates=candidates,
    )


def evaluate_stage1_look(
    plan: AnalysisPlan,
    *,
    look: Look,
    evidence: Sequence[FamilyLookEvidence],
) -> Stage1DecisionReport:
    """按预注册 family、Holm 校正和两次查看边界生成报告。"""
    if plan.evidence_tier != "formal":
        raise ValueError("Stage 1 decisions require a formal analysis plan")
    if not plan.decision_families:
        raise ValueError("analysis plan has no registered decision families")
    look_index = 0 if look == "core" else 1
    expected_clusters = (
        plan.core_analysis_cluster_ids
        if look == "core"
        else plan.analysis_cluster_ids
    )
    for item in evidence:
        if item.look != look:
            raise ValueError("evidence look does not match requested look")
        if item.paired_samples != item.expected_paired_samples:
            raise ValueError("paired sample count does not match expected count")
        if (
            item.decision_confidence != plan.decision_confidence[look_index]
            or item.bootstrap_seed != plan.bootstrap_seeds[look_index]
            or item.bootstrap_iterations != plan.bootstrap_iterations
            or item.analysis_cluster_ids != expected_clusters
            or item.paired_clusters != len(expected_clusters)
        ):
            raise ValueError("evidence does not match registered look identity")

    expected_keys = {
        (family.family_id, family.baseline_id, candidate_id)
        for family in plan.decision_families
        for candidate_id in family.candidate_ids
    }
    actual_keys = {
        (item.family_id, item.baseline_id, item.candidate_id) for item in evidence
    }
    if len(actual_keys) != len(evidence) or actual_keys != expected_keys:
        raise ValueError("evidence does not match fixed family candidate set")

    alpha_index = look_index
    decisions: list[FamilyLookDecision] = []
    for family in plan.decision_families:
        by_candidate = {
            item.candidate_id: item
            for item in evidence
            if item.family_id == family.family_id
            and item.baseline_id == family.baseline_id
        }
        family_evidence = tuple(
            by_candidate[candidate_id] for candidate_id in family.candidate_ids
        )
        candidate_decisions = _candidate_decisions(
            plan,
            family,
            family_evidence,
            look=look,
        )
        if look == "core":
            decisions.append(_core_family_decision(family, candidate_decisions))
        else:
            decisions.append(
                _final_family_decision(
                    family,
                    family_evidence,
                    candidate_decisions,
                    alpha=plan.look_alpha[alpha_index],
                )
            )
    stopped_at: Literal["core", "reserve", "completed"] | None
    if look == "final":
        stopped_at = "reserve"
    elif all(decision.status != "Continue" for decision in decisions):
        stopped_at = "core"
    else:
        stopped_at = None
    return Stage1DecisionReport(
        look=look,
        alpha=plan.look_alpha[alpha_index],
        confidence=plan.decision_confidence[alpha_index],
        stopped_at=stopped_at,
        decisions=tuple(decisions),
    )
