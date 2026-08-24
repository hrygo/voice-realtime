"""Stage 1 Core/Final 两次查看的确定性决策报告器。"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from voice_realtime.benchmarks.asr.analysis_plan import AnalysisPlan, DecisionFamily
from voice_realtime.benchmarks.asr.manifest import sha256_file

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
    comparison_sha256: str
    gate_metrics_sha256: str
    look: Look
    mean_cer_difference: float
    baseline_macro_cer: float = Field(ge=0)
    candidate_macro_cer: float = Field(ge=0)
    relative_cer_difference: float | None
    bootstrap_standard_error: float = Field(ge=0)
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

    @field_validator(
        "mean_cer_difference",
        "baseline_macro_cer",
        "candidate_macro_cer",
        "relative_cer_difference",
        "bootstrap_standard_error",
        "ci_low",
        "ci_high",
    )
    @classmethod
    def _validate_finite(cls, value: float | None) -> float | None:
        if value is None:
            return None
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

    @field_validator("comparison_sha256", "gate_metrics_sha256")
    @classmethod
    def _validate_comparison_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("comparison SHA-256 must be 64 hexadecimal characters")
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
    analysis_plan_sha256: str | None = None
    evidence_bundle_sha256: str | None = None
    comparison_sha256s: tuple[str, ...] = ()
    gate_metrics_sha256s: tuple[str, ...] = ()
    decisions: tuple[FamilyLookDecision, ...]

    @field_validator(
        "analysis_plan_sha256",
        "evidence_bundle_sha256",
    )
    @classmethod
    def _optional_provenance_hash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return FamilyLookEvidence._validate_comparison_hash(value)

    @field_validator("comparison_sha256s", "gate_metrics_sha256s")
    @classmethod
    def _comparison_hashes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        validated = tuple(
            FamilyLookEvidence._validate_comparison_hash(item) for item in value
        )
        if len(validated) != len(set(validated)):
            raise ValueError("comparison SHA-256 values must be unique")
        return validated


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


class FamilyGateMetricsArtifact(BaseModel):
    """预注册非劣门禁的结构化来源，不允许在 decision evidence 内手填。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    analysis_plan_sha256: str
    look: Look
    family_id: str = Field(min_length=1)
    baseline_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    noninferiority_gates: Mapping[str, GateStatus]
    hard_failures: tuple[str, ...] = ()
    source_artifact_sha256s: Mapping[str, str] = Field(min_length=1)

    @field_validator("analysis_plan_sha256")
    @classmethod
    def _plan_hash(cls, value: str) -> str:
        return FamilyLookEvidence._validate_comparison_hash(value)

    @field_validator("source_artifact_sha256s")
    @classmethod
    def _source_hashes(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        if any(not name.strip() for name in value):
            raise ValueError("gate metric source artifact name cannot be empty")
        normalized = {
            name.strip(): FamilyLookEvidence._validate_comparison_hash(artifact_hash)
            for name, artifact_hash in value.items()
        }
        if len(normalized) != len(value):
            raise ValueError("gate metric source artifact names must be unique")
        return normalized


def load_family_look_evidence(
    path: Path,
    *,
    expected_plan_sha256: str,
    expected_look: Look,
    comparison_paths: Sequence[Path],
    gate_metrics_paths: Sequence[Path],
    gate_source_paths: Mapping[str, Path],
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
    if (
        len(comparison_paths) != len(bundle.comparisons)
        or len(gate_metrics_paths) != len(bundle.comparisons)
    ):
        raise ValueError(
            "each Stage 1 evidence row requires one comparison and gate metrics artifact"
        )
    comparisons_by_identity: dict[tuple[str, str, str], tuple[str, Mapping[str, object]]] = {}
    for comparison_path in comparison_paths:
        if comparison_path.stat().st_size > _MAX_EVIDENCE_BYTES:
            raise ValueError("formal comparison exceeds 16 MiB")
        raw = json.loads(comparison_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("formal comparison must be a JSON object")
        comparison_payload = raw
        identity_values = tuple(
            comparison_payload.get(field)
            for field in ("family_id", "baseline_id", "candidate_id")
        )
        if not all(isinstance(value, str) and value for value in identity_values):
            raise ValueError("formal comparison identity is incomplete")
        identity = (
            str(identity_values[0]),
            str(identity_values[1]),
            str(identity_values[2]),
        )
        if identity in comparisons_by_identity:
            raise ValueError("formal comparison identity must be unique")
        if (
            comparison_payload.get("evidence_tier") != "formal"
            or comparison_payload.get("analysis_plan_sha256") != expected_plan_sha256
            or comparison_payload.get("look") != expected_look
        ):
            raise ValueError("comparison does not match formal analysis plan/look")
        comparisons_by_identity[identity] = (
            sha256_file(comparison_path),
            comparison_payload,
        )
    gates_by_identity: dict[
        tuple[str, str, str], tuple[str, FamilyGateMetricsArtifact]
    ] = {}
    for gate_metrics_path in gate_metrics_paths:
        if gate_metrics_path.stat().st_size > _MAX_EVIDENCE_BYTES:
            raise ValueError("gate metrics artifact exceeds 16 MiB")
        gate_metrics = FamilyGateMetricsArtifact.model_validate_json(
            gate_metrics_path.read_text(encoding="utf-8")
        )
        identity = (
            gate_metrics.family_id,
            gate_metrics.baseline_id,
            gate_metrics.candidate_id,
        )
        if identity in gates_by_identity:
            raise ValueError("gate metrics identity must be unique")
        if (
            gate_metrics.analysis_plan_sha256 != expected_plan_sha256
            or gate_metrics.look != expected_look
        ):
            raise ValueError("gate metrics do not match formal analysis plan/look")
        gates_by_identity[identity] = (sha256_file(gate_metrics_path), gate_metrics)
    expected_gate_sources: dict[str, str] = {}
    for _, gate_metrics in gates_by_identity.values():
        for name, expected_hash in gate_metrics.source_artifact_sha256s.items():
            previous = expected_gate_sources.setdefault(name, expected_hash)
            if previous != expected_hash:
                raise ValueError("gate source name maps to conflicting SHA-256 values")
    if set(gate_source_paths) != set(expected_gate_sources):
        raise ValueError("gate source paths must match the registered source artifact set")
    for name, source_path in gate_source_paths.items():
        if sha256_file(source_path) != expected_gate_sources[name]:
            raise ValueError(f"gate source SHA-256 mismatch: {name}")
    field_pairs = (
        ("mean_cer_difference", "mean_cer_difference"),
        ("baseline_macro_cer", "baseline_macro_cer"),
        ("candidate_macro_cer", "candidate_macro_cer"),
        ("relative_cer_difference", "relative_cer_difference"),
        ("bootstrap_standard_error", "bootstrap_standard_error"),
        ("ci_low", "ci_low"),
        ("ci_high", "ci_high"),
        ("raw_p_value", "raw_p_value"),
        ("conditional_power", "conditional_power"),
        ("paired_samples", "paired_samples"),
        ("paired_clusters", "paired_clusters"),
        ("decision_confidence", "decision_confidence"),
        ("bootstrap_seed", "seed"),
        ("bootstrap_iterations", "bootstrap_iterations"),
        ("analysis_cluster_ids", "analysis_cluster_ids"),
    )
    for evidence in bundle.comparisons:
        identity = (evidence.family_id, evidence.baseline_id, evidence.candidate_id)
        source = comparisons_by_identity.get(identity)
        if source is None:
            raise ValueError("Stage 1 evidence has no matching formal comparison")
        source_hash, source_comparison = source
        if evidence.comparison_sha256 != source_hash:
            raise ValueError("Stage 1 evidence comparison SHA-256 mismatch")
        gate_source = gates_by_identity.get(identity)
        if gate_source is None:
            raise ValueError("Stage 1 evidence has no matching gate metrics artifact")
        gate_source_hash, gate_metrics = gate_source
        if evidence.gate_metrics_sha256 != gate_source_hash:
            raise ValueError("Stage 1 evidence gate metrics SHA-256 mismatch")
        if (
            dict(evidence.noninferiority_gates)
            != dict(gate_metrics.noninferiority_gates)
            or evidence.hard_failures != gate_metrics.hard_failures
        ):
            raise ValueError("Stage 1 gates do not match the gate metrics artifact")
        for evidence_field, comparison_field in field_pairs:
            evidence_value = getattr(evidence, evidence_field)
            comparison_value = source_comparison.get(comparison_field)
            if evidence_field == "analysis_cluster_ids" and isinstance(
                comparison_value, list
            ):
                comparison_value = tuple(comparison_value)
            if evidence_value != comparison_value:
                raise ValueError(
                    f"Stage 1 evidence field does not match comparison: {evidence_field}"
                )
        if evidence.expected_paired_samples != evidence.paired_samples:
            raise ValueError("formal evidence must include the complete paired sample set")
    return bundle.comparisons


def write_stage1_decision_report(
    output: Path,
    report: Stage1DecisionReport,
) -> None:
    """原子写入不可覆盖的 0600 决策报告。"""
    if (
        report.analysis_plan_sha256 is None
        or report.evidence_bundle_sha256 is None
        or not report.comparison_sha256s
        or not report.gate_metrics_sha256s
    ):
        raise ValueError("Stage 1 decision report requires complete provenance hashes")
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
        if any(
            set(item.noninferiority_gates)
            != set(family.required_noninferiority_gates)
            for item in family_evidence
        ):
            raise ValueError("evidence gates do not match the registered family gates")
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
    power_inadequate = (
        plan.final_power is None
        or plan.simulated_familywise_alpha is None
        or plan.final_power <= 0.85
        or plan.simulated_familywise_alpha > 0.05
    )
    if power_inadequate:
        decisions = [
            decision.model_copy(
                update={
                    "status": "Experimental / No decision",
                    "selected_candidate_id": None,
                    "candidates": tuple(
                        candidate.model_copy(
                            update={
                                "advance_eligible": False,
                                "reason_codes": (
                                    *candidate.reason_codes,
                                    "underpowered_design",
                                ),
                            }
                        )
                        for candidate in decision.candidates
                    ),
                }
            )
            for decision in decisions
        ]
    stopped_at: Literal["core", "reserve", "completed"] | None
    if power_inadequate:
        stopped_at = None
    elif look == "final":
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
