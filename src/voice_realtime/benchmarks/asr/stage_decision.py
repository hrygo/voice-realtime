"""Derive auditable ASR stage decisions from sealed source artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from pydantic import ConfigDict
from pydantic.dataclasses import dataclass as pydantic_dataclass

from voice_realtime.benchmarks.asr.report import Stage1DecisionReport
from voice_realtime.benchmarks.asr.stage_contracts import (
    PROMOTION_HARD_GATES,
    FaultKind,
    FinalistSelectionEvidence,
    GateStatus,
    PromotionGate,
    StageDecisionReport,
    StageGateEvidenceBundle,
    StageNumber,
    StageRunManifest,
    UpstreamStage,
)
from voice_realtime.benchmarks.asr.stage_evidence import (
    StableFile as _StableFile,
)
from voice_realtime.benchmarks.asr.stage_evidence import (
    StageEvidenceError,
    verify_sealed_run,
    write_stage_decision_report,
)
from voice_realtime.benchmarks.asr.stage_evidence import (
    VerifiedRun as _VerifiedRun,
)
from voice_realtime.benchmarks.asr.stage_evidence import json_lines as _json_lines
from voice_realtime.benchmarks.asr.stage_evidence import json_object as _json_object
from voice_realtime.benchmarks.asr.stage_evidence import normalize_path as _normalize_path
from voice_realtime.benchmarks.asr.stage_evidence import (
    outside_repository as _outside_repository,
)
from voice_realtime.benchmarks.asr.stage_evidence import (
    resolve_repository_root as _repository_root,
)
from voice_realtime.benchmarks.asr.stage_evidence import stable_file as _stable_file

_PROMOTION_DURATION_MS = 3_600_000
_STAGE3_DURATION_MS = 1_800_000
_UPSTREAM_ORDER: tuple[UpstreamStage, ...] = (
    "stage1",
    "stage2",
    "stage3",
    "stage4",
)
_STAGE1_ADVANCEMENT_STATUSES = frozenset(
    {"Advance-Early", "Finalist / Reliability Pending"}
)
_STAGE_ADVANCEMENT_STATUSES = frozenset(
    {"Screen-Pass", "Confirm-Pass", "Finalist / Reliability Pending"}
)
_EXPECTED_UPSTREAM_STATUS: dict[UpstreamStage, str] = {
    "stage2": "Confirm-Pass",
    "stage3": "Finalist / Reliability Pending",
    "stage4": "Confirm-Pass",
}
_FAULT_SEQUENCE: tuple[tuple[str, FaultKind, int, int], ...] = (
    ("d1", "disconnect", 600_000, 0),
    ("d2", "disconnect", 1_200_000, 0),
    ("crash", "asr_crash", 1_800_000, 0),
    ("d3", "disconnect", 2_400_000, 0),
    ("delay", "finalization_delay", 3_600_000, 5_000),
)


@pydantic_dataclass(config=ConfigDict(extra="forbid", frozen=True))
class StageDecisionRequest:
    """Only paths and stage identity are accepted from a decision caller."""

    stage: StageNumber
    family_id: str
    candidate_id: str
    run_dir: Path
    gate_evidence_path: Path
    finalist_selection_path: Path
    upstream_report_paths: Mapping[UpstreamStage, Path]
    output_path: Path
    repository_root: Path

    def __post_init__(self) -> None:
        if type(self.stage) is not int or self.stage not in {2, 3, 4, 5}:
            raise ValueError("stage must be one of 2, 3, 4, or 5")
        for value, label in (
            (self.family_id, "family_id"),
            (self.candidate_id, "candidate_id"),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"{label} must be non-empty text")
        for field_name in (
            "run_dir",
            "gate_evidence_path",
            "finalist_selection_path",
            "output_path",
            "repository_root",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, Path):
                raise ValueError(f"{field_name} must be a path")
            object.__setattr__(self, field_name, _normalize_path(value))
        normalized: dict[UpstreamStage, Path] = {}
        for key, value in self.upstream_report_paths.items():
            if type(key) is not str or key not in _UPSTREAM_ORDER:
                raise ValueError("upstream report keys must be stage1 through stage4")
            if not isinstance(value, Path):
                raise ValueError("upstream report paths must be paths")
            normalized[cast(UpstreamStage, key)] = _normalize_path(value)
        object.__setattr__(self, "upstream_report_paths", MappingProxyType(normalized))
        object.__setattr__(self, "family_id", self.family_id.strip())
        object.__setattr__(self, "candidate_id", self.candidate_id.strip())


@dataclass(frozen=True, slots=True)
class _FaultVerification:
    sha256: str
    counts: Mapping[FaultKind, int]
    valid: bool


@dataclass(frozen=True, slots=True)
class _MetricsVerification:
    sha256: str
    duration_ms: int | None
    wall_elapsed_ms: int | None
    valid: bool


def _required_upstream(stage: StageNumber) -> tuple[UpstreamStage, ...]:
    return _UPSTREAM_ORDER[: stage - 1]


def _validate_stage1_report(
    report: Stage1DecisionReport,
    *,
    family_id: str,
    candidate_id: str,
) -> None:
    families = tuple(item for item in report.decisions if item.family_id == family_id)
    if len(families) != 1:
        raise StageEvidenceError("stage1 family identity is missing or ambiguous")
    family = families[0]
    if family.selected_candidate_id != candidate_id:
        raise StageEvidenceError("stage1 selected finalist mismatch")
    candidate_ids = tuple(item.candidate_id for item in family.candidates)
    if len(candidate_ids) != len(set(candidate_ids)):
        raise StageEvidenceError("stage1 candidate identity is ambiguous")
    selected = tuple(item for item in family.candidates if item.candidate_id == candidate_id)
    eligible = tuple(
        item
        for item in family.candidates
        if type(item.advance_eligible) is bool
        and type(item.hard_rejected) is bool
        and type(item.futility_rejected) is bool
        and type(item.required_gates_passed) is bool
        and item.advance_eligible
        and not item.hard_rejected
        and not item.futility_rejected
        and item.required_gates_passed
    )
    if len(selected) != 1 or len(eligible) != 1 or eligible[0].candidate_id != candidate_id:
        raise StageEvidenceError("stage1 candidate is not uniquely advance-eligible")
    if family.status not in _STAGE1_ADVANCEMENT_STATUSES:
        raise StageEvidenceError("stage1 candidate is not eligible for this stage")


def _load_upstream(
    request: StageDecisionRequest,
    repository: Path,
    *,
    run: _VerifiedRun,
) -> Mapping[UpstreamStage, _StableFile]:
    required = _required_upstream(request.stage)
    missing = [stage for stage in required if stage not in request.upstream_report_paths]
    if missing:
        raise StageEvidenceError(f"missing upstream report: {missing[0]}")
    files: dict[UpstreamStage, _StableFile] = {}
    for stage in required:
        path = request.upstream_report_paths[stage]
        _outside_repository(path, repository, label=f"{stage} report")
        stable = _stable_file(path, label=f"{stage} report")
        files[stage] = stable
        if stage == "stage1":
            try:
                report = Stage1DecisionReport.model_validate_json(stable.raw)
            except ValueError as exc:
                raise StageEvidenceError("stage1 report is invalid") from exc
            _validate_stage1_report(
                report,
                family_id=request.family_id,
                candidate_id=request.candidate_id,
            )
            continue
        try:
            stage_report = StageDecisionReport.model_validate_json(stable.raw)
        except ValueError as exc:
            raise StageEvidenceError(f"{stage} report is invalid") from exc
        expected_stage = int(stage[-1])
        if stage_report.stage != expected_stage:
            raise StageEvidenceError(f"{stage} report stage identity mismatch")
        if (
            stage_report.family_id != request.family_id
            or stage_report.candidate_id != request.candidate_id
        ):
            raise StageEvidenceError(f"{stage} report family/candidate mismatch")
        if (
            request.stage == 5
            and stage == "stage3"
            and stage_report.run_manifest_sha256 != run.manifest_file.sha256
        ):
            raise StageEvidenceError(f"{stage} report run identity mismatch")
        if stage_report.status != _EXPECTED_UPSTREAM_STATUS[stage]:
            raise StageEvidenceError(f"{stage} report status order mismatch")
        expected_previous = _UPSTREAM_ORDER[: expected_stage - 1]
        if tuple(stage_report.upstream_report_sha256s) != expected_previous:
            raise StageEvidenceError(f"{stage} report upstream order mismatch")
        for previous in expected_previous:
            previous_file = files.get(previous)
            if previous_file is None:
                raise StageEvidenceError(f"{stage} report upstream chain is incomplete")
            if stage_report.upstream_report_sha256s[previous] != previous_file.sha256:
                raise StageEvidenceError(f"{stage} report upstream hash mismatch")
    return MappingProxyType(files)


def _validate_run_upstream_binding(
    manifest: StageRunManifest,
    upstream_sha256s: Mapping[UpstreamStage, str],
) -> None:
    """新式 formal manifest 必须与决策阶段重新读取的上游证据完全一致。"""

    if manifest.evidence_tier != "formal" or manifest.input_manifest_sha256 is None:
        return
    if manifest.eligibility_sha256 is None:
        raise StageEvidenceError("formal run eligibility identity is missing")
    bound = {
        stage: manifest.upstream_report_sha256s.get(stage)
        for stage in upstream_sha256s
    }
    if bound != upstream_sha256s:
        raise StageEvidenceError("run manifest upstream identity mismatch")


def _load_gate_evidence(
    request: StageDecisionRequest,
    repository: Path,
    run: _VerifiedRun,
    upstream_files: Mapping[UpstreamStage, _StableFile],
) -> Mapping[PromotionGate, GateStatus]:
    _outside_repository(request.gate_evidence_path, repository, label="gate evidence")
    stable = _stable_file(request.gate_evidence_path, label="gate evidence")
    try:
        bundle = StageGateEvidenceBundle.model_validate_json(stable.raw)
    except ValueError as exc:
        raise StageEvidenceError("gate evidence is invalid") from exc
    if bundle.family_id != request.family_id or bundle.candidate_id != request.candidate_id:
        raise StageEvidenceError("gate evidence family/candidate mismatch")
    reusable_stage5 = request.stage == 3 and bundle.stage == 5
    if bundle.stage != request.stage and not reusable_stage5:
        raise StageEvidenceError("gate evidence stage mismatch")
    allowed_hashes = set(run.artifact_hashes.values())
    allowed_hashes.update(item.sha256 for item in upstream_files.values())
    if request.stage == 3:
        stage3_artifacts = {
            path: run.artifact_files.get(path)
            for path in ("checkpoints/stage3.json", "metrics-stage3.json")
        }
        if any(artifact is None for artifact in stage3_artifacts.values()):
            raise StageEvidenceError("Stage 3 gate sources require the sealed slice")
        allowed_hashes = {
            artifact.sha256
            for artifact in stage3_artifacts.values()
            if artifact is not None
        }
        allowed_hashes.update(item.sha256 for item in upstream_files.values())
    for gate in PROMOTION_HARD_GATES:
        if any(
            source_hash not in allowed_hashes
            for source_hash in bundle.source_artifact_sha256s[gate]
        ):
            raise StageEvidenceError(f"unknown gate source hash: {gate}")
    if request.stage in {2, 3, 4} and not reusable_stage5:
        if any(
            bundle.gates[gate] not in {"not_applicable", "unsupported"}
            for gate in PROMOTION_HARD_GATES
        ):
            raise StageEvidenceError("unmeasured stage gates must be not_applicable or unsupported")
        return MappingProxyType(dict(bundle.gates))
    if request.stage in {2, 3, 4}:
        return MappingProxyType(dict.fromkeys(PROMOTION_HARD_GATES, "not_applicable"))
    return MappingProxyType(dict(bundle.gates))


def _load_selection(
    request: StageDecisionRequest,
    repository: Path,
    upstream_files: Mapping[UpstreamStage, _StableFile],
) -> FinalistSelectionEvidence:
    _outside_repository(request.finalist_selection_path, repository, label="finalist selection")
    stable = _stable_file(request.finalist_selection_path, label="finalist selection")
    try:
        selection = FinalistSelectionEvidence.model_validate_json(stable.raw)
    except ValueError as exc:
        raise StageEvidenceError("finalist selection is invalid") from exc
    if selection.family_id != request.family_id:
        raise StageEvidenceError("finalist selection family mismatch")
    if selection.selected_candidate_id != request.candidate_id:
        raise StageEvidenceError("finalist selection candidate mismatch")
    if (
        len(selection.eligible_candidate_ids) != 1
        or selection.eligible_candidate_ids[0] != request.candidate_id
    ):
        raise StageEvidenceError("selection does not establish a unique finalist")
    expected = {stage: item.sha256 for stage, item in upstream_files.items()}
    if tuple(selection.upstream_report_sha256s) != _UPSTREAM_ORDER:
        raise StageEvidenceError("finalist selection upstream chain is incomplete")
    if selection.upstream_report_sha256s != expected:
        raise StageEvidenceError("finalist selection upstream hash mismatch")
    return selection


def _strict_int(value: object) -> int | None:
    return value if type(value) is int else None


def _load_metrics(
    run: _VerifiedRun,
    *,
    artifact_name: str,
    expected_duration: int | None = None,
    expected_elapsed: int | None = None,
) -> _MetricsVerification:
    stable = run.artifact_files.get(artifact_name)
    if stable is None:
        raise StageEvidenceError(f"{artifact_name} is missing from artifact index")
    payload = _json_object(stable, label=artifact_name)
    duration = _strict_int(payload.get("canonical_audio_duration_ms"))
    elapsed = _strict_int(payload.get("monotonic_wall_elapsed_ms"))
    if duration is None:
        raise StageEvidenceError(f"{artifact_name} duration must be an integer")
    if expected_elapsed is not None and elapsed is None:
        raise StageEvidenceError(f"{artifact_name} wall elapsed must be an integer")
    valid = duration > 0
    if expected_duration is not None:
        valid = valid and duration == expected_duration
    if expected_elapsed is not None:
        valid = valid and elapsed is not None and elapsed >= expected_elapsed
    return _MetricsVerification(stable.sha256, duration, elapsed, valid)


def _load_stage3_metrics(run: _VerifiedRun) -> _MetricsVerification:
    checkpoint = run.artifact_files.get("checkpoints/stage3.json")
    metrics = run.artifact_files.get("metrics-stage3.json")
    if checkpoint is None or metrics is None:
        raise StageEvidenceError("Stage 3 checkpoint and metrics slice are required")
    checkpoint_payload = _json_object(checkpoint, label="Stage 3 checkpoint")
    metrics_payload = _json_object(metrics, label="Stage 3 metrics")
    expected_window = {"start_ms": 0, "end_ms": _STAGE3_DURATION_MS}
    checkpoint_window = checkpoint_payload.get("window")
    metrics_window = metrics_payload.get("window")
    if (
        type(checkpoint_payload.get("stage")) is not int
        or type(metrics_payload.get("stage")) is not int
        or type(checkpoint_payload.get("cursor_ms")) is not int
        or not isinstance(checkpoint_window, dict)
        or not isinstance(metrics_window, dict)
        or any(
            type(window.get(key)) is not int
            for window in (checkpoint_window, metrics_window)
            for key in ("start_ms", "end_ms")
        )
        or checkpoint_payload.get("stage") != 3
        or metrics_payload.get("stage") != 3
        or checkpoint_payload.get("cursor_ms") != _STAGE3_DURATION_MS
        or checkpoint_window != expected_window
        or metrics_window != expected_window
    ):
        raise StageEvidenceError("Stage 3 checkpoint window mismatch")
    duration = _strict_int(metrics_payload.get("canonical_audio_duration_ms"))
    if duration is None:
        raise StageEvidenceError("Stage 3 metrics duration must be an integer")
    return _MetricsVerification(metrics.sha256, duration, None, duration == _STAGE3_DURATION_MS)


def _fault_identity(row: Mapping[str, Any]) -> tuple[str, FaultKind, int, int] | None:
    event_id = row.get("event_id")
    kind = row.get("kind")
    cursor = row.get("planned_cursor_ms")
    actual = row.get("actual_cursor_ms")
    duration = row.get("duration_ms")
    if (
        type(event_id) is not str
        or not event_id
        or kind not in {"disconnect", "asr_crash", "finalization_delay"}
        or type(cursor) is not int
        or type(actual) is not int
        or type(duration) is not int
        or duration < 0
        or cursor != actual
    ):
        return None
    return event_id, cast(FaultKind, kind), cursor, duration


def _verify_fault_execution(run: _VerifiedRun) -> _FaultVerification:
    stable = run.artifact_files.get("fault-execution.jsonl")
    if stable is None:
        raise StageEvidenceError("fault execution evidence is missing")
    rows = _json_lines(stable, label="fault execution")
    counts: dict[FaultKind, int] = {
        "disconnect": 0,
        "asr_crash": 0,
        "finalization_delay": 0,
    }
    valid = len(rows) == 20
    groups: list[tuple[str, FaultKind, int, int, tuple[Mapping[str, Any], ...]]] = []
    for offset in range(0, len(rows), 4):
        group = rows[offset : offset + 4]
        if not group:
            continue
        if len(group) != 4:
            raise StageEvidenceError("fault lifecycle event chain is incomplete")
        identities = tuple(_fault_identity(row) for row in group)
        if any(identity is None for identity in identities):
            raise StageEvidenceError("fault identity/cursor/duration is invalid")
        first = cast(tuple[str, FaultKind, int, int], identities[0])
        if any(identity != first for identity in identities[1:]):
            raise StageEvidenceError("fault identity differs inside event chain")
        states = tuple(row.get("state") for row in group)
        if states[:3] != ("planned", "attempt_started", "applied") or states[3] not in {
            "recovered",
            "failed",
            "unknown",
        }:
            raise StageEvidenceError("fault lifecycle state sequence is invalid")
        terminal = cast(str, states[3])
        observations = tuple(row.get("observation_available") for row in group)
        if any(type(value) is not bool for value in observations):
            raise StageEvidenceError("fault observation_available must be boolean")
        if observations[:2] != (True, True):
            raise StageEvidenceError("fault pre-injection observation is unavailable")
        if observations[2:] not in {(True, True), (False, False)}:
            raise StageEvidenceError("fault terminal observation availability is inconsistent")
        terminal_observation_available = observations[2:] == (True, True)
        if not terminal_observation_available:
            valid = False
        applied_outcome = group[2].get("outcome")
        terminal_outcome = group[3].get("outcome")
        if (
            applied_outcome not in {"recovered", "failed", "unknown"}
            or terminal_outcome != terminal
        ):
            raise StageEvidenceError("fault outcome identity is invalid")
        if not terminal_observation_available and (
            applied_outcome != "unknown" or terminal != "unknown"
        ):
            raise StageEvidenceError("unavailable fault observation must remain unknown")
        if applied_outcome != terminal:
            valid = False
        before_ids = tuple(row.get("session_id_before") for row in group)
        before_epochs = tuple(row.get("source_epoch_before") for row in group)
        after_ids = tuple(row.get("session_id_after") for row in group[2:])
        after_epochs = tuple(row.get("source_epoch_after") for row in group[2:])
        before_epoch_values = cast(tuple[int, ...], before_epochs)
        if (
            any(type(value) is not str or not value for value in before_ids)
            or any(type(value) is not int for value in before_epochs)
            or before_ids[:2] != before_ids[2:]
            or before_epochs[:2] != before_epochs[2:]
        ):
            raise StageEvidenceError("fault session/source identity is invalid")
        if terminal_observation_available:
            after_epoch_values = cast(tuple[int, ...], after_epochs)
            if (
                any(type(value) is not str or not value for value in after_ids)
                or any(type(value) is not int for value in after_epochs)
                or after_ids[0] != after_ids[1]
                or after_epochs[0] != after_epochs[1]
                or any(
                    after < before
                    for before, after in zip(
                        before_epoch_values[2:], after_epoch_values, strict=True
                    )
                )
            ):
                raise StageEvidenceError("fault session/source identity is invalid")
        elif any(value is not None for value in (*after_ids, *after_epochs)):
            raise StageEvidenceError("unavailable fault observation must omit after identity")
        expected = next((item for item in _FAULT_SEQUENCE if item[0] == first[0]), None)
        if expected is None:
            valid = False
        elif first[1:] != expected[1:]:
            raise StageEvidenceError("fault does not match the preregistered sequence")
        groups.append((*first, group))
        if terminal == "recovered":
            counts[first[1]] += 1

    if len(groups) != len(_FAULT_SEQUENCE):
        valid = False
    else:
        identities = tuple(group[:4] for group in groups)
        expected_identities = _FAULT_SEQUENCE
        if identities != expected_identities:
            valid = False
        for previous, current in pairwise(groups):
            previous_after_id = previous[4][3].get("session_id_after")
            previous_after_epoch = previous[4][3].get("source_epoch_after")
            current_before_id = current[4][0].get("session_id_before")
            current_before_epoch = current[4][0].get("source_epoch_before")
            if previous_after_id is None or previous_after_epoch is None:
                valid = False
            elif (previous_after_id, previous_after_epoch) != (
                current_before_id,
                current_before_epoch,
            ):
                raise StageEvidenceError("fault session/source continuity is broken")
    if counts != {"disconnect": 3, "asr_crash": 1, "finalization_delay": 1}:
        valid = False
    return _FaultVerification(stable.sha256, MappingProxyType(counts), valid)


def _stage5_status(
    run: _VerifiedRun,
    *,
    gates: Mapping[PromotionGate, GateStatus],
    metrics: _MetricsVerification,
    faults: _FaultVerification,
) -> str:
    if run.state.status == "deferred":
        return "deferred"
    if run.state.status == "failed":
        return "Reject"
    promotable = (
        run.manifest.evidence_tier == "formal"
        and run.state.status == "completed"
        and run.state.stop_reason == "schedule_complete"
        and all(gates[gate] == "passed" for gate in PROMOTION_HARD_GATES)
        and metrics.valid
        and run.state.cursor_ms == _PROMOTION_DURATION_MS
        and faults.valid
    )
    return "Promote" if promotable else "Reject"


def _non_stage5_status(
    request: StageDecisionRequest,
    run: _VerifiedRun,
    metrics: _MetricsVerification,
) -> str:
    if run.state.status == "deferred":
        return "deferred"
    if run.state.status == "failed":
        return "Reject"
    if request.stage == 3 and not metrics.valid:
        return "Reject"
    if request.stage in {2, 4} and metrics.duration_ms != run.state.cursor_ms:
        return "Reject"
    if run.state.stop_reason == "screen_fail":
        return "Screen-Fail"
    if run.state.stop_reason != "schedule_complete":
        return "Reject"
    if not metrics.valid:
        return "Reject"
    return "Confirm-Pass" if request.stage in {2, 4} else "Finalist / Reliability Pending"


def verify_stage_decision(request: StageDecisionRequest) -> StageDecisionReport:
    """Verify a sealed source chain and derive the decision report."""

    repository = _repository_root(request.repository_root)
    _outside_repository(request.run_dir, repository, label="run directory")
    _outside_repository(request.gate_evidence_path, repository, label="gate evidence")
    _outside_repository(request.output_path, repository, label="decision output")
    try:
        request.output_path.lstat()
    except FileNotFoundError:
        pass
    else:
        raise StageEvidenceError("decision output already exists")

    run = verify_sealed_run(request.run_dir)
    if (
        run.manifest.family_id != request.family_id
        or run.manifest.candidate_id != request.candidate_id
    ):
        raise StageEvidenceError("run family/candidate mismatch")
    if request.stage == 3:
        if run.manifest.stage != 5 or run.manifest.covered_stages != (3, 5):
            raise StageEvidenceError("Stage 3 decisions require the sealed Stage 5 composite run")
    elif run.manifest.stage != request.stage:
        raise StageEvidenceError("run stage mismatch")

    upstream_files = _load_upstream(
        request,
        repository,
        run=run,
    )
    _validate_run_upstream_binding(
        run.manifest,
        {stage: item.sha256 for stage, item in upstream_files.items()},
    )
    gates = _load_gate_evidence(request, repository, run, upstream_files)
    selection: FinalistSelectionEvidence | None = None
    if request.stage == 5:
        if run.manifest.evidence_tier != "formal":
            raise StageEvidenceError("Promote requires formal evidence")
        selection = _load_selection(request, repository, upstream_files)

    if request.stage == 3:
        metrics = _load_stage3_metrics(run)
    elif request.stage == 5:
        metrics = _load_metrics(
            run,
            artifact_name="metrics.json",
            expected_duration=_PROMOTION_DURATION_MS,
            expected_elapsed=_PROMOTION_DURATION_MS,
        )
    else:
        metrics = _load_metrics(run, artifact_name="metrics.json")

    faults = _verify_fault_execution(run) if request.stage == 5 else None
    upstream_hashes = {stage: item.sha256 for stage, item in upstream_files.items()}
    status = (
        _stage5_status(run, gates=gates, metrics=metrics, faults=faults)
        if request.stage == 5 and faults is not None
        else _non_stage5_status(request, run, metrics)
    )
    report_kwargs: dict[str, Any] = {
        "stage": request.stage,
        "family_id": request.family_id,
        "candidate_id": request.candidate_id,
        "status": status,
        "run_manifest_sha256": run.manifest_file.sha256,
        "upstream_report_sha256s": upstream_hashes,
        "hard_gates": dict(gates),
        "actual_duration_ms": (
            metrics.duration_ms if metrics.duration_ms and metrics.duration_ms > 0 else None
        ),
        "artifact_index_sha256": run.index_file.sha256,
        "metrics_sha256": metrics.sha256,
        "unique_finalist": selection is not None,
    }
    if request.stage == 5 and faults is not None:
        report_kwargs["executed_fault_counts"] = dict(faults.counts)
        report_kwargs["fault_execution_sha256"] = faults.sha256
    return StageDecisionReport(**report_kwargs)


__all__ = [
    "StageDecisionRequest",
    "StageEvidenceError",
    "verify_sealed_run",
    "verify_stage_decision",
    "write_stage_decision_report",
]
