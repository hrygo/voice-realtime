"""Stage 2–5 请求、外部制品和资格证据的 fail-closed 验证。

本模块只负责把调用者给出的路径转换成稳定、类型化的内存输入。文件身份由
``read_stable_file`` 统一读取：同一个 regular-file descriptor 完成 hash 和解析，
避免 ``hash(path)`` 与 ``read(path)`` 之间的 TOCTOU 窗口。它不创建 run 目录，
不构造 executor，也不启动模型或服务。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal, cast

from voice_realtime.benchmarks.asr.stage_contracts import (
    EvidenceTier,
    FaultPlan,
    ScheduleManifest,
    StageDecisionReport,
    StageEligibilityEvidence,
    StageInputManifest,
    StageModelManifest,
    StageNumber,
    UpstreamStage,
)
from voice_realtime.benchmarks.asr.stage_executors import ValidatedRuntimeInputs
from voice_realtime.benchmarks.asr.stage_inputs import (
    ResolvedStageInput,
    StageInputError,
    resolve_stage_inputs,
)

_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_STAGES = frozenset({2, 3, 4, 5})
_UPSTREAM_STAGES = frozenset({"stage1", "stage2", "stage3", "stage4"})
_ZERO_GIT_COMMIT = "0" * 40
_MAX_JSON_BYTES = 64 * 1024 * 1024


class StageRunnerError(RuntimeError):
    """统一执行器错误基类。"""

    code = "execution_failed"


class StageRequestError(StageRunnerError, ValueError):
    """请求身份、路径或冻结制品无效。"""

    code = "invalid_request"


class StageEligibilityError(StageRunnerError, ValueError):
    """正式运行的资格证据无效。"""

    code = "evidence_mismatch"


@dataclass(frozen=True, slots=True)
class StableFile:
    """从一个稳定 descriptor 读取的不可变文件快照。"""

    path: Path = field(repr=False)
    raw: bytes = field(repr=False)
    sha256: str
    identity: tuple[int, int, int, int, int, int] = field(repr=False)


@dataclass(frozen=True, slots=True)
class StableMeasurement:
    """只保留模型等大文件的稳定身份和摘要，不累积原始 bytes。"""

    path: Path = field(repr=False)
    size_bytes: int
    sha256: str
    identity: tuple[int, int, int, int, int, int] = field(repr=False)


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _regular_lstat(path: Path, *, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise StageRequestError(f"{label} does not exist") from exc
    if stat.S_ISLNK(info.st_mode):
        raise StageRequestError(f"{label} must not be a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise StageRequestError(f"{label} must be a regular file")
    return info


def _consume_stable_file(
    path: Path,
    *,
    label: str,
    max_bytes: int | None = None,
    capture_raw: bool,
) -> tuple[bytes | None, int, str, tuple[int, int, int, int, int, int]]:
    """在同一 descriptor 上完成稳定性校验，并可选择是否捕获原始 bytes。"""

    candidate = Path(path)
    lstat_before = _regular_lstat(candidate, label=label)
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(candidate, flags)
        except OSError as exc:
            raise StageRequestError(f"{label} cannot be opened safely") from exc
        fstat_before = os.fstat(descriptor)
        if _file_identity(lstat_before) != _file_identity(fstat_before):
            raise StageRequestError(f"{label} changed while opening")
        chunks: list[bytes] | None = [] if capture_raw else None
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise StageRequestError(f"{label} exceeds maximum size")
            digest.update(chunk)
            if chunks is not None:
                chunks.append(chunk)
        fstat_after = os.fstat(descriptor)
        lstat_after = _regular_lstat(candidate, label=label)
        identity_before = _file_identity(fstat_before)
        if (
            identity_before != _file_identity(fstat_after)
            or identity_before != _file_identity(lstat_after)
            or fstat_after.st_size != total
        ):
            raise StageRequestError(f"{label} changed while reading")
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return (
        b"".join(chunks) if chunks is not None else None,
        total,
        digest.hexdigest(),
        _file_identity(lstat_after),
    )


def read_stable_file(
    path: Path,
    *,
    label: str = "file",
    max_bytes: int | None = None,
) -> StableFile:
    """读取 JSON 等小制品的 exact raw bytes 及稳定身份。"""

    raw, _, sha256, identity = _consume_stable_file(
        path,
        label=label,
        max_bytes=max_bytes,
        capture_raw=True,
    )
    assert raw is not None
    return StableFile(path=Path(path), raw=raw, sha256=sha256, identity=identity)


def measure_stable_file(
    path: Path,
    *,
    label: str = "file",
) -> StableMeasurement:
    """流式 hash/size 校验大文件，绝不把完整文件载入内存。"""

    _, size_bytes, sha256, identity = _consume_stable_file(
        path,
        label=label,
        capture_raw=False,
    )
    return StableMeasurement(
        path=Path(path),
        size_bytes=size_bytes,
        sha256=sha256,
        identity=identity,
    )


@dataclass(frozen=True, slots=True)
class StageRunRequest:
    """运行请求；路径只作为验证入口，绝不直接写入制品。"""

    run_id: str
    stage: StageNumber
    covered_stages: tuple[StageNumber, ...]
    family_id: str
    arm: Literal["baseline", "finalist"]
    candidate_id: str
    evidence_tier: EvidenceTier
    executor_id: str
    model_manifest_path: Path
    model_root: Path
    profile_path: Path
    runtime_config_path: Path
    schedule_path: Path
    input_manifest_path: Path
    input_root: Path
    output_root: Path
    repository_root: Path
    eligibility_path: Path | None = None
    upstream_report_paths: Mapping[str, Path] = field(default_factory=dict)
    fault_plan_path: Path | None = None
    lock_path: Path | None = None
    lock_timeout_secs: float = 0.0

    def __post_init__(self) -> None:
        path_fields = (
            "model_manifest_path",
            "model_root",
            "profile_path",
            "runtime_config_path",
            "schedule_path",
            "input_manifest_path",
            "input_root",
            "output_root",
            "repository_root",
            "eligibility_path",
            "fault_plan_path",
            "lock_path",
        )
        try:
            for field_name in path_fields:
                value = getattr(self, field_name)
                if value is not None and not isinstance(value, Path):
                    object.__setattr__(self, field_name, Path(value))
            upstream: dict[str, Path] = {}
            for key, value in self.upstream_report_paths.items():
                if type(key) is not str or key not in _UPSTREAM_STAGES:
                    raise StageRequestError("upstream report keys must be stage1 through stage4")
                upstream[key] = value if isinstance(value, Path) else Path(value)
        except (TypeError, ValueError) as exc:
            raise StageRequestError("stage request contains an invalid path") from exc
        if not isinstance(self.run_id, str) or not _RUN_ID_PATTERN.fullmatch(self.run_id):
            raise StageRequestError("run_id must be a single safe path component")
        if type(self.stage) is not int or self.stage not in _STAGES:
            raise StageRequestError(f"unsupported stage: {self.stage}")
        if type(self.arm) is not str or self.arm not in {"baseline", "finalist"}:
            raise StageRequestError("arm must be baseline or finalist")
        if type(self.evidence_tier) is not str or self.evidence_tier not in {
            "formal",
            "experimental",
        }:
            raise StageRequestError("evidence_tier must be formal or experimental")
        _require_text(self.family_id, label="family_id")
        _require_text(self.candidate_id, label="candidate_id")
        _require_text(self.executor_id, label="executor_id")
        covered = tuple(self.covered_stages)
        if any(type(item) is not int or item not in _STAGES for item in covered):
            raise StageRequestError("covered_stages contains an unsupported stage")
        if not covered or len(covered) != len(set(covered)):
            raise StageRequestError("covered_stages must be non-empty and unique")
        if self.stage == 5:
            if covered not in ((5,), (3, 5)):
                raise StageRequestError("Stage 5 covered_stages must be (5,) or (3, 5)")
            if covered == (3, 5) and (
                self.family_id != "meeting" or self.arm != "finalist"
            ):
                raise StageRequestError(
                    "covered_stages (3, 5) requires meeting finalist"
                )
        elif covered != (self.stage,):
            raise StageRequestError("covered_stages must contain only the physical stage")
        if type(self.lock_timeout_secs) not in {int, float} or not math.isfinite(
            float(self.lock_timeout_secs)
        ) or self.lock_timeout_secs < 0:
            raise StageRequestError("lock_timeout_secs must be finite and non-negative")
        object.__setattr__(self, "covered_stages", covered)
        object.__setattr__(self, "upstream_report_paths", MappingProxyType(upstream))

    @property
    def quarantine_path(self) -> Path:
        from voice_realtime.benchmarks.resource_lock import resource_quarantine_path

        return resource_quarantine_path(self.lock_path)


@dataclass(frozen=True, slots=True)
class StageEligibilityResult:
    eligible: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ValidatedStageRunRequest:
    """经过所有外部身份校验的内存请求。"""

    request: StageRunRequest = field(repr=False)
    schedule: ScheduleManifest
    input_manifest: StageInputManifest
    resolved_inputs: tuple[ResolvedStageInput, ...] = field(repr=False)
    model_manifest: StageModelManifest
    runtime_inputs: ValidatedRuntimeInputs = field(repr=False)
    identity_sha256s: Mapping[str, str]
    git_commit: str
    fault_plan: FaultPlan | None = None
    eligibility: StageEligibilityEvidence | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "identity_sha256s",
            MappingProxyType(dict(self.identity_sha256s)),
        )


def _require_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StageRequestError(f"{label} must be non-empty text")
    return value.strip()


def validate_request_for_lock(request: StageRunRequest) -> None:
    """只验证请求自身与 lock/quarantine 路径，供获取 flock 前调用。"""

    from voice_realtime.benchmarks.resource_lock import default_resource_lock_path

    repository = request.repository_root.resolve(strict=False)
    raw_lock = request.lock_path or default_resource_lock_path()
    raw_quarantine = request.quarantine_path
    for raw_path, label in ((raw_lock, "lock path"), (raw_quarantine, "quarantine path")):
        try:
            info = raw_path.lstat()
        except FileNotFoundError:
            info = None
        except OSError as exc:
            raise StageRequestError(f"{label} cannot be inspected") from exc
        if info is not None:
            if stat.S_ISLNK(info.st_mode):
                raise StageRequestError(f"{label} must not be a symlink")
            if not stat.S_ISREG(info.st_mode):
                raise StageRequestError(f"{label} must be a regular file")
        path = raw_path.resolve(strict=False)
        if path == repository or repository in path.parents:
            raise StageRequestError(f"{label} must be outside repository root")


def _resolve_directory(path: Path, *, label: str) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise StageRequestError(f"{label} does not exist") from exc
    if stat.S_ISLNK(info.st_mode):
        raise StageRequestError(f"{label} must not be a symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise StageRequestError(f"{label} must be a directory")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise StageRequestError(f"{label} cannot be resolved") from exc
    if not resolved.is_dir():
        raise StageRequestError(f"{label} must be a directory")
    return resolved


def _reject_repository_path(path: Path, repository_root: Path, *, label: str) -> Path:
    resolved = path.resolve(strict=True)
    if resolved == repository_root or repository_root in resolved.parents:
        raise StageRequestError(f"{label} must be outside repository root")
    return resolved


def _external_file(path: Path, repository_root: Path, *, label: str) -> StableFile:
    stable = read_stable_file(path, label=label, max_bytes=_MAX_JSON_BYTES)
    _reject_repository_path(stable.path, repository_root, label=label)
    return stable


def _external_directory(path: Path, repository_root: Path, *, label: str) -> Path:
    resolved = _resolve_directory(path, label=label)
    if resolved == repository_root or repository_root in resolved.parents:
        raise StageRequestError(f"{label} must be outside repository root")
    return resolved


def _json_object(stable: StableFile, *, label: str) -> Mapping[str, object]:
    try:
        payload = json.loads(stable.raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StageRequestError(f"{label} must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise StageRequestError(f"{label} JSON root must be an object")
    return cast(Mapping[str, object], payload)


def _load_model_manifest(
    path: Path,
    repository_root: Path,
    model_root: Path,
) -> tuple[StageModelManifest, StableFile]:
    stable = _external_file(path, repository_root, label="model manifest")
    try:
        manifest = StageModelManifest.model_validate_json(stable.raw)
    except ValueError as exc:
        raise StageRequestError("model manifest is invalid") from exc
    root = _external_directory(model_root, repository_root, label="model root")
    for model_file in manifest.files:
        model_path = root
        for part in PurePosixPath(model_file.relative_path).parts:
            model_path /= part
            try:
                component = model_path.lstat()
            except OSError as exc:
                raise StageRequestError("model file does not exist") from exc
            if stat.S_ISLNK(component.st_mode):
                raise StageRequestError("model file path component must not be a symlink")
        measured = measure_stable_file(model_path, label="model file")
        if (
            measured.size_bytes != model_file.size_bytes
            or measured.sha256 != model_file.sha256
        ):
            raise StageRequestError("model file identity mismatch")
    return manifest, stable


def _load_schedule(
    path: Path,
    repository_root: Path,
    stage: StageNumber,
    family_id: str,
) -> tuple[ScheduleManifest, StableFile]:
    stable = _external_file(path, repository_root, label="schedule")
    payload = dict(_json_object(stable, label="schedule"))
    # Existing model_dump(mode="json") includes this computed field.  It is
    # not accepted as an input field, but removing only this known field keeps
    # strict validation for every other unknown key.
    payload.pop("total_duration_ms", None)
    try:
        schedule = ScheduleManifest.model_validate(payload)
    except ValueError as exc:
        raise StageRequestError("schedule is invalid") from exc
    if schedule.stage != stage or schedule.family_id != family_id:
        raise StageRequestError("schedule identity does not match request")
    return schedule, stable


def _load_fault_plan(
    path: Path | None,
    repository_root: Path,
    stage: StageNumber,
) -> tuple[FaultPlan | None, StableFile | None]:
    if path is None:
        if stage == 5:
            raise StageRequestError("Stage 5 requires a fault plan")
        return None, None
    if stage != 5:
        raise StageRequestError("fault plan is only valid for Stage 5")
    stable = _external_file(path, repository_root, label="fault plan")
    try:
        fault_plan = FaultPlan.model_validate_json(stable.raw)
    except ValueError as exc:
        raise StageRequestError("fault plan is invalid") from exc
    return fault_plan, stable


def _validate_stage1_report(
    stable: StableFile,
    *,
    request: StageRunRequest,
    evidence: StageEligibilityEvidence,
    expected_eligible: bool,
) -> None:
    from voice_realtime.benchmarks.asr.report import Stage1DecisionReport

    try:
        report = Stage1DecisionReport.model_validate_json(stable.raw)
    except ValueError as exc:
        raise StageEligibilityError("stage1 report is invalid") from exc
    families = tuple(item for item in report.decisions if item.family_id == request.family_id)
    if len(families) != 1:
        raise StageEligibilityError("stage1 report family identity is missing or ambiguous")
    family = families[0]
    if (
        type(family.family_id) is not str
        or not family.family_id.strip()
        or type(family.baseline_id) is not str
        or not family.baseline_id.strip()
        or (
            family.selected_candidate_id is not None
            and (
                type(family.selected_candidate_id) is not str
                or not family.selected_candidate_id.strip()
            )
        )
    ):
        raise StageEligibilityError("stage1 family identity is invalid")
    if family.selected_candidate_id is not None:
        selected = tuple(
            item
            for item in family.candidates
            if item.candidate_id == family.selected_candidate_id
        )
        if len(selected) != 1:
            raise StageEligibilityError("stage1 selected finalist identity is missing or ambiguous")

    candidates = tuple(
        item for item in family.candidates if item.candidate_id == request.candidate_id
    )
    if request.arm == "finalist":
        if len(candidates) != 1:
            raise StageEligibilityError(
                "stage1 finalist candidate identity is missing or ambiguous"
            )
        if family.selected_candidate_id != request.candidate_id:
            raise StageEligibilityError("stage1 selected finalist mismatch")
    elif request.candidate_id != family.baseline_id:
        raise StageEligibilityError("stage1 baseline identity mismatch")
    if len(candidates) > 1:
        raise StageEligibilityError("stage1 candidate identity is ambiguous")

    candidate_eligible = False
    if candidates:
        candidate = candidates[0]
        if type(candidate.candidate_id) is not str or not candidate.candidate_id.strip():
            raise StageEligibilityError("stage1 candidate identity is invalid")
        if any(
            type(getattr(candidate, field_name)) is not bool
            for field_name in (
                "advance_eligible",
                "hard_rejected",
                "futility_rejected",
                "required_gates_passed",
            )
        ):
            raise StageEligibilityError("stage1 candidate gate fields must be booleans")
        candidate_eligible = (
            candidate.advance_eligible is True
            and candidate.required_gates_passed is True
            and candidate.hard_rejected is False
            and candidate.futility_rejected is False
        )

    promotable_statuses = {"Advance-Early", "Finalist / Reliability Pending"}
    all_eligible_candidates = tuple(
        item
        for item in family.candidates
        if type(item.advance_eligible) is bool
        and type(item.required_gates_passed) is bool
        and type(item.hard_rejected) is bool
        and type(item.futility_rejected) is bool
        and item.advance_eligible is True
        and item.required_gates_passed is True
        and item.hard_rejected is False
        and item.futility_rejected is False
    )
    if expected_eligible:
        if family.status not in promotable_statuses:
            raise StageEligibilityError("stage1 family status cannot advance candidate")
        if family.selected_candidate_id is None or len(all_eligible_candidates) != 1:
            raise StageEligibilityError("stage1 finalist selection is not unique")
        if family.selected_candidate_id != all_eligible_candidates[0].candidate_id:
            raise StageEligibilityError("stage1 selected candidate gate mismatch")
        if request.arm == "finalist" and not candidate_eligible:
            raise StageEligibilityError("stage1 candidate is not advance eligible")
    else:
        if family.status in promotable_statuses or family.selected_candidate_id is not None:
            raise StageEligibilityError("stage1 report contradicts ineligible evidence")
        if all_eligible_candidates:
            raise StageEligibilityError("stage1 report has an advance-eligible finalist")


def _validate_upstream_report(
    stage_name: UpstreamStage,
    stable: StableFile,
    *,
    request: StageRunRequest,
    evidence: StageEligibilityEvidence,
    expected_eligible: bool,
) -> None:
    if stage_name == "stage1":
        _validate_stage1_report(
            stable,
            request=request,
            evidence=evidence,
            expected_eligible=expected_eligible,
        )
        return
    try:
        report = StageDecisionReport.model_validate_json(stable.raw)
    except ValueError as exc:
        raise StageEligibilityError(f"{stage_name} report is invalid") from exc
    expected_stage = int(stage_name.removeprefix("stage"))
    if (
        type(report.stage) is not int
        or type(report.family_id) is not str
        or type(report.candidate_id) is not str
        or report.stage != expected_stage
        or report.family_id != request.family_id
        or report.candidate_id != request.candidate_id
    ):
        raise StageEligibilityError(f"{stage_name} report identity mismatch")
    advancement_statuses = {
        "Screen-Pass",
        "Confirm-Pass",
        "Finalist / Reliability Pending",
    }
    if expected_eligible and report.status not in advancement_statuses:
        raise StageEligibilityError(f"{stage_name} report cannot advance candidate")
    if not expected_eligible and report.status in advancement_statuses:
        raise StageEligibilityError(f"{stage_name} report contradicts ineligible evidence")


def _expected_upstream_eligibility(
    evidence: StageEligibilityEvidence,
    *,
    stage_name: UpstreamStage,
    last_upstream: UpstreamStage,
) -> bool:
    """返回一个上游报告在资格链中必须呈现的推进状态。"""

    if evidence.eligible:
        return True
    if evidence.reason == "not_unique_finalist":
        if evidence.target_stage != 5:
            raise StageEligibilityError(
                "not_unique_finalist eligibility is only valid for Stage 5"
            )
        return True
    if evidence.reason == "stage1_not_advanced":
        if evidence.target_stage != 2 or last_upstream != "stage1":
            raise StageEligibilityError(
                "stage1_not_advanced eligibility is only valid for Stage 2"
            )
        return False
    if evidence.reason == "upstream_incomplete":
        if evidence.target_stage == 2:
            raise StageEligibilityError(
                "Stage 2 must use stage1_not_advanced for an ineligible report"
            )
        return stage_name != last_upstream
    raise StageEligibilityError("unsupported eligibility reason")


def _load_eligibility(
    request: StageRunRequest,
    repository_root: Path,
) -> StageEligibilityEvidence | None:
    if request.evidence_tier == "experimental":
        if request.eligibility_path is not None or request.upstream_report_paths:
            raise StageRequestError("experimental request must not provide formal eligibility")
        return None
    if request.eligibility_path is None:
        raise StageEligibilityError("formal request requires eligibility evidence")
    eligibility_stable = _external_file(
        request.eligibility_path,
        repository_root,
        label="eligibility evidence",
    )
    try:
        evidence = StageEligibilityEvidence.model_validate_json(eligibility_stable.raw)
    except ValueError as exc:
        raise StageEligibilityError("eligibility evidence is invalid") from exc
    if type(evidence.target_stage) is not int or type(evidence.eligible) is not bool:
        raise StageEligibilityError("eligibility stage and flag must use exact types")
    if (
        evidence.target_stage != request.stage
        or evidence.family_id != request.family_id
        or evidence.candidate_id != request.candidate_id
    ):
        raise StageEligibilityError("eligibility identity does not match request")
    expected_stages = {f"stage{number}" for number in range(1, request.stage)}
    if set(evidence.upstream_report_sha256s) != expected_stages:
        raise StageEligibilityError("eligibility upstream stage order is incomplete")
    if set(request.upstream_report_paths) != expected_stages:
        raise StageEligibilityError("upstream report paths do not match eligibility evidence")
    upstream_names = tuple(sorted(expected_stages))
    last_upstream = cast(UpstreamStage, upstream_names[-1])
    for stage_name, report_path in request.upstream_report_paths.items():
        upstream_stage = cast(UpstreamStage, stage_name)
        stable = _external_file(report_path, repository_root, label=f"{stage_name} report")
        if stable.sha256 != evidence.upstream_report_sha256s[upstream_stage]:
            raise StageEligibilityError(f"{stage_name} report hash mismatch")
        expected_eligible = _expected_upstream_eligibility(
            evidence,
            stage_name=upstream_stage,
            last_upstream=last_upstream,
        )
        _validate_upstream_report(
            upstream_stage,
            stable,
            request=request,
            evidence=evidence,
            expected_eligible=expected_eligible,
        )
    return evidence


def _git_commit(repository_root: Path, evidence_tier: EvidenceTier) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if evidence_tier == "formal":
            raise StageRequestError("formal repository HEAD cannot be verified") from exc
        return _ZERO_GIT_COMMIT
    value = completed.stdout.strip().lower()
    if len(value) == 40 and all(character in "0123456789abcdef" for character in value):
        return value
    if evidence_tier == "formal":
        raise StageRequestError("formal repository HEAD is not a valid commit")
    return _ZERO_GIT_COMMIT


def validate_stage_request(request: StageRunRequest) -> ValidatedStageRunRequest:
    """读取并重新验证请求中的全部外部制品。"""

    repository_root = _resolve_directory(request.repository_root, label="repository root")
    model_root = _external_directory(request.model_root, repository_root, label="model root")
    model_manifest, model_stable = _load_model_manifest(
        request.model_manifest_path,
        repository_root,
        model_root,
    )
    profile_stable = _external_file(request.profile_path, repository_root, label="profile")
    profile = _json_object(profile_stable, label="profile")
    runtime_stable = _external_file(
        request.runtime_config_path,
        repository_root,
        label="runtime config",
    )
    runtime_config = _json_object(runtime_stable, label="runtime config")
    schedule, schedule_stable = _load_schedule(
        request.schedule_path,
        repository_root,
        request.stage,
        request.family_id,
    )
    input_stable = _external_file(
        request.input_manifest_path,
        repository_root,
        label="input manifest",
    )
    try:
        input_manifest = StageInputManifest.model_validate_json(input_stable.raw)
        resolved_inputs = resolve_stage_inputs(
            schedule,
            schedule_stable.sha256,
            input_manifest,
            request.input_root,
            repository_root,
            request.evidence_tier,
        )
    except (StageInputError, ValueError) as exc:
        raise StageRequestError("stage inputs are invalid") from exc
    _external_directory(request.input_root, repository_root, label="input root")
    _external_directory(request.output_root, repository_root, label="output root")
    fault_plan, fault_stable = _load_fault_plan(
        request.fault_plan_path,
        repository_root,
        request.stage,
    )
    eligibility = _load_eligibility(request, repository_root)
    git_commit = _git_commit(repository_root, request.evidence_tier)
    runtime_inputs = ValidatedRuntimeInputs(
        model_root=model_root,
        model_manifest=model_manifest,
        profile=profile,
        runtime_config=runtime_config,
    )
    identity_sha256s: dict[str, str] = {
        "model_manifest": model_stable.sha256,
        "profile": profile_stable.sha256,
        "runtime_config": runtime_stable.sha256,
        "schedule": schedule_stable.sha256,
        "input_manifest": input_stable.sha256,
    }
    if fault_stable is not None:
        identity_sha256s["fault_plan"] = fault_stable.sha256
    return ValidatedStageRunRequest(
        request=request,
        schedule=schedule,
        input_manifest=input_manifest,
        resolved_inputs=resolved_inputs,
        model_manifest=model_manifest,
        runtime_inputs=runtime_inputs,
        identity_sha256s=identity_sha256s,
        git_commit=git_commit,
        fault_plan=fault_plan,
        eligibility=eligibility,
    )


def validate_stage_eligibility(
    validated: ValidatedStageRunRequest,
) -> StageEligibilityResult:
    if validated.request.evidence_tier == "experimental":
        return StageEligibilityResult(eligible=True, reason="experimental")
    evidence = validated.eligibility
    if evidence is None:
        raise StageEligibilityError("formal request has no eligibility evidence")
    return StageEligibilityResult(eligible=evidence.eligible, reason=evidence.reason)


def load_stage_run_request(
    payload: Mapping[str, object] | None = None,
    **kwargs: object,
) -> StageRunRequest:
    values = dict(payload or {})
    values.update(kwargs)
    try:
        return StageRunRequest(**values)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise StageRequestError("invalid stage run request") from exc


__all__ = [
    "StableFile",
    "StableMeasurement",
    "StageEligibilityError",
    "StageEligibilityResult",
    "StageRequestError",
    "StageRunRequest",
    "StageRunnerError",
    "ValidatedStageRunRequest",
    "load_stage_run_request",
    "measure_stable_file",
    "read_stable_file",
    "validate_request_for_lock",
    "validate_stage_eligibility",
    "validate_stage_request",
]
