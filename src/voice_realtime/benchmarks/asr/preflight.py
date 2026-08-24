"""不读取音频或逐字稿的目标域 blind metadata 预检。"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core.core_schema import ValidationInfo

from voice_realtime.benchmarks.asr.manifest import sha256_file

BlindSplit = Literal["blind-core", "blind-reserve"]
_SHA256_LENGTH = 64
_MAX_METADATA_BYTES = 64 * 1024 * 1024


def _default_blind_durations() -> dict[BlindSplit, int]:
    return {"blind-core": 3_600_000, "blind-reserve": 2_700_000}


def _default_scenario_durations() -> dict[BlindSplit, dict[str, int]]:
    return {
        "blind-core": {
            "near-field": 9 * 60_000,
            "meeting": 17 * 60_000,
            "code-switch": 9 * 60_000,
            "accent": 11 * 60_000,
            "noise": 6 * 60_000,
            "entity": 6 * 60_000,
            "negative": 2 * 60_000,
        },
        "blind-reserve": {
            "near-field": 6 * 60_000,
            "meeting": 13 * 60_000,
            "code-switch": 6 * 60_000,
            "accent": 9 * 60_000,
            "noise": 4 * 60_000,
            "entity": 4 * 60_000,
            "negative": 3 * 60_000,
        },
    }


def _default_minimum_speakers() -> dict[BlindSplit, int]:
    return {"blind-core": 14, "blind-reserve": 6}


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _validate_sha256(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("SHA-256 必须是 64 位十六进制")
    return normalized


def _validate_opaque_token(value: str, *, prefix: str) -> str:
    normalized = value.strip()
    if not normalized.startswith(f"{prefix}:"):
        raise ValueError(f"{prefix} identity must be an opaque token")
    suffix = normalized.removeprefix(f"{prefix}:")
    if not suffix or any(character in suffix for character in ("/", "\\", " ")):
        raise ValueError(f"{prefix} identity must be an opaque token")
    return normalized


class SourceCatalogEntry(_FrozenModel):
    """不含授权原件和个人身份的来源快照状态。"""

    source_token: str
    source_snapshot_sha256: str
    authorization_ref: str = Field(min_length=1, max_length=300)
    authorization_status: Literal["approved", "pending", "expired", "rejected"]
    deidentification_status: Literal["verified", "pending", "rejected"]
    human_reviewed: bool

    @field_validator("source_token")
    @classmethod
    def _source_token(cls, value: str) -> str:
        return _validate_opaque_token(value, prefix="source")

    @field_validator("source_snapshot_sha256")
    @classmethod
    def _source_hash(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator("authorization_ref")
    @classmethod
    def _authorization_ref(cls, value: str) -> str:
        return _validate_opaque_token(value, prefix="authorization")


class BlindCandidateMetadata(_FrozenModel):
    """只含匿名 locator 与统计分层信息的候选样本。"""

    sample_id: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    split: BlindSplit
    source_token: str
    source_locator: str = Field(min_length=1, max_length=1000)
    duration_ms: int = Field(gt=0)
    session_token: str
    content_group_token: str
    analysis_cluster_token: str
    speaker_tokens: tuple[str, ...] = Field(min_length=1)
    source_sample_rate_hz: Literal[16000] = 16000
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)
    channel_index: int = Field(ge=0, le=63)
    scenario: str = Field(min_length=1, max_length=200)
    language: str = Field(min_length=1, max_length=64)
    tags: tuple[str, ...] = ()
    synthetic: Literal[False]

    @field_validator(
        "source_token",
        "session_token",
        "content_group_token",
        "analysis_cluster_token",
    )
    @classmethod
    def _identity_token(cls, value: str, info: ValidationInfo) -> str:
        prefixes = {
            "source_token": "source",
            "session_token": "session",
            "content_group_token": "content",
            "analysis_cluster_token": "cluster",
        }
        field_name = info.field_name
        if field_name is None:
            raise ValueError("identity field name is unavailable")
        return _validate_opaque_token(value, prefix=prefixes[field_name])

    @field_validator("speaker_tokens")
    @classmethod
    def _speaker_tokens(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            _validate_opaque_token(item, prefix="speaker") for item in value
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError("speaker opaque token 必须唯一")
        return normalized

    @field_validator("source_locator")
    @classmethod
    def _relative_locator(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts or not normalized:
            raise ValueError("source_locator 必须是相对路径")
        return str(path)

    @field_validator("tags")
    @classmethod
    def _unique_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value if item.strip())
        if len(normalized) != len(set(normalized)):
            raise ValueError("tag 必须唯一")
        return normalized

    @model_validator(mode="after")
    def _exact_frame_interval(self) -> Self:
        if self.end_frame <= self.start_frame:
            raise ValueError("frame interval must be positive")
        if self.end_frame - self.start_frame != self.duration_ms * 16:
            raise ValueError("frame interval must exactly match duration_ms")
        return self


class ReferenceCatalogEntry(_FrozenModel):
    """只记录 reference 制品身份与人工标注状态，不承载正文。"""

    sample_id: str = Field(min_length=1, max_length=200)
    reference_sha256: str
    reference_revision: str = Field(min_length=1, max_length=200)
    normalization_version: Literal["nfkc-casefold-punct-space-v1"]
    annotation_status: Literal["pending", "double_annotated", "adjudicated"]
    annotator_count: int = Field(ge=0)
    adjudicated: bool

    @field_validator("reference_sha256")
    @classmethod
    def _reference_hash(cls, value: str) -> str:
        return _validate_sha256(value)


class BlindPreflightSpec(_FrozenModel):
    """可在音频进入 runner 前单独审核的 metadata 包。"""

    schema_version: Literal["1.0"] = "1.0"
    corpus_version: str = Field(min_length=1, max_length=200)
    normalization_version: Literal["nfkc-casefold-punct-space-v1"] = (
        "nfkc-casefold-punct-space-v1"
    )
    sources: tuple[SourceCatalogEntry, ...] = Field(min_length=1)
    candidates: tuple[BlindCandidateMetadata, ...] = Field(min_length=1)
    references: tuple[ReferenceCatalogEntry, ...] = ()
    required_duration_ms: dict[BlindSplit, int] = Field(
        default_factory=_default_blind_durations
    )
    required_scenario_duration_ms: dict[BlindSplit, dict[str, int]] = Field(
        default_factory=_default_scenario_durations
    )
    minimum_speakers: dict[BlindSplit, int] = Field(
        default_factory=_default_minimum_speakers
    )

    @model_validator(mode="after")
    def _unique_catalog_identities(self) -> Self:
        for label, values in (
            ("source_token", tuple(item.source_token for item in self.sources)),
            ("sample_id", tuple(item.sample_id for item in self.candidates)),
            ("reference sample_id", tuple(item.sample_id for item in self.references)),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} 必须唯一")
        return self


class BlindPreflightReport(_FrozenModel):
    """metadata 就绪状态；刻意不提供 blind_ready。"""

    schema_version: Literal["1.0"] = "1.0"
    status: Literal["metadata_ready", "incomplete"]
    metadata_sha256: str
    blockers: tuple[str, ...]
    sample_count: dict[BlindSplit, int]
    unique_duration_ms: dict[BlindSplit, int]
    scenario_duration_ms: dict[BlindSplit, dict[str, int]]
    cluster_set_sha256: str
    sample_order_sha256: str

    @field_validator("metadata_sha256", "cluster_set_sha256", "sample_order_sha256")
    @classmethod
    def _hashes(cls, value: str) -> str:
        return _validate_sha256(value)


def _cross_look_overlap(
    core: tuple[BlindCandidateMetadata, ...],
    reserve: tuple[BlindCandidateMetadata, ...],
    attribute: str,
) -> bool:
    def values(samples: tuple[BlindCandidateMetadata, ...]) -> set[str]:
        observed: set[str] = set()
        for sample in samples:
            value = getattr(sample, attribute)
            if isinstance(value, tuple):
                observed.update(cast(tuple[str, ...], value))
            else:
                observed.add(cast(str, value))
        return observed

    return bool(values(core) & values(reserve))


def _stable_token_set_sha256(tokens: set[str]) -> str:
    payload = "\n".join(sorted(tokens)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _stable_sequence_sha256(values: tuple[str, ...]) -> str:
    payload = "\n".join(values).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def evaluate_blind_preflight(
    spec: BlindPreflightSpec,
    *,
    metadata_sha256: str,
) -> BlindPreflightReport:
    """只检查 metadata 的配额、隔离、授权与标注状态。"""
    blockers: list[str] = []
    split_samples = {
        split: tuple(sample for sample in spec.candidates if sample.split == split)
        for split in ("blind-core", "blind-reserve")
    }
    sample_count: dict[BlindSplit, int] = {}
    unique_duration_ms: dict[BlindSplit, int] = {}
    scenario_duration_ms: dict[BlindSplit, dict[str, int]] = {}
    for raw_split, samples in split_samples.items():
        split = cast(BlindSplit, raw_split)
        sample_count[split] = len(samples)
        duration = sum(sample.duration_ms for sample in samples)
        unique_duration_ms[split] = duration
        scenarios: dict[str, int] = {}
        for sample in samples:
            scenarios[sample.scenario] = scenarios.get(sample.scenario, 0) + sample.duration_ms
        scenario_duration_ms[split] = dict(sorted(scenarios.items()))
        if duration != spec.required_duration_ms[split]:
            blockers.append(f"{split}_duration_quota_mismatch")
        if scenarios != spec.required_scenario_duration_ms[split]:
            blockers.append(f"{split}_scenario_quota_mismatch")
        speakers = {speaker for sample in samples for speaker in sample.speaker_tokens}
        if len(speakers) < spec.minimum_speakers[split]:
            blockers.append(f"{split}_speaker_quota_not_met")

    core = split_samples["blind-core"]
    reserve = split_samples["blind-reserve"]
    for attribute, blocker in (
        ("session_token", "cross_look_session_overlap"),
        ("speaker_tokens", "cross_look_speaker_overlap"),
        ("content_group_token", "cross_look_content_group_overlap"),
        ("analysis_cluster_token", "cross_look_analysis_cluster_overlap"),
    ):
        if _cross_look_overlap(core, reserve, attribute):
            blockers.append(blocker)

    sources = {item.source_token: item for item in spec.sources}
    referenced_source_tokens = {sample.source_token for sample in spec.candidates}
    if referenced_source_tokens != set(sources):
        blockers.append("source_catalog_sample_set_mismatch")
    if any(item.authorization_status != "approved" for item in sources.values()):
        blockers.append("source_authorization_not_approved")
    if any(item.deidentification_status != "verified" for item in sources.values()):
        blockers.append("source_deidentification_not_verified")
    if any(not item.human_reviewed for item in sources.values()):
        blockers.append("source_human_review_missing")

    candidate_ids = {sample.sample_id for sample in spec.candidates}
    references = {item.sample_id: item for item in spec.references}
    if candidate_ids != set(references):
        blockers.append("reference_sample_set_mismatch")
    if any(
        item.annotation_status != "adjudicated"
        or item.annotator_count < 2
        or not item.adjudicated
        for item in references.values()
    ):
        blockers.append("reference_adjudication_incomplete")
    if any(
        item.normalization_version != spec.normalization_version
        for item in references.values()
    ):
        blockers.append("reference_normalization_mismatch")

    clusters = {sample.analysis_cluster_token for sample in spec.candidates}
    unique_blockers = tuple(dict.fromkeys(blockers))
    return BlindPreflightReport(
        status="metadata_ready" if not unique_blockers else "incomplete",
        metadata_sha256=metadata_sha256,
        blockers=unique_blockers,
        sample_count=sample_count,
        unique_duration_ms=unique_duration_ms,
        scenario_duration_ms=scenario_duration_ms,
        cluster_set_sha256=_stable_token_set_sha256(clusters),
        sample_order_sha256=_stable_sequence_sha256(
            tuple(sample.sample_id for sample in spec.candidates)
        ),
    )


def run_blind_preflight(
    *,
    metadata_path: Path,
    output_path: Path,
    repository_root: Path,
) -> BlindPreflightReport:
    """从项目外读取 metadata，原子写入不可覆盖的受限预检报告。"""
    resolved_repository = repository_root.resolve(strict=True)
    resolved_metadata = metadata_path.resolve(strict=True)
    resolved_output = output_path.resolve(strict=False)
    if resolved_metadata.is_relative_to(resolved_repository) or resolved_output.is_relative_to(
        resolved_repository
    ):
        raise ValueError("preflight metadata and report must be outside the repository")
    if output_path.exists():
        raise FileExistsError(f"preflight report already exists: {output_path}")
    if metadata_path.stat().st_size > _MAX_METADATA_BYTES:
        raise ValueError("preflight metadata exceeds 64 MiB")
    spec = BlindPreflightSpec.model_validate_json(
        metadata_path.read_text(encoding="utf-8")
    )
    report = evaluate_blind_preflight(spec, metadata_sha256=sha256_file(metadata_path))
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_path.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
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
        temporary.replace(output_path)
        output_path.chmod(0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return report
