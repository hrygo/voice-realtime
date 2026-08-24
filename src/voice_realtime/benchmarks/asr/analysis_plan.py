"""冻结 Core/Reserve 两次查看的序贯统计计划。"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from voice_realtime.benchmarks.asr.manifest import (
    CorpusInputManifest,
    CorpusReferenceManifest,
    load_corpus_input_manifest,
    load_reference_manifest,
    resolve_relative_file,
    sha256_file,
)
from voice_realtime.benchmarks.asr.preflight import BlindPreflightReport

_SHA256_LENGTH = 64
_MAX_ANALYSIS_ARTIFACT_BYTES = 16 * 1024 * 1024


class DecisionFamily(BaseModel):
    """同一业务方向内共享多重比较校正的候选集合。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    family_id: str = Field(min_length=1)
    baseline_id: str = Field(min_length=1)
    candidate_ids: tuple[str, ...] = Field(min_length=1)
    pilot_baseline_cer: float = Field(gt=0, le=1)
    relative_mde: float = 0.05
    required_noninferiority_gates: tuple[str, ...] = ()

    @field_validator("family_id", "baseline_id")
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("decision family identity 不能为空")
        return normalized

    @field_validator("candidate_ids", "required_noninferiority_gates")
    @classmethod
    def _validate_unique_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("decision family entry 不能为空")
        if len(normalized) != len(set(normalized)):
            raise ValueError("decision family entry 必须唯一")
        return normalized

    @model_validator(mode="after")
    def _validate_registered_family(self) -> Self:
        if self.baseline_id in self.candidate_ids:
            raise ValueError("baseline_id 不能同时是 candidate_id")
        if self.relative_mde != 0.05:
            raise ValueError("relative_mde 必须是 0.05")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def minimum_detectable_effect(self) -> float:
        """由本决策族 pilot baseline CER 冻结的绝对 MDE。"""
        return self.pilot_baseline_cer * self.relative_mde


class AnalysisPlanDesign(BaseModel):
    """在读取 blind 输出前由 dev/pilot 冻结的统计设计输入。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    candidate_ids: tuple[str, ...] = Field(min_length=2)
    bootstrap_seeds: tuple[int, int]
    bootstrap_iterations: Literal[10000] = 10_000
    pilot_baseline_cer: float = Field(gt=0, le=1)
    primary_endpoints: tuple[str, ...] = Field(min_length=1)
    normalization_version: str = Field(min_length=1)
    filtering_rules: tuple[str, ...] = Field(min_length=1)
    decision_families: tuple[DecisionFamily, ...] = Field(min_length=1)
    power_simulation_sha256: str
    power_simulation_iterations: Literal[10000] = 10_000
    pilot_cluster_variance: float = Field(gt=0)
    core_power: float = Field(ge=0, le=1)
    final_power: float = Field(ge=0, le=1)
    simulated_familywise_alpha: float = Field(ge=0, le=1)

    @field_validator("power_simulation_sha256")
    @classmethod
    def _power_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != _SHA256_LENGTH or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("power simulation SHA-256 必须是 64 位十六进制")
        return normalized

    @field_validator(
        "candidate_ids",
        "primary_endpoints",
        "filtering_rules",
    )
    @classmethod
    def _unique_entries(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("analysis design entry 不能为空")
        if len(normalized) != len(set(normalized)):
            raise ValueError("analysis design entry 必须唯一")
        return normalized

    @model_validator(mode="after")
    def _validate_design(self) -> Self:
        if self.bootstrap_seeds[0] == self.bootstrap_seeds[1]:
            raise ValueError("Core/Reserve bootstrap seed 必须不同")
        candidate_ids = set(self.candidate_ids)
        family_ids = tuple(family.family_id for family in self.decision_families)
        if len(family_ids) != len(set(family_ids)):
            raise ValueError("decision family id 必须唯一")
        for family in self.decision_families:
            if not {family.baseline_id, *family.candidate_ids} <= candidate_ids:
                raise ValueError("decision family identity 必须包含在 candidate_ids 中")
        return self


class PowerSimulationArtifact(BaseModel):
    """blind 开封前由 dev/pilot 生成的结构化功效模拟摘要。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    iterations: Literal[10000]
    pilot_cluster_variance: float = Field(gt=0)
    core_power: float = Field(ge=0, le=1)
    final_power: float = Field(ge=0, le=1)
    simulated_familywise_alpha: float = Field(ge=0, le=1)


class AnalysisPlan(BaseModel):
    """在 Core 输出可见前冻结的候选、边界、MDE 与随机种子。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    evidence_tier: Literal["exploratory", "formal"] = "exploratory"
    candidate_ids: tuple[str, ...] = Field(min_length=2)
    candidate_profile_sha256: dict[str, str] = Field(default_factory=dict)
    core_manifest_sha256: str
    reserve_manifest_sha256: str
    core_reference_sha256: str
    reserve_reference_sha256: str
    preflight_report_sha256: str | None = None
    preflight_metadata_sha256: str | None = None
    cluster_set_sha256: str | None = None
    sample_order_sha256: str | None = None
    power_simulation_sha256: str | None = None
    power_simulation_iterations: Literal[10000] = 10_000
    pilot_cluster_variance: float | None = Field(default=None, gt=0)
    core_power: float | None = Field(default=None, ge=0, le=1)
    final_power: float | None = Field(default=None, ge=0, le=1)
    simulated_familywise_alpha: float | None = Field(default=None, ge=0, le=1)
    core_duration_ms: int | None = Field(default=None, gt=0)
    reserve_duration_ms: int | None = Field(default=None, gt=0)
    core_analysis_cluster_ids: tuple[str, ...] = ()
    reserve_analysis_cluster_ids: tuple[str, ...] = ()
    analysis_cluster_ids: tuple[str, ...] = ()
    primary_endpoints: tuple[str, ...] = ()
    normalization_version: str | None = None
    filtering_rules: tuple[str, ...] = ()
    look_alpha: tuple[float, float] = (0.01, 0.04)
    decision_confidence: tuple[float, float] = (0.99, 0.96)
    conditional_power_futility: float = 0.20
    bootstrap_seeds: tuple[int, int]
    bootstrap_iterations: Literal[10000] = 10_000
    pilot_baseline_cer: float = Field(gt=0, le=1)
    relative_mde: float = 0.05
    decision_families: tuple[DecisionFamily, ...] = ()
    allowed_stopping_states: tuple[
        Literal["core", "reserve", "completed"],
        Literal["core", "reserve", "completed"],
        Literal["core", "reserve", "completed"],
    ] = ("core", "reserve", "completed")

    @field_validator(
        "core_manifest_sha256",
        "reserve_manifest_sha256",
        "core_reference_sha256",
        "reserve_reference_sha256",
        "preflight_report_sha256",
        "preflight_metadata_sha256",
        "cluster_set_sha256",
        "sample_order_sha256",
        "power_simulation_sha256",
    )
    @classmethod
    def _validate_hash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if len(normalized) != _SHA256_LENGTH or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("manifest SHA-256 必须是 64 位十六进制")
        return normalized

    @field_validator("candidate_profile_sha256")
    @classmethod
    def _validate_profile_hashes(cls, value: dict[str, str]) -> dict[str, str]:
        validated: dict[str, str] = {}
        for raw_candidate_id, raw_hash in value.items():
            candidate_id = raw_candidate_id.strip()
            if not candidate_id:
                raise ValueError("candidate profile identity 不能为空")
            normalized_hash = raw_hash.strip().lower()
            if len(normalized_hash) != _SHA256_LENGTH or any(
                character not in "0123456789abcdef"
                for character in normalized_hash
            ):
                raise ValueError("candidate profile SHA-256 必须是 64 位十六进制")
            validated[candidate_id] = normalized_hash
        return validated

    @field_validator("candidate_ids")
    @classmethod
    def _validate_candidates(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(candidate.strip() for candidate in value)
        if any(not candidate for candidate in normalized):
            raise ValueError("candidate_id 不能为空")
        if len(normalized) != len(set(normalized)):
            raise ValueError("candidate_id 必须唯一")
        return normalized

    @field_validator(
        "analysis_cluster_ids",
        "core_analysis_cluster_ids",
        "reserve_analysis_cluster_ids",
        "primary_endpoints",
        "filtering_rules",
    )
    @classmethod
    def _validate_unique_entries(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("analysis plan entry 不能为空")
        if len(normalized) != len(set(normalized)):
            raise ValueError("analysis plan entry 必须唯一")
        return normalized

    @model_validator(mode="after")
    def _freeze_registered_design(self) -> Self:
        if self.look_alpha != (0.01, 0.04):
            raise ValueError("look_alpha 必须是预注册的 (0.01, 0.04)")
        if self.decision_confidence != (0.99, 0.96):
            raise ValueError("decision_confidence 必须是 (0.99, 0.96)")
        if self.conditional_power_futility != 0.20:
            raise ValueError("conditional_power_futility 必须是 0.20")
        if self.relative_mde != 0.05:
            raise ValueError("relative_mde 必须是 0.05")
        if self.allowed_stopping_states != ("core", "reserve", "completed"):
            raise ValueError("allowed_stopping_states 必须是预注册的三种状态")
        if self.bootstrap_seeds[0] == self.bootstrap_seeds[1]:
            raise ValueError("Core/Reserve bootstrap seed 必须不同")
        family_ids = tuple(family.family_id for family in self.decision_families)
        if len(family_ids) != len(set(family_ids)):
            raise ValueError("decision family id 必须唯一")
        registered_ids = set(self.candidate_ids)
        for family in self.decision_families:
            family_ids_in_plan = {family.baseline_id, *family.candidate_ids}
            if not family_ids_in_plan <= registered_ids:
                raise ValueError("decision family identity 必须包含在 candidate_ids 中")
        if self.candidate_profile_sha256 and (
            set(self.candidate_profile_sha256) != registered_ids
        ):
            raise ValueError("candidate profile set 必须与 candidate_ids 完全一致")
        if self.evidence_tier == "formal":
            if any(
                not family.required_noninferiority_gates
                for family in self.decision_families
            ):
                raise ValueError(
                    "formal analysis plan requires non-inferiority gates for every family"
                )
            cluster_partition_valid = (
                bool(self.core_analysis_cluster_ids)
                and bool(self.reserve_analysis_cluster_ids)
                and not (
                    set(self.core_analysis_cluster_ids)
                    & set(self.reserve_analysis_cluster_ids)
                )
                and set(self.analysis_cluster_ids)
                == {
                    *self.core_analysis_cluster_ids,
                    *self.reserve_analysis_cluster_ids,
                }
            )
            formal_fields_present = (
                bool(self.candidate_profile_sha256)
                and self.preflight_report_sha256 is not None
                and self.preflight_metadata_sha256 is not None
                and self.cluster_set_sha256 is not None
                and self.sample_order_sha256 is not None
                and self.power_simulation_sha256 is not None
                and self.pilot_cluster_variance is not None
                and self.core_power is not None
                and self.final_power is not None
                and self.simulated_familywise_alpha is not None
                and self.core_duration_ms is not None
                and self.reserve_duration_ms is not None
                and bool(self.analysis_cluster_ids)
                and cluster_partition_valid
                and bool(self.primary_endpoints)
                and self.normalization_version is not None
                and bool(self.filtering_rules)
                and bool(self.decision_families)
            )
            if not formal_fields_present:
                raise ValueError(
                    "formal analysis plan requires preflight, profiles, durations, "
                    "clusters, endpoints, normalization, filters and decision families"
                )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def minimum_detectable_effect(self) -> float:
        """由 pilot baseline CER 冻结的 5% 相对改善绝对值。"""
        return self.pilot_baseline_cer * self.relative_mde


def load_analysis_plan_design(path: Path) -> AnalysisPlanDesign:
    """有界读取冻结前统计设计。"""
    if path.stat().st_size > _MAX_ANALYSIS_ARTIFACT_BYTES:
        raise ValueError("analysis design exceeds 16 MiB")
    return AnalysisPlanDesign.model_validate_json(path.read_text(encoding="utf-8"))


def load_analysis_plan(path: Path) -> AnalysisPlan:
    """有界读取已冻结的分析计划。"""
    if path.stat().st_size > _MAX_ANALYSIS_ARTIFACT_BYTES:
        raise ValueError("analysis plan exceeds 16 MiB")
    return AnalysisPlan.model_validate_json(path.read_text(encoding="utf-8"))


def _hash_sealed(path: Path) -> str:
    if path.stat().st_mode & 0o777 != 0:
        raise ValueError("both reference manifests must be sealed with mode 000")
    try:
        path.chmod(0o600)
        return sha256_file(path)
    finally:
        path.chmod(0)


def sealed_sha256(path: Path) -> str:
    """只供冻结/开盲控制面核验 mode 000 参考制品。"""
    return _hash_sealed(path)


def freeze_analysis_plan(
    output: Path,
    plan: AnalysisPlan,
    *,
    core_manifest: Path,
    reserve_manifest: Path,
    core_reference: Path,
    reserve_reference: Path,
) -> None:
    """核验两段同时封存后，原子写入不可覆盖的分析计划。"""
    if output.exists():
        raise FileExistsError(f"frozen analysis plan already exists: {output}")
    if (core_reference.stat().st_mode & 0o777) != 0 or (
        reserve_reference.stat().st_mode & 0o777
    ) != 0:
        raise ValueError("both reference manifests must be sealed with mode 000")
    actual_hashes = (
        sha256_file(core_manifest),
        sha256_file(reserve_manifest),
        _hash_sealed(core_reference),
        _hash_sealed(reserve_reference),
    )
    expected_hashes = (
        plan.core_manifest_sha256,
        plan.reserve_manifest_sha256,
        plan.core_reference_sha256,
        plan.reserve_reference_sha256,
    )
    if actual_hashes != expected_hashes:
        raise ValueError("analysis artifact SHA-256 mismatch")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.parent.chmod(0o700)
    payload = plan.model_dump_json(indent=2, exclude_computed_fields=True) + "\n"
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
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(output)
        output.chmod(0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _load_sealed_reference(
    path: Path,
) -> tuple[str, CorpusReferenceManifest]:
    if path.stat().st_mode & 0o777 != 0:
        raise ValueError("both reference manifests must be sealed with mode 000")
    try:
        path.chmod(0o600)
        return sha256_file(path), load_reference_manifest(path)
    finally:
        path.chmod(0)


def _verify_reference_binding(
    manifest_path: Path,
    manifest: CorpusInputManifest,
    reference: CorpusReferenceManifest,
) -> None:
    if reference.split != manifest.split:
        raise ValueError("reference split does not match input manifest")
    if reference.corpus_version != manifest.corpus_version:
        raise ValueError("reference corpus version does not match input manifest")
    if reference.normalization_version != manifest.normalization_version:
        raise ValueError("reference normalization does not match input manifest")
    if reference.input_manifest_sha256 != sha256_file(manifest_path):
        raise ValueError("reference is not bound to input manifest SHA-256")
    input_ids = {sample.sample_id for sample in manifest.samples}
    reference_ids = {sample.sample_id for sample in reference.samples}
    if input_ids != reference_ids:
        raise ValueError("reference and input sample IDs must match exactly")


def _cluster_set_sha256(cluster_ids: tuple[str, ...]) -> str:
    payload = "\n".join(sorted(cluster_ids)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sample_order_sha256(sample_ids: tuple[str, ...]) -> str:
    payload = "\n".join(sample_ids).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _verify_materialized_pcm(
    corpus_root: Path,
    *manifests: CorpusInputManifest,
) -> None:
    root = corpus_root.resolve(strict=True)
    for manifest in manifests:
        for sample in manifest.samples:
            audio_path = resolve_relative_file(root, sample.audio_path)
            expected_size = sample.duration_ms * 32
            if audio_path.stat().st_size != expected_size:
                raise ValueError(f"PCM byte length does not match duration: {sample.sample_id}")
            if sha256_file(audio_path) != sample.audio_sha256:
                raise ValueError(f"PCM SHA-256 does not match manifest: {sample.sample_id}")


def _verify_cross_look_isolation(
    core: CorpusInputManifest,
    reserve: CorpusInputManifest,
) -> None:
    def values(
        manifest: CorpusInputManifest,
        field: Literal["session", "content", "speaker", "cluster"],
    ) -> set[str]:
        if field == "session":
            return {sample.session_id for sample in manifest.samples}
        if field == "content":
            return {
                sample.content_group_id
                for sample in manifest.samples
                if sample.content_group_id is not None
            }
        if field == "speaker":
            return {speaker for sample in manifest.samples for speaker in sample.speakers}
        return {
            sample.analysis_cluster_id
            for sample in manifest.samples
            if sample.analysis_cluster_id is not None
        }

    for field in ("session", "content", "speaker", "cluster"):
        if values(core, field) & values(reserve, field):
            raise ValueError(f"Core/Reserve {field} identities must not overlap")


def freeze_formal_analysis_plan(
    output: Path,
    design: AnalysisPlanDesign,
    *,
    core_manifest: Path,
    reserve_manifest: Path,
    core_reference: Path,
    reserve_reference: Path,
    preflight_report: Path,
    profile_paths: Mapping[str, Path],
    corpus_root: Path,
    power_simulation: Path,
    preflight_metadata: Path,
) -> AnalysisPlan:
    """语义核验 Core/Reserve、preflight 与 profiles 后冻结正式计划。"""
    if output.exists():
        raise FileExistsError(f"frozen analysis plan already exists: {output}")
    if set(profile_paths) != set(design.candidate_ids):
        raise ValueError("profile path set must match fixed candidate set")
    if sha256_file(power_simulation) != design.power_simulation_sha256:
        raise ValueError("power simulation SHA-256 does not match analysis design")
    if power_simulation.stat().st_size > _MAX_ANALYSIS_ARTIFACT_BYTES:
        raise ValueError("power simulation exceeds 16 MiB")
    simulated = PowerSimulationArtifact.model_validate_json(
        power_simulation.read_text(encoding="utf-8")
    )
    if (
        simulated.iterations != design.power_simulation_iterations
        or simulated.pilot_cluster_variance != design.pilot_cluster_variance
        or simulated.core_power != design.core_power
        or simulated.final_power != design.final_power
        or simulated.simulated_familywise_alpha
        != design.simulated_familywise_alpha
    ):
        raise ValueError("power simulation semantics do not match analysis design")
    core = load_corpus_input_manifest(core_manifest)
    reserve = load_corpus_input_manifest(reserve_manifest)
    if core.split != "blind-core" or reserve.split != "blind-reserve":
        raise ValueError("formal analysis requires blind-core and blind-reserve manifests")
    if core.corpus_version != reserve.corpus_version:
        raise ValueError("Core/Reserve corpus versions must match")
    if core.normalization_version != reserve.normalization_version:
        raise ValueError("Core/Reserve normalization versions must match")
    if core.normalization_version != design.normalization_version:
        raise ValueError("analysis normalization does not match blind manifests")
    _verify_materialized_pcm(corpus_root, core, reserve)
    _verify_cross_look_isolation(core, reserve)

    core_reference_hash, core_references = _load_sealed_reference(core_reference)
    reserve_reference_hash, reserve_references = _load_sealed_reference(
        reserve_reference
    )
    _verify_reference_binding(core_manifest, core, core_references)
    _verify_reference_binding(reserve_manifest, reserve, reserve_references)

    core_clusters = tuple(
        sample.analysis_cluster_id or "" for sample in core.samples
    )
    reserve_clusters = tuple(
        sample.analysis_cluster_id or "" for sample in reserve.samples
    )
    if any(not cluster_id for cluster_id in (*core_clusters, *reserve_clusters)):
        raise ValueError("formal blind samples require explicit analysis_cluster_id")
    if set(core_clusters) & set(reserve_clusters):
        raise ValueError("Core/Reserve analysis clusters must not overlap")
    analysis_cluster_ids = tuple(sorted({*core_clusters, *reserve_clusters}))

    if preflight_report.stat().st_size > _MAX_ANALYSIS_ARTIFACT_BYTES:
        raise ValueError("preflight report exceeds 16 MiB")
    preflight = BlindPreflightReport.model_validate_json(
        preflight_report.read_text(encoding="utf-8")
    )
    if preflight.status != "metadata_ready" or preflight.blockers:
        raise ValueError("formal analysis requires metadata_ready preflight")
    if sha256_file(preflight_metadata) != preflight.metadata_sha256:
        raise ValueError("preflight metadata SHA-256 does not match report")
    core_duration_ms = sum(sample.duration_ms for sample in core.samples)
    reserve_duration_ms = sum(sample.duration_ms for sample in reserve.samples)
    if preflight.unique_duration_ms != {
        "blind-core": core_duration_ms,
        "blind-reserve": reserve_duration_ms,
    }:
        raise ValueError("preflight duration does not match blind manifests")
    if preflight.cluster_set_sha256 != _cluster_set_sha256(analysis_cluster_ids):
        raise ValueError("preflight cluster set does not match blind manifests")
    sample_order = tuple(
        sample.sample_id for sample in (*core.samples, *reserve.samples)
    )
    if preflight.sample_order_sha256 != _sample_order_sha256(sample_order):
        raise ValueError("preflight sample order does not match blind manifests")

    plan = AnalysisPlan(
        evidence_tier="formal",
        candidate_ids=design.candidate_ids,
        candidate_profile_sha256={
            candidate_id: sha256_file(profile_paths[candidate_id])
            for candidate_id in design.candidate_ids
        },
        core_manifest_sha256=sha256_file(core_manifest),
        reserve_manifest_sha256=sha256_file(reserve_manifest),
        core_reference_sha256=core_reference_hash,
        reserve_reference_sha256=reserve_reference_hash,
        preflight_report_sha256=sha256_file(preflight_report),
        preflight_metadata_sha256=preflight.metadata_sha256,
        cluster_set_sha256=preflight.cluster_set_sha256,
        sample_order_sha256=preflight.sample_order_sha256,
        power_simulation_sha256=design.power_simulation_sha256,
        power_simulation_iterations=design.power_simulation_iterations,
        pilot_cluster_variance=design.pilot_cluster_variance,
        core_power=design.core_power,
        final_power=design.final_power,
        simulated_familywise_alpha=design.simulated_familywise_alpha,
        core_duration_ms=core_duration_ms,
        reserve_duration_ms=reserve_duration_ms,
        analysis_cluster_ids=analysis_cluster_ids,
        core_analysis_cluster_ids=tuple(sorted(set(core_clusters))),
        reserve_analysis_cluster_ids=tuple(sorted(set(reserve_clusters))),
        primary_endpoints=design.primary_endpoints,
        normalization_version=design.normalization_version,
        filtering_rules=design.filtering_rules,
        bootstrap_seeds=design.bootstrap_seeds,
        bootstrap_iterations=design.bootstrap_iterations,
        pilot_baseline_cer=design.pilot_baseline_cer,
        decision_families=design.decision_families,
    )
    freeze_analysis_plan(
        output,
        plan,
        core_manifest=core_manifest,
        reserve_manifest=reserve_manifest,
        core_reference=core_reference,
        reserve_reference=reserve_reference,
    )
    return plan
