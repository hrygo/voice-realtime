"""Finalist-only Stage 2–5 的不可变排程、故障与决策制品契约。"""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

StageNumber = Literal[2, 3, 4, 5]
GateStatus = Literal["passed", "failed", "unsupported", "not_applicable"]
_SHA256_LENGTH = 64
_GIT_SHA_LENGTH = 40


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _validate_hex(value: str, *, length: int, label: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != length or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{label} must be {length} hexadecimal characters")
    return normalized


class ScheduleSegment(_FrozenModel):
    """一个确定性 Screen/Confirm/可靠性输入片段。"""

    segment_id: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    purpose: Literal["screen", "confirm", "system", "interaction", "reliability"]
    input_sha256: str
    duration_ms: int = Field(gt=0)
    repetition: int = Field(ge=1)

    @field_validator("input_sha256")
    @classmethod
    def _input_hash(cls, value: str) -> str:
        return _validate_hex(value, length=_SHA256_LENGTH, label="input_sha256")


class ScheduleManifest(_FrozenModel):
    """固定输入顺序；不承载音频、逐字稿或绝对路径。"""

    schema_version: Literal["1.0"] = "1.0"
    stage: StageNumber
    family_id: str = Field(min_length=1, max_length=200)
    segments: tuple[ScheduleSegment, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_schedule(self) -> Self:
        segment_ids = tuple(segment.segment_id for segment in self.segments)
        if len(segment_ids) != len(set(segment_ids)):
            raise ValueError("schedule segment_id must be unique")
        purposes = tuple(segment.purpose for segment in self.segments)
        if "screen" in purposes and "confirm" in purposes:
            first_confirm = purposes.index("confirm")
            if any(purpose == "screen" for purpose in purposes[first_confirm:]):
                raise ValueError("screen segments must precede confirm segments")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_duration_ms(self) -> int:
        return sum(
            segment.duration_ms * segment.repetition for segment in self.segments
        )


class FaultEvent(_FrozenModel):
    """按 canonical 音频游标触发的一次故障。"""

    event_id: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    cursor_ms: int = Field(ge=0)
    kind: Literal["disconnect", "asr_crash", "finalization_delay"]
    duration_ms: int = Field(default=0, ge=0)


class FaultPlan(_FrozenModel):
    """Stage 5 v1 固定故障预算，避免结果可见后选择故障。"""

    schema_version: Literal["1.0"] = "1.0"
    stage: Literal[5]
    duration_ms: int = Field(gt=0)
    events: tuple[FaultEvent, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_fixed_plan(self) -> Self:
        event_ids = tuple(event.event_id for event in self.events)
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("fault event_id must be unique")
        cursors = tuple(event.cursor_ms for event in self.events)
        if tuple(sorted(cursors)) != cursors or len(cursors) != len(set(cursors)):
            raise ValueError("fault cursors must be unique and increasing")
        if any(event.cursor_ms + event.duration_ms > self.duration_ms for event in self.events):
            raise ValueError("fault event exceeds reliability duration")
        counts = {
            kind: sum(event.kind == kind for event in self.events)
            for kind in ("disconnect", "asr_crash", "finalization_delay")
        }
        if counts != {
            "disconnect": 3,
            "asr_crash": 1,
            "finalization_delay": 1,
        }:
            raise ValueError("Stage 5 requires fixed fault counts: 3 disconnect, 1 crash, 1 delay")
        return self


class StageRunManifest(_FrozenModel):
    """Stage 2–5 一次实验臂的全部可复现身份。"""

    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    stage: StageNumber
    family_id: str = Field(min_length=1, max_length=200)
    arm: Literal["baseline", "finalist"]
    candidate_id: str = Field(min_length=1, max_length=200)
    git_commit: str
    model_sha256: str
    profile_sha256: str
    runtime_config_sha256: str
    schedule_sha256: str
    fault_plan_sha256: str | None = None
    started_at: datetime
    status: Literal["planned", "running", "completed", "failed", "deferred"]

    @field_validator("git_commit")
    @classmethod
    def _git_commit(cls, value: str) -> str:
        return _validate_hex(value, length=_GIT_SHA_LENGTH, label="git_commit")

    @field_validator(
        "model_sha256",
        "profile_sha256",
        "runtime_config_sha256",
        "schedule_sha256",
        "fault_plan_sha256",
    )
    @classmethod
    def _artifact_hash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_hex(value, length=_SHA256_LENGTH, label="artifact SHA-256")

    @field_validator("started_at")
    @classmethod
    def _aware_started_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("started_at must include timezone")
        return value

    @model_validator(mode="after")
    def _require_fault_plan_for_stage5(self) -> Self:
        if self.stage == 5 and self.fault_plan_sha256 is None:
            raise ValueError("fault plan is required for Stage 5")
        return self


class ArtifactIdentity(_FrozenModel):
    """阶段输出中一个相对路径制品的 hash 与大小。"""

    path: str = Field(min_length=1, max_length=1000)
    sha256: str
    size_bytes: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts or not normalized:
            raise ValueError("artifact path must be relative")
        return str(path)

    @field_validator("sha256")
    @classmethod
    def _artifact_hash(cls, value: str) -> str:
        return _validate_hex(value, length=_SHA256_LENGTH, label="artifact SHA-256")


class ArtifactIndex(_FrozenModel):
    """一次阶段运行的不可变输出索引。"""

    schema_version: Literal["1.0"] = "1.0"
    run_manifest_sha256: str
    artifacts: tuple[ArtifactIdentity, ...] = Field(min_length=1)

    @field_validator("run_manifest_sha256")
    @classmethod
    def _manifest_hash(cls, value: str) -> str:
        return _validate_hex(value, length=_SHA256_LENGTH, label="run manifest SHA-256")

    @model_validator(mode="after")
    def _unique_artifacts(self) -> Self:
        paths = tuple(artifact.path for artifact in self.artifacts)
        if len(paths) != len(set(paths)):
            raise ValueError("artifact paths must be unique")
        return self


DecisionStatus = Literal[
    "deferred",
    "not_run",
    "Screen-Pass",
    "Screen-Fail",
    "Confirm-Pass",
    "Finalist / Reliability Pending",
    "Promote",
    "Reject",
    "Experimental / No decision",
]


class StageDecisionReport(_FrozenModel):
    """跨阶段状态机；只有 Stage 5 可以输出 Promote。"""

    schema_version: Literal["1.0"] = "1.0"
    stage: StageNumber
    family_id: str = Field(min_length=1, max_length=200)
    candidate_id: str = Field(min_length=1, max_length=200)
    status: DecisionStatus
    run_manifest_sha256: str
    upstream_report_sha256: str | None = None
    required_hard_gates: tuple[str, ...] = Field(min_length=1)
    hard_gates: dict[str, GateStatus] = Field(default_factory=dict)

    @field_validator("required_hard_gates")
    @classmethod
    def _required_gate_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(gate.strip() for gate in value)
        if any(not gate for gate in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("required hard gate names must be non-empty and unique")
        return normalized

    @field_validator("run_manifest_sha256", "upstream_report_sha256")
    @classmethod
    def _report_hash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_hex(value, length=_SHA256_LENGTH, label="report SHA-256")

    @model_validator(mode="after")
    def _protect_promotion_boundary(self) -> Self:
        if set(self.hard_gates) != set(self.required_hard_gates):
            raise ValueError("hard gates must match the fixed required set")
        if self.status != "Promote":
            return self
        if self.stage != 5:
            raise ValueError("only Stage 5 may emit Promote")
        if not self.hard_gates or any(
            status != "passed" for status in self.hard_gates.values()
        ):
            raise ValueError("Promote requires all hard gates to pass")
        if self.upstream_report_sha256 is None:
            raise ValueError("Promote requires an upstream report")
        return self
