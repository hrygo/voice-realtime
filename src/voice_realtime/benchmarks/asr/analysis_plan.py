"""冻结 Core/Reserve 两次查看的序贯统计计划。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from voice_realtime.benchmarks.asr.manifest import sha256_file

_SHA256_LENGTH = 64


class AnalysisPlan(BaseModel):
    """在 Core 输出可见前冻结的候选、边界、MDE 与随机种子。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    candidate_ids: tuple[str, ...] = Field(min_length=2)
    core_manifest_sha256: str
    reserve_manifest_sha256: str
    core_reference_sha256: str
    reserve_reference_sha256: str
    look_alpha: tuple[float, float] = (0.01, 0.04)
    decision_confidence: tuple[float, float] = (0.99, 0.96)
    conditional_power_futility: float = 0.20
    bootstrap_seeds: tuple[int, int]
    pilot_baseline_cer: float = Field(gt=0, le=1)
    relative_mde: float = 0.05

    @field_validator(
        "core_manifest_sha256",
        "reserve_manifest_sha256",
        "core_reference_sha256",
        "reserve_reference_sha256",
    )
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != _SHA256_LENGTH or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("manifest SHA-256 必须是 64 位十六进制")
        return normalized

    @field_validator("candidate_ids")
    @classmethod
    def _validate_candidates(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(candidate.strip() for candidate in value)
        if any(not candidate for candidate in normalized):
            raise ValueError("candidate_id 不能为空")
        if len(normalized) != len(set(normalized)):
            raise ValueError("candidate_id 必须唯一")
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
        if self.bootstrap_seeds[0] == self.bootstrap_seeds[1]:
            raise ValueError("Core/Reserve bootstrap seed 必须不同")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def minimum_detectable_effect(self) -> float:
        """由 pilot baseline CER 冻结的 5% 相对改善绝对值。"""
        return self.pilot_baseline_cer * self.relative_mde


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
    payload = plan.model_dump_json(indent=2) + "\n"
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
