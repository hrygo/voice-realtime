"""Stage 2–5 executor boundary and the explicit executor registry.

The module intentionally contains no concrete ASR/runtime adapter.  A stage runner
owns scheduling, artifacts and decisions; an executor only owns the lifecycle of a
single concrete runtime and returns opaque, typed observations.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol, runtime_checkable

from voice_realtime.benchmarks.asr.stage_contracts import (
    EvidenceTier,
    FaultEvent,
    FaultKind,
    ScheduleSegment,
    StageModelManifest,
    StageNumber,
)
from voice_realtime.benchmarks.asr.stage_inputs import ResolvedStageInput

StageInputKind = Literal["pcm", "interaction_script"]
FaultOutcome = Literal["applied", "recovered", "failed", "unknown"]
MetricValue = float | int | bool | str

_STAGES = frozenset({2, 3, 4, 5})
_INPUT_KINDS = frozenset({"pcm", "interaction_script"})
_FAULT_KINDS = frozenset({"disconnect", "asr_crash", "finalization_delay"})
_FAULT_OUTCOMES = frozenset({"applied", "recovered", "failed", "unknown"})
_SHA256_LENGTH = 64


class StageExecutorError(RuntimeError):
    """Base class for stable stage executor errors."""

    code = "STAGE_EXECUTOR_ERROR"


class UnknownStageExecutorError(StageExecutorError):
    """The requested executor ID is not present in the explicit registry."""

    code = "UNKNOWN_STAGE_EXECUTOR"

    def __init__(self, executor_id: str) -> None:
        super().__init__(f"{self.code}: unknown stage executor: {executor_id}")


class DuplicateStageExecutorError(StageExecutorError, ValueError):
    """An executor ID was registered more than once."""

    code = "DUPLICATE_STAGE_EXECUTOR"

    def __init__(self, executor_id: str) -> None:
        super().__init__(f"{self.code}: stage executor already registered: {executor_id}")


class StageExecutorCapabilityError(StageExecutorError):
    """An executor does not satisfy the immutable request capability contract."""

    code = "STAGE_EXECUTOR_CAPABILITY_MISMATCH"


def _require_non_negative_int(value: int, *, label: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")


def _require_non_empty_text(value: str, *, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty")


def _normalize_sha256(value: str, *, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = value.strip().lower()
    if len(normalized) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{label} must be a 64-character SHA-256 hexadecimal digest")
    return normalized


def _freeze_value(value: object) -> object:
    """Recursively copy common mutable containers into immutable values."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                key: _freeze_value(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_value(item) for item in value)
    return value


def _freeze_mapping(value: Mapping[str, object], *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    frozen: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{label} keys must be non-empty strings")
        frozen[key] = _freeze_value(item)
    return MappingProxyType(frozen)


def _freeze_metrics(value: Mapping[str, MetricValue]) -> Mapping[str, MetricValue]:
    if not isinstance(value, Mapping):
        raise TypeError("metrics must be a mapping")
    frozen: dict[str, MetricValue] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError("metrics keys must be non-empty strings")
        if isinstance(item, (bool, int)):
            frozen[key] = item
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(f"metrics[{key}] must be finite")
            frozen[key] = item
        elif isinstance(item, str):
            frozen[key] = item
        else:
            raise TypeError(f"metrics[{key}] must be a scalar observation value")
    return MappingProxyType(frozen)


@dataclass(frozen=True, slots=True)
class StageExecutorCapabilities:
    """Capabilities declared by one executor implementation."""

    supported_stages: frozenset[StageNumber]
    supported_inputs: frozenset[StageInputKind]
    supports_continuation: bool
    supported_faults: frozenset[FaultKind]
    is_synthetic: bool

    def __post_init__(self) -> None:
        stages = frozenset(self.supported_stages)
        inputs = frozenset(self.supported_inputs)
        faults = frozenset(self.supported_faults)
        object.__setattr__(self, "supported_stages", stages)
        object.__setattr__(self, "supported_inputs", inputs)
        object.__setattr__(self, "supported_faults", faults)
        if not stages:
            raise ValueError("supported_stages must be non-empty")
        if invalid_stages := stages - _STAGES:
            raise ValueError(
                "supported_stages contains unsupported stage: "
                f"{sorted(invalid_stages)}"
            )
        if not inputs:
            raise ValueError("supported_inputs must be non-empty")
        if invalid_inputs := inputs - _INPUT_KINDS:
            raise ValueError(
                "supported_inputs contains unsupported input: "
                f"{sorted(invalid_inputs)}"
            )
        if invalid_faults := faults - _FAULT_KINDS:
            raise ValueError(
                "supported_faults contains unsupported fault: "
                f"{sorted(invalid_faults)}"
            )
        if type(self.supports_continuation) is not bool:
            raise TypeError("supports_continuation must be bool")
        if type(self.is_synthetic) is not bool:
            raise TypeError("is_synthetic must be bool")


@dataclass(frozen=True, slots=True)
class CursorRange:
    """A half-open, non-negative canonical audio cursor range in milliseconds."""

    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        _require_non_negative_int(self.start_ms, label="start_ms")
        _require_non_negative_int(self.end_ms, label="end_ms")
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must be greater than or equal to start_ms")

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True, slots=True)
class ValidatedRuntimeInputs:
    """Validated runtime inputs kept in memory and never written to artifacts."""

    model_root: Path = field(repr=False)
    model_manifest: StageModelManifest
    profile: Mapping[str, object] = field(repr=False)
    runtime_config: Mapping[str, object] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.model_root, Path):
            raise TypeError("model_root must be a Path")
        if not isinstance(self.model_manifest, StageModelManifest):
            raise TypeError("model_manifest must be a StageModelManifest")
        object.__setattr__(self, "profile", _freeze_mapping(self.profile, label="profile"))
        object.__setattr__(
            self,
            "runtime_config",
            _freeze_mapping(self.runtime_config, label="runtime_config"),
        )


@dataclass(frozen=True, slots=True)
class StageExecutionContext:
    """Non-sensitive context shared by an executor lifecycle."""

    run_id: str
    stage: StageNumber
    covered_stages: tuple[StageNumber, ...]
    family_id: str
    candidate_id: str
    evidence_tier: EvidenceTier
    identity_sha256s: Mapping[str, str]
    runtime_inputs: ValidatedRuntimeInputs = field(repr=False)

    def __post_init__(self) -> None:
        _require_non_empty_text(self.run_id, label="run_id")
        _require_non_empty_text(self.family_id, label="family_id")
        _require_non_empty_text(self.candidate_id, label="candidate_id")
        if self.stage not in _STAGES:
            raise ValueError(f"unsupported stage: {self.stage}")
        covered = tuple(self.covered_stages)
        if not covered or len(covered) != len(set(covered)):
            raise ValueError("covered_stages must be non-empty and unique")
        if any(item not in _STAGES for item in covered):
            raise ValueError("covered_stages contains unsupported stage")
        if self.stage != 5 and covered != (self.stage,):
            raise ValueError("covered_stages must contain only the physical stage")
        if self.stage == 5 and covered not in ((5,), (3, 5)):
            raise ValueError("Stage 5 covered_stages must be (5,) or (3, 5)")
        if covered == (3, 5) and self.family_id != "meeting":
            raise ValueError("covered_stages (3, 5) requires family_id meeting")
        if self.evidence_tier not in {"formal", "experimental"}:
            raise ValueError(f"unsupported evidence tier: {self.evidence_tier}")
        identities: dict[str, str] = {}
        for key, value in self.identity_sha256s.items():
            _require_non_empty_text(key, label="identity key")
            identities[key] = _normalize_sha256(value, label=f"identity[{key}] SHA-256")
        object.__setattr__(self, "covered_stages", covered)
        object.__setattr__(self, "identity_sha256s", MappingProxyType(identities))


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    """Opaque identity of one physical runtime session."""

    session_id: str
    process_ids: tuple[int, ...] = ()
    source_epoch: int = 0

    def __post_init__(self) -> None:
        _require_non_empty_text(self.session_id, label="session_id")
        _require_non_negative_int(self.source_epoch, label="source_epoch")
        process_ids = tuple(self.process_ids)
        for process_id in process_ids:
            _require_non_negative_int(process_id, label="process_id")
        if len(process_ids) != len(set(process_ids)):
            raise ValueError("process_ids must be unique")
        object.__setattr__(self, "process_ids", process_ids)


@dataclass(frozen=True, slots=True)
class SegmentObservation:
    """One feed result; cursor and identity are opaque to decision evaluators."""

    segment_id: str
    repetition_index: int
    slice_index: int
    cursor: CursorRange
    session_id: str
    source_epoch: int
    metrics: Mapping[str, MetricValue]

    def __post_init__(self) -> None:
        _require_non_empty_text(self.segment_id, label="segment_id")
        _require_non_negative_int(self.repetition_index, label="repetition_index")
        _require_non_negative_int(self.slice_index, label="slice_index")
        if not isinstance(self.cursor, CursorRange):
            raise TypeError("cursor must be a CursorRange")
        _require_non_empty_text(self.session_id, label="session_id")
        _require_non_negative_int(self.source_epoch, label="source_epoch")
        object.__setattr__(self, "metrics", _freeze_metrics(self.metrics))


@dataclass(frozen=True, slots=True)
class FaultObservation:
    """Result of one runner-scheduled fault injection."""

    event_id: str
    kind: FaultKind
    planned_cursor_ms: int
    actual_cursor_ms: int
    outcome: FaultOutcome
    session_id_before: str
    session_id_after: str
    source_epoch_before: int
    source_epoch_after: int

    def __post_init__(self) -> None:
        _require_non_empty_text(self.event_id, label="event_id")
        if self.kind not in _FAULT_KINDS:
            raise ValueError(f"unsupported fault kind: {self.kind}")
        _require_non_negative_int(self.planned_cursor_ms, label="planned_cursor_ms")
        _require_non_negative_int(self.actual_cursor_ms, label="actual_cursor_ms")
        if self.outcome not in _FAULT_OUTCOMES:
            raise ValueError(f"unsupported fault outcome: {self.outcome}")
        _require_non_empty_text(self.session_id_before, label="session_id_before")
        _require_non_empty_text(self.session_id_after, label="session_id_after")
        _require_non_negative_int(self.source_epoch_before, label="source_epoch_before")
        _require_non_negative_int(self.source_epoch_after, label="source_epoch_after")
        if self.source_epoch_after < self.source_epoch_before:
            raise ValueError("source_epoch_after must not precede source_epoch_before")


@dataclass(frozen=True, slots=True)
class RuntimeObservation:
    """Resource snapshot captured without exposing paths or vendor objects."""

    monotonic_ms: int
    rss_bytes: int
    file_descriptors: int
    background_tasks: int
    queue_depth: int

    def __post_init__(self) -> None:
        _require_non_negative_int(self.monotonic_ms, label="monotonic_ms")
        _require_non_negative_int(self.rss_bytes, label="rss_bytes")
        _require_non_negative_int(self.file_descriptors, label="file_descriptors")
        _require_non_negative_int(self.background_tasks, label="background_tasks")
        _require_non_negative_int(self.queue_depth, label="queue_depth")


@dataclass(frozen=True, slots=True)
class FinalObservation:
    """EOF/terminal observation returned after a runner-controlled finalization."""

    eof_sent: bool
    terminal_received: bool
    finalization_latency_ms: int
    metrics: Mapping[str, MetricValue]
    fault_observation: FaultObservation | None = None

    def __post_init__(self) -> None:
        if type(self.eof_sent) is not bool or type(self.terminal_received) is not bool:
            raise TypeError("eof_sent and terminal_received must be bool")
        _require_non_negative_int(self.finalization_latency_ms, label="finalization_latency_ms")
        if self.terminal_received and not self.eof_sent:
            raise ValueError("terminal_received requires eof_sent")
        if self.fault_observation is not None and not isinstance(
            self.fault_observation, FaultObservation
        ):
            raise TypeError("fault_observation must be FaultObservation or None")
        object.__setattr__(self, "metrics", _freeze_metrics(self.metrics))


@dataclass(frozen=True, slots=True)
class CloseObservation:
    """Resource release audit for one executor-owned runtime."""

    released: bool
    remaining_process_ids: tuple[int, ...] = ()
    remaining_ports: tuple[int, ...] = ()
    remaining_tasks: int = 0
    remaining_connections: int = 0

    def __post_init__(self) -> None:
        if type(self.released) is not bool:
            raise TypeError("released must be bool")
        process_ids = tuple(self.remaining_process_ids)
        ports = tuple(self.remaining_ports)
        for process_id in process_ids:
            _require_non_negative_int(process_id, label="remaining_process_id")
        for port in ports:
            _require_non_negative_int(port, label="remaining_port")
        if len(process_ids) != len(set(process_ids)):
            raise ValueError("remaining_process_ids must be unique")
        _require_non_negative_int(self.remaining_tasks, label="remaining_tasks")
        _require_non_negative_int(self.remaining_connections, label="remaining_connections")
        if self.released and (
            process_ids
            or ports
            or self.remaining_tasks
            or self.remaining_connections
        ):
            raise ValueError("released observation cannot report remaining resources")
        object.__setattr__(self, "remaining_process_ids", process_ids)
        object.__setattr__(self, "remaining_ports", ports)


@runtime_checkable
class StageExecutor(Protocol):
    """Concrete stage runtime adapter owned by ``run_stage``."""

    executor_id: str
    capabilities: StageExecutorCapabilities

    async def prepare(self, context: StageExecutionContext) -> None: ...

    async def start(self, context: StageExecutionContext) -> SessionIdentity: ...

    async def feed_segment(
        self,
        segment: ScheduleSegment,
        resolved_input: ResolvedStageInput,
        cursor_range: CursorRange,
    ) -> SegmentObservation: ...

    async def inject_fault(self, event: FaultEvent) -> FaultObservation: ...

    async def snapshot(self) -> RuntimeObservation: ...

    async def finalize(self, finalization_fault: FaultEvent | None) -> FinalObservation: ...

    async def close(self) -> CloseObservation: ...


def validate_executor_capabilities(
    capabilities: StageExecutorCapabilities,
    *,
    stage: StageNumber,
    input_kind: StageInputKind,
    evidence_tier: EvidenceTier,
    required_faults: Iterable[FaultKind] = (),
    requires_continuation: bool = False,
) -> None:
    """Fail closed when a runtime cannot satisfy a validated stage request."""

    if evidence_tier not in {"formal", "experimental"}:
        raise StageExecutorCapabilityError(
            f"{StageExecutorCapabilityError.code}: unsupported evidence tier {evidence_tier}"
        )
    if evidence_tier == "formal" and capabilities.is_synthetic:
        raise StageExecutorCapabilityError(
            "SYNTHETIC_EXECUTOR_NOT_ALLOWED: formal runs require a real executor"
        )
    if stage not in capabilities.supported_stages:
        raise StageExecutorCapabilityError(
            f"{StageExecutorCapabilityError.code}: stage executor does not support stage {stage}"
        )
    if input_kind not in capabilities.supported_inputs:
        raise StageExecutorCapabilityError(
            f"{StageExecutorCapabilityError.code}: stage executor does not "
            f"support input {input_kind}"
        )
    if requires_continuation and not capabilities.supports_continuation:
        raise StageExecutorCapabilityError(
            f"{StageExecutorCapabilityError.code}: stage executor does not support continuation"
        )
    missing_faults = frozenset(required_faults) - capabilities.supported_faults
    if missing_faults:
        raise StageExecutorCapabilityError(
            f"{StageExecutorCapabilityError.code}: unsupported faults {sorted(missing_faults)}"
        )


StageExecutorFactory = Callable[[], StageExecutor]


class StageExecutorRegistry:
    """Explicit stable-ID factory registry; it never discovers or loads models."""

    def __init__(self) -> None:
        self._factories: dict[str, StageExecutorFactory] = {}

    @staticmethod
    def _normalize_id(executor_id: str) -> str:
        if not isinstance(executor_id, str):
            raise TypeError("executor_id must be a string")
        normalized = executor_id.strip()
        if not normalized:
            raise ValueError("executor_id must be non-empty")
        return normalized

    def register(self, executor_id: str, factory: StageExecutorFactory) -> None:
        normalized = self._normalize_id(executor_id)
        if normalized in self._factories:
            raise DuplicateStageExecutorError(normalized)
        if not callable(factory):
            raise TypeError("stage executor factory must be callable")
        self._factories[normalized] = factory

    def create(self, executor_id: str) -> StageExecutor:
        normalized = self._normalize_id(executor_id)
        factory = self._factories.get(normalized)
        if factory is None:
            raise UnknownStageExecutorError(normalized)
        executor = factory()
        if not isinstance(executor, StageExecutor):
            raise StageExecutorCapabilityError(
                f"{StageExecutorCapabilityError.code}: factory result does not "
                "implement the StageExecutor protocol"
            )
        executor_id_value = getattr(executor, "executor_id", None)
        if executor_id_value != normalized:
            raise StageExecutorCapabilityError(
                f"{StageExecutorCapabilityError.code}: stage executor identity mismatch: "
                f"expected {normalized}, received {executor_id_value}"
            )
        capabilities = getattr(executor, "capabilities", None)
        if not isinstance(capabilities, StageExecutorCapabilities):
            raise StageExecutorCapabilityError(
                f"{StageExecutorCapabilityError.code}: invalid stage executor capabilities"
            )
        return executor


__all__ = [
    "CloseObservation",
    "CursorRange",
    "DuplicateStageExecutorError",
    "FaultObservation",
    "FaultOutcome",
    "FinalObservation",
    "MetricValue",
    "RuntimeObservation",
    "SegmentObservation",
    "SessionIdentity",
    "StageExecutionContext",
    "StageExecutor",
    "StageExecutorCapabilities",
    "StageExecutorCapabilityError",
    "StageExecutorError",
    "StageExecutorFactory",
    "StageExecutorRegistry",
    "StageInputKind",
    "UnknownStageExecutorError",
    "ValidatedRuntimeInputs",
    "validate_executor_capabilities",
]
