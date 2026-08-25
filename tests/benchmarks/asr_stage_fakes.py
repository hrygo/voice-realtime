"""Synthetic stage executor used only by stage-runner tests.

This module is deliberately under ``tests``.  Production registries must never
import it or silently fall back to a synthetic runtime.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from voice_realtime.benchmarks.asr.stage_contracts import (
    FaultEvent,
    FaultPlan,
    PCMInputBinding,
    ScheduleManifest,
    SchedulePurpose,
    ScheduleSegment,
    StageInputManifest,
    StageModelFile,
    StageModelManifest,
    StageNumber,
    StagePhase,
)
from voice_realtime.benchmarks.asr.stage_evaluators import (
    ScreenDecision,
    StagePolicy,
)
from voice_realtime.benchmarks.asr.stage_executors import (
    CloseObservation,
    CursorRange,
    FaultObservation,
    FaultOutcome,
    FinalObservation,
    RuntimeObservation,
    SegmentObservation,
    SessionIdentity,
    StageExecutionContext,
    StageExecutorCapabilities,
)
from voice_realtime.benchmarks.asr.stage_inputs import ResolvedStageInput
from voice_realtime.benchmarks.asr.stage_runner import StageRunRequest


class SyntheticStageExecutor:
    """Deterministic lifecycle fake for unit tests; never a production fallback."""

    executor_id = "test-synthetic"
    capabilities = StageExecutorCapabilities(
        supported_stages=frozenset({2, 3, 4, 5}),
        supported_inputs=frozenset({"pcm", "interaction_script"}),
        supports_continuation=True,
        supported_faults=frozenset({"disconnect", "asr_crash", "finalization_delay"}),
        is_synthetic=True,
    )

    def __init__(
        self,
        *,
        executor_id: str | None = None,
        capabilities: StageExecutorCapabilities | None = None,
        segment_observations: Mapping[str, SegmentObservation]
        | Sequence[SegmentObservation]
        | None = None,
        fault_observations: Mapping[str, FaultObservation] | None = None,
        runtime_observation: RuntimeObservation | None = None,
        final_observation: FinalObservation | None = None,
        close_observation: CloseObservation | None = None,
        observation: CloseObservation | None = None,
        fault_outcomes: FaultOutcome | Mapping[str, FaultOutcome] = "recovered",
        fault_overrides: Mapping[str, FaultOutcome] | None = None,
        fail_on: str | Iterable[str] | None = None,
    ) -> None:
        if executor_id is not None:
            self.executor_id = executor_id
        if capabilities is not None:
            self.capabilities = capabilities
        if segment_observations is None:
            self._segment_observations: (
                Mapping[str, SegmentObservation] | tuple[SegmentObservation, ...]
            ) = {}
        elif isinstance(segment_observations, Mapping):
            self._segment_observations = dict(segment_observations)
        else:
            self._segment_observations = tuple(segment_observations)
        self._fault_observations = dict(fault_observations or {})
        self._runtime_observation = runtime_observation or RuntimeObservation(
            monotonic_ms=0,
            rss_bytes=0,
            file_descriptors=0,
            background_tasks=0,
            queue_depth=0,
        )
        self._final_observation = final_observation or FinalObservation(
            eof_sent=True,
            terminal_received=True,
            finalization_latency_ms=0,
            metrics={},
        )
        self._close_observation = (
            close_observation or observation or CloseObservation(released=True)
        )
        self._fault_outcomes = fault_outcomes
        self._fault_overrides = dict(fault_overrides or {})
        if fail_on is None:
            self._fail_on = frozenset()
        elif isinstance(fail_on, str):
            self._fail_on = frozenset({fail_on})
        else:
            self._fail_on = frozenset(fail_on)

        self.calls: list[str] = []
        self.close_count = 0
        self.start_count = 0
        self.session_ids: tuple[str, ...] = ()
        self.fed_segment_ids: tuple[str, ...] = ()
        self.cursor_ranges: tuple[CursorRange, ...] = ()
        self.injected_faults: tuple[tuple[str, int], ...] = ()
        self.finalize_order: tuple[str, ...] = ()
        self._session: SessionIdentity | None = None
        self._segment_counts: dict[str, int] = {}
        self._sequence_index = 0

    def _maybe_fail(self, method: str) -> None:
        if method in self._fail_on:
            raise RuntimeError(f"synthetic executor failure: {method}")

    async def prepare(self, context: StageExecutionContext) -> None:
        del context
        self._maybe_fail("prepare")
        self.calls.append("prepare")

    async def start(self, context: StageExecutionContext) -> SessionIdentity:
        del context
        self._maybe_fail("start")
        self.calls.append("start")
        self.start_count += 1
        session = SessionIdentity(
            session_id=f"synthetic-session-{self.start_count}",
            source_epoch=self.start_count,
        )
        self._session = session
        self.session_ids = (*self.session_ids, session.session_id)
        return session

    async def feed_segment(
        self,
        segment: ScheduleSegment,
        resolved_input: ResolvedStageInput,
        cursor_range: CursorRange,
    ) -> SegmentObservation:
        self._maybe_fail("feed_segment")
        if self._session is None:
            raise RuntimeError("synthetic executor feed requires start")
        if resolved_input.segment_id != segment.segment_id:
            raise ValueError("synthetic input segment mismatch")
        self.calls.append("feed_segment")
        self.fed_segment_ids = (*self.fed_segment_ids, segment.segment_id)
        self.cursor_ranges = (*self.cursor_ranges, cursor_range)
        repetition_index = self._segment_counts.get(segment.segment_id, 0)
        self._segment_counts[segment.segment_id] = repetition_index + 1
        if isinstance(self._segment_observations, Mapping):
            preset = self._segment_observations.get(segment.segment_id)
        else:
            preset = (
                self._segment_observations[self._sequence_index]
                if self._sequence_index < len(self._segment_observations)
                else None
            )
            self._sequence_index += 1
        if preset is not None:
            return preset
        return SegmentObservation(
            segment_id=segment.segment_id,
            repetition_index=repetition_index,
            slice_index=0,
            cursor=cursor_range,
            session_id=self._session.session_id,
            source_epoch=self._session.source_epoch,
            metrics={"duration_ms": cursor_range.duration_ms},
        )

    async def inject_fault(self, event: FaultEvent) -> FaultObservation:
        self._maybe_fail("inject_fault")
        if self._session is None:
            raise RuntimeError("synthetic executor fault requires start")
        self.calls.append("inject_fault")
        self.injected_faults = (*self.injected_faults, (event.event_id, event.cursor_ms))
        preset = self._fault_observations.get(event.event_id)
        if preset is not None:
            return preset
        if event.event_id in self._fault_overrides:
            outcome = self._fault_overrides[event.event_id]
        elif event.kind in self._fault_overrides:
            outcome = self._fault_overrides[event.kind]
        elif isinstance(self._fault_outcomes, Mapping):
            outcome = self._fault_outcomes.get(
                event.event_id,
                self._fault_outcomes.get(event.kind, "recovered"),
            )
        else:
            outcome = self._fault_outcomes
        return FaultObservation(
            event_id=event.event_id,
            kind=event.kind,
            planned_cursor_ms=event.cursor_ms,
            actual_cursor_ms=event.cursor_ms,
            outcome=outcome,
            session_id_before=self._session.session_id,
            session_id_after=self._session.session_id,
            source_epoch_before=self._session.source_epoch,
            source_epoch_after=self._session.source_epoch,
        )

    async def snapshot(self) -> RuntimeObservation:
        self._maybe_fail("snapshot")
        self.calls.append("snapshot")
        return self._runtime_observation

    async def finalize(self, finalization_fault: FaultEvent | None) -> FinalObservation:
        self._maybe_fail("finalize")
        self.calls.append("finalize")
        self.finalize_order = ("eof", "delay", "terminal") if finalization_fault else (
            "eof",
            "terminal",
        )
        return self._final_observation

    async def close(self) -> CloseObservation:
        self._maybe_fail("close")
        self.calls.append("close")
        self.close_count += 1
        return self._close_observation



@dataclass(frozen=True)
class StageFixture:
    request: StageRunRequest
    policy: StagePolicy


@dataclass(frozen=True)
class TestStagePolicy:
    __test__ = False

    pass_screen: bool = True

    def phase_for(self, segment: ScheduleSegment) -> StagePhase:
        return cast(StagePhase, segment.purpose)

    def evaluate_screen(
        self, observations: tuple[SegmentObservation, ...]
    ) -> ScreenDecision:
        del observations
        return ScreenDecision.PASS if self.pass_screen else ScreenDecision.FAIL


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_private_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


DEFAULT_SEGMENTS: tuple[tuple[str, SchedulePurpose, int], ...] = (
    ("screen-001", "screen", 1_000),
    ("confirm-001", "confirm", 1_000),
)


def build_stage_fixture(
    tmp_path: Path,
    *,
    lock_path: Path | None = None,
    stage: StageNumber = 2,
    covered_stages: tuple[StageNumber, ...] = (2,),
    segment_specs: tuple[tuple[str, SchedulePurpose, int], ...] = DEFAULT_SEGMENTS,
    fault_plan: FaultPlan | None = None,
) -> StageFixture:
    repo = tmp_path / "repo"
    external = tmp_path / "external"
    input_root = external / "inputs"
    output_root = external / "runs"
    repo.mkdir()
    input_root.mkdir(parents=True)
    output_root.mkdir()
    segments: list[ScheduleSegment] = []
    bindings: list[PCMInputBinding] = []
    for segment_id, purpose, duration_ms in segment_specs:
        path = input_root / f"{segment_id}.pcm"
        with path.open("wb") as stream:
            stream.truncate(duration_ms * 32)
        digest = _sha256(path)
        segments.append(
            ScheduleSegment(
                segment_id=segment_id,
                purpose=purpose,
                input_sha256=digest,
                duration_ms=duration_ms,
                repetition=1,
            )
        )
        bindings.append(
            PCMInputBinding(
                segment_id=segment_id,
                relative_path=path.name,
                input_sha256=digest,
                size_bytes=duration_ms * 32,
                duration_ms=duration_ms,
            )
        )
    schedule = ScheduleManifest(stage=stage, family_id="meeting", segments=tuple(segments))
    schedule_path = external / "schedule.json"
    _write_private_json(schedule_path, schedule.model_dump(mode="json"))
    schedule_hash = _sha256(schedule_path)
    input_manifest_path = external / "inputs.json"
    _write_private_json(
        input_manifest_path,
        StageInputManifest(
            schedule_sha256=schedule_hash,
            bindings=tuple(bindings),
        ).model_dump(mode="json"),
    )
    model_root = external / "model"
    model_root.mkdir()
    model_file = model_root / "weights.bin"
    model_file.write_bytes(b"test-model")
    model_file.chmod(0o600)
    model_manifest_path = external / "model-manifest.json"
    _write_private_json(
        model_manifest_path,
        StageModelManifest(
            model_id="test/model",
            model_revision="test-revision",
            files=(
                StageModelFile(
                    relative_path="weights.bin",
                    sha256=_sha256(model_file),
                    size_bytes=model_file.stat().st_size,
                ),
            ),
        ).model_dump(mode="json"),
    )
    identity_paths = {
        name: external / f"{name}.json"
        for name in ("profile", "runtime-config")
    }
    for name, path in identity_paths.items():
        _write_private_json(path, {"identity": name})
    fault_plan_path = external / "fault-plan.json" if fault_plan is not None else None
    if fault_plan_path is not None:
        _write_private_json(fault_plan_path, fault_plan.model_dump(mode="json"))
    request = StageRunRequest(
        run_id=f"stage{stage}-meeting-test",
        stage=stage,
        covered_stages=covered_stages,
        family_id="meeting",
        arm="finalist" if stage == 5 else "baseline",
        candidate_id="qwen",
        evidence_tier="experimental",
        executor_id="test-synthetic",
        model_manifest_path=model_manifest_path,
        model_root=model_root,
        profile_path=identity_paths["profile"],
        runtime_config_path=identity_paths["runtime-config"],
        schedule_path=schedule_path,
        input_manifest_path=input_manifest_path,
        input_root=input_root,
        output_root=output_root,
        repository_root=repo,
        fault_plan_path=fault_plan_path,
        lock_path=lock_path or external / "host.lock",
        lock_timeout_secs=0.0,
    )
    return StageFixture(request=request, policy=TestStagePolicy())


__all__ = [
    "DEFAULT_SEGMENTS",
    "StageFixture",
    "SyntheticStageExecutor",
    "TestStagePolicy",
    "build_stage_fixture",
]
