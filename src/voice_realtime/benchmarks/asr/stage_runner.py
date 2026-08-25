"""Stage 2–5 统一生命周期编排器。

请求和外部证据验证位于 :mod:`stage_validation`；本模块只负责持有资源锁、驱动
executor、维护 canonical cursor/状态以及封存 run 制品。``run_stage`` 是唯一
lock owner，不启动任何未通过 validation 的 runtime。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from voice_realtime.benchmarks.asr.stage_artifacts import (
    StageArtifactError,
    StageArtifactWriter,
)
from voice_realtime.benchmarks.asr.stage_contracts import (
    FaultKind,
    FaultPlan,
    ScheduleManifest,
    ScheduleSegment,
    StagePhase,
    StageRunManifest,
    StageRunState,
    StageStatus,
)
from voice_realtime.benchmarks.asr.stage_evaluators import (
    MeetingStagePolicy,
    ScreenDecision,
    StagePolicy,
    validate_composite_schedule,
    validate_screen_observations,
)
from voice_realtime.benchmarks.asr.stage_executors import (
    CloseObservation,
    CursorRange,
    FinalObservation,
    RuntimeObservation,
    SegmentObservation,
    SessionIdentity,
    StageExecutionContext,
    StageExecutor,
    StageExecutorCapabilities,
    validate_executor_capabilities,
)
from voice_realtime.benchmarks.asr.stage_faults import (
    FaultScheduler,
    execute_fault,
    fault_payload,
    validate_fault_observation,
)
from voice_realtime.benchmarks.asr.stage_inputs import ResolvedStageInput, verify_resolved_input
from voice_realtime.benchmarks.asr.stage_validation import (
    StageEligibilityError,
    StageEligibilityResult,
    StageRequestError,
    StageRunnerError,
    StageRunRequest,
    ValidatedStageRunRequest,
    load_stage_run_request,
    read_stable_file,
    validate_request_for_lock,
    validate_stage_eligibility,
    validate_stage_request,
)
from voice_realtime.benchmarks.resource_lock import (
    ResourceQuarantinedError,
    exclusive_resource_lock,
    require_no_resource_quarantine,
    write_resource_quarantine,
)


class StageStateError(StageRunnerError, ValueError):
    """状态机发生非法跳转。"""

    code = "invalid_state_transition"


@dataclass(frozen=True, slots=True)
class StageRunResult:
    """不泄露绝对路径或原始输入的 terminal 结果。"""

    run_id: str
    status: StageStatus
    covered_stages: tuple[int, ...]
    stop_reason: str
    manifest_sha256: str | None
    artifact_index_sha256: str | None
    executed_fault_counts: Mapping[FaultKind, int]
    stage3_checkpoint_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "covered_stages", tuple(self.covered_stages))
        object.__setattr__(
            self,
            "executed_fault_counts",
            MappingProxyType(dict(self.executed_fault_counts)),
        )


ALLOWED_STATUS_TRANSITIONS: Mapping[StageStatus, frozenset[StageStatus]] = {
    "planned": frozenset({"running", "deferred"}),
    "running": frozenset({"completed", "failed"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "deferred": frozenset(),
}


def validate_status_transition(current: StageStatus, target: StageStatus) -> None:
    """验证一次不可回退、不可 terminal re-entry 的状态转移。"""

    if target not in ALLOWED_STATUS_TRANSITIONS.get(current, frozenset()):
        raise StageStateError(f"illegal stage status transition: {current} -> {target}")


def _state(
    run_id: str,
    *,
    status: StageStatus,
    phase: Literal[
        "planned", "preflight", "screen", "confirm", "reliability", "finalizing", "terminal"
    ],
    cursor_ms: int,
    start_count: int,
    session_id: str | None,
    started_at: datetime | None,
    finished_at: datetime | None,
    stop_reason: str | None = None,
    failure_code: str | None = None,
) -> StageRunState:
    return StageRunState(
        run_id=run_id,
        status=status,
        phase=phase,
        cursor_ms=cursor_ms,
        start_count=start_count,
        session_id=session_id,
        started_at=started_at,
        finished_at=finished_at,
        stop_reason=stop_reason,
        failure_code=failure_code,
    )


def _build_manifest(
    validated: ValidatedStageRunRequest,
    *,
    status: Literal["planned", "running", "completed", "failed", "deferred"],
    started_at: datetime,
) -> StageRunManifest:
    request = validated.request
    return StageRunManifest(
        run_id=request.run_id,
        stage=request.stage,
        covered_stages=request.covered_stages,
        family_id=request.family_id,
        arm=request.arm,
        candidate_id=request.candidate_id,
        evidence_tier=request.evidence_tier,
        executor_id=request.executor_id,
        git_commit=validated.git_commit,
        model_sha256=validated.identity_sha256s["model_manifest"],
        profile_sha256=validated.identity_sha256s["profile"],
        runtime_config_sha256=validated.identity_sha256s["runtime_config"],
        schedule_sha256=validated.identity_sha256s["schedule"],
        fault_plan_sha256=validated.identity_sha256s.get("fault_plan"),
        started_at=started_at,
        status=status,
    )


def _stable_hash(path: Path, *, label: str) -> str:
    return read_stable_file(path, label=label).sha256


def _result(
    validated: ValidatedStageRunRequest,
    writer: StageArtifactWriter,
    *,
    status: StageStatus,
    stop_reason: str,
    executed_fault_counts: Mapping[FaultKind, int] | None = None,
    stage3_checkpoint_sha256: str | None = None,
) -> StageRunResult:
    counts = _normalized_fault_counts(validated, executed_fault_counts or {})
    return StageRunResult(
        run_id=validated.request.run_id,
        status=status,
        covered_stages=validated.request.covered_stages,
        stop_reason=stop_reason,
        manifest_sha256=_stable_hash(writer.run_dir / "manifest.json", label="manifest"),
        artifact_index_sha256=_stable_hash(
            writer.run_dir / "artifact-index.json",
            label="artifact index",
        ),
        executed_fault_counts=counts,
        stage3_checkpoint_sha256=stage3_checkpoint_sha256,
    )


def _normalized_fault_counts(
    validated: ValidatedStageRunRequest,
    executed_fault_counts: Mapping[FaultKind, int],
) -> dict[FaultKind, int]:
    counts: dict[FaultKind, int] = dict(executed_fault_counts)
    if validated.fault_plan is not None:
        for event in validated.fault_plan.events:
            counts.setdefault(event.kind, 0)
    return counts


def _record_deferred(
    validated: ValidatedStageRunRequest,
    eligibility: StageEligibilityResult,
) -> StageRunResult:
    request = validated.request
    now = datetime.now(UTC)
    fault_counts = _normalized_fault_counts(validated, {})
    writer = StageArtifactWriter.create(request.output_root, request.run_id)
    writer.replace_manifest(_build_manifest(validated, status="planned", started_at=now))
    writer.replace_state(
        _state(
            request.run_id,
            status="planned",
            phase="planned",
            cursor_ms=0,
            start_count=0,
            session_id=None,
            started_at=None,
            finished_at=None,
        )
    )
    writer.append_event({"event_kind": "state", "status": "planned", "phase": "planned"})
    validate_status_transition("planned", "deferred")
    writer.replace_manifest(_build_manifest(validated, status="deferred", started_at=now))
    writer.replace_state(
        _state(
            request.run_id,
            status="deferred",
            phase="terminal",
            cursor_ms=0,
            start_count=0,
            session_id=None,
            started_at=None,
            finished_at=datetime.now(UTC),
            stop_reason=eligibility.reason,
        )
    )
    writer.append_event(
        {"event_kind": "terminal", "status": "deferred", "reason": eligibility.reason}
    )
    writer.write_metrics(
        {
            "executed_cursor_ms": 0,
            "start_count": 0,
            "executed_fault_counts": fault_counts,
        }
    )
    writer.write_summary(
        {
            "status": "deferred",
            "reason": eligibility.reason,
            "covered_stages": list(request.covered_stages),
            "executed_fault_counts": fault_counts,
        }
    )
    writer.ensure_empty_streams()
    writer.seal()
    return _result(
        validated,
        writer,
        status="deferred",
        stop_reason=eligibility.reason,
        executed_fault_counts=fault_counts,
    )


def _input_kind(resolved: ResolvedStageInput) -> Literal["pcm", "interaction_script"]:
    return "interaction_script" if hasattr(resolved, "actions") else "pcm"


def _validate_executor(
    request: StageRunRequest,
    resolved_inputs: tuple[ResolvedStageInput, ...],
    schedule: ScheduleManifest,
    executor: StageExecutor,
    fault_plan: FaultPlan | None,
) -> None:
    if not isinstance(executor, StageExecutor):
        raise StageRequestError("executor factory did not return StageExecutor")
    if executor.executor_id != request.executor_id:
        raise StageRequestError("stage executor identity mismatch")
    capabilities = executor.capabilities
    if not isinstance(capabilities, StageExecutorCapabilities):
        raise StageRequestError("stage executor capabilities are invalid")
    required_faults = (
        frozenset(event.kind for event in fault_plan.events)
        if fault_plan is not None
        else frozenset()
    )
    for kind in frozenset(_input_kind(item) for item in resolved_inputs):
        validate_executor_capabilities(
            capabilities,
            stage=request.stage,
            input_kind=kind,
            evidence_tier=request.evidence_tier,
            required_faults=required_faults,
            requires_continuation=any(
                segment.purpose == "confirm" for segment in schedule.segments
            ),
        )


def _observation_matches(
    observation: SegmentObservation,
    segment: ScheduleSegment,
    cursor: CursorRange,
    session: SessionIdentity,
    *,
    repetition_index: int,
    slice_index: int,
) -> None:
    if observation.segment_id != segment.segment_id:
        raise StageRunnerError("executor observation segment mismatch")
    if observation.repetition_index != repetition_index:
        raise StageRunnerError("executor observation repetition mismatch")
    if observation.slice_index != slice_index:
        raise StageRunnerError("executor observation slice mismatch")
    if observation.cursor != cursor:
        raise StageRunnerError("executor observation cursor mismatch")
    if observation.session_id != session.session_id:
        raise StageRunnerError("executor observation session mismatch")
    if observation.source_epoch != session.source_epoch:
        raise StageRunnerError("executor observation source epoch mismatch")


def _unknown_close() -> CloseObservation:
    return CloseObservation(
        released=False,
        remaining_process_ids=(),
        remaining_ports=(),
        remaining_tasks=1,
        remaining_connections=1,
    )


async def _close_executor(
    executor: StageExecutor,
) -> tuple[CloseObservation, Exception | None]:
    """只调用一次 close，并把非法/异常 close 归为释放未知。"""

    try:
        observation = await executor.close()
    except Exception as exc:
        return _unknown_close(), exc
    if not isinstance(observation, CloseObservation):
        return _unknown_close(), StageRunnerError("executor returned invalid close observation")
    return observation, None


def _is_meeting_composite(
    validated: ValidatedStageRunRequest,
    policy: StagePolicy,
) -> bool:
    return isinstance(policy, MeetingStagePolicy) and policy.is_stage5_composite(
        covered_stages=validated.request.covered_stages,
        family_id=validated.request.family_id,
    )


def _logical_segments(
    observations: list[SegmentObservation],
    *,
    end_ms: int | None = None,
) -> list[dict[str, object]]:
    grouped: dict[tuple[str, int], dict[str, object]] = {}
    for observation in observations:
        if end_ms is not None and observation.cursor.end_ms > end_ms:
            continue
        key = (observation.segment_id, observation.repetition_index)
        logical = grouped.setdefault(
            key,
            {
                "segment_id": observation.segment_id,
                "repetition_index": observation.repetition_index,
                "slices": [],
            },
        )
        slices = logical["slices"]
        if not isinstance(slices, list):
            raise StageRunnerError("logical segment slice accumulator is invalid")
        slices.append(
            {
                "slice_index": observation.slice_index,
                "cursor_start_ms": observation.cursor.start_ms,
                "cursor_end_ms": observation.cursor.end_ms,
                "session_id": observation.session_id,
                "source_epoch": observation.source_epoch,
                "metrics": dict(observation.metrics),
            }
        )
    return list(grouped.values())


def _write_stage3_checkpoint(
    writer: StageArtifactWriter,
    *,
    session: SessionIdentity,
    observations: list[SegmentObservation],
    policy: MeetingStagePolicy,
) -> None:
    stage3_window = policy.stage3_window
    logical_segments = _logical_segments(
        observations,
        end_ms=stage3_window.end_ms,
    )
    writer.write_stage_checkpoint(
        3,
        {
            "stage": 3,
            "window": {
                "start_ms": stage3_window.start_ms,
                "end_ms": stage3_window.end_ms,
            },
            "cursor_ms": stage3_window.end_ms,
            "session_id": session.session_id,
            "source_epoch": session.source_epoch,
            "observation_count": sum(
                len(item["slices"])
                for item in logical_segments
                if isinstance(item["slices"], list)
            ),
        },
    )
    writer.write_stage_metrics(
        3,
        {
            "stage": 3,
            "window": {
                "start_ms": stage3_window.start_ms,
                "end_ms": stage3_window.end_ms,
            },
            "canonical_audio_duration_ms": stage3_window.end_ms,
            "logical_segments": logical_segments,
        },
    )


async def _run_locked(
    validated: ValidatedStageRunRequest,
    executor: StageExecutor,
    policy: StagePolicy,
    writer: StageArtifactWriter,
) -> StageRunResult:
    request = validated.request
    started_at = datetime.now(UTC)

    # Initial snapshots are deliberately outside the lifecycle catch. If the
    # writer is already broken, no fake terminal/index is produced.
    writer.replace_manifest(_build_manifest(validated, status="planned", started_at=started_at))
    writer.replace_state(
        _state(
            request.run_id,
            status="planned",
            phase="planned",
            cursor_ms=0,
            start_count=0,
            session_id=None,
            started_at=None,
            finished_at=None,
        )
    )
    writer.append_event({"event_kind": "state", "status": "planned", "phase": "planned"})
    validate_status_transition("planned", "running")
    writer.replace_manifest(_build_manifest(validated, status="running", started_at=started_at))
    writer.replace_state(
        _state(
            request.run_id,
            status="running",
            phase="preflight",
            cursor_ms=0,
            start_count=0,
            session_id=None,
            started_at=started_at,
            finished_at=None,
        )
    )
    writer.append_event({"event_kind": "state", "status": "running", "phase": "preflight"})
    writer.ensure_empty_streams()

    session: SessionIdentity | None = None
    cursor = 0
    screen_observations: list[SegmentObservation] = []
    runtime_observation: RuntimeObservation | None = None
    final_observation: FinalObservation | None = None
    lifecycle_error: Exception | None = None
    screen_failed = False
    fault_not_recovered = False
    fault_counts: dict[FaultKind, int] = {}
    fault_scheduler = FaultScheduler.from_plan(validated.fault_plan)
    meeting_composite = _is_meeting_composite(validated, policy)
    meeting_policy: MeetingStagePolicy | None = (
        policy if isinstance(policy, MeetingStagePolicy) and meeting_composite else None
    )
    stage3_checkpoint_written = False
    segment_observations: list[SegmentObservation] = []

    try:
        context = StageExecutionContext(
            run_id=request.run_id,
            stage=request.stage,
            covered_stages=request.covered_stages,
            family_id=request.family_id,
            candidate_id=request.candidate_id,
            evidence_tier=request.evidence_tier,
            identity_sha256s=validated.identity_sha256s,
            runtime_inputs=validated.runtime_inputs,
        )
        await executor.prepare(context)
        candidate_session = await executor.start(context)
        if not isinstance(candidate_session, SessionIdentity):
            raise StageRunnerError("executor returned invalid session identity")
        session = candidate_session
        if validated.fault_plan is not None and (
            validated.schedule.total_duration_ms != validated.fault_plan.duration_ms
        ):
            raise StageRunnerError("Stage 5 schedule duration does not match fault plan")
        initial_policy_phase = policy.phase_for(validated.schedule.segments[0])
        initial_phase: StagePhase = (
            initial_policy_phase
            if initial_policy_phase in {"preflight", "screen", "confirm", "reliability"}
            else "reliability"
        )
        writer.replace_state(
            _state(
                request.run_id,
                status="running",
                phase=initial_phase,
                cursor_ms=0,
                start_count=1,
                session_id=session.session_id,
                started_at=started_at,
                finished_at=None,
            )
        )
        writer.append_event(
            {
                "event_kind": "started",
                "start_count": 1,
                "session_id": session.session_id,
                "source_epoch": session.source_epoch,
            }
        )
        leading_screen_count = 0
        for segment in validated.schedule.segments:
            if segment.purpose == "screen":
                leading_screen_count += 1
            else:
                break
        for segment_index, (segment, resolved) in enumerate(
            zip(validated.schedule.segments, validated.resolved_inputs, strict=True)
        ):
            phase = policy.phase_for(segment)
            if phase not in {
                "preflight",
                "screen",
                "confirm",
                "reliability",
                "interaction",
                "system",
            }:
                raise StageRunnerError("stage policy returned invalid phase")
            for repetition_index in range(segment.repetition):
                verify_resolved_input(resolved)
                segment_end = cursor + segment.duration_ms
                slice_index = 0
                while True:
                    cursor_range = fault_scheduler.next_range(cursor, segment_end)
                    if (
                        meeting_composite
                        and not stage3_checkpoint_written
                        and meeting_policy is not None
                        and cursor < meeting_policy.stage3_window.end_ms < cursor_range.end_ms
                    ):
                        cursor_range = CursorRange(
                            cursor,
                            meeting_policy.stage3_window.end_ms,
                        )
                    if cursor_range.duration_ms == 0:
                        event = fault_scheduler.pop_due(cursor)
                        if event is None:
                            break
                        session, outcome = await execute_fault(
                            executor,
                            writer,
                            event,
                            cursor=cursor,
                            session=session,
                        )
                        fault_counts.setdefault(event.kind, 0)
                        if outcome == "recovered":
                            fault_counts[event.kind] += 1
                        else:
                            fault_not_recovered = True
                        writer.append_event(
                            {
                                "event_kind": "fault",
                                "event_id": event.event_id,
                                "kind": event.kind,
                                "cursor_ms": cursor,
                                "outcome": outcome,
                                "session_id": session.session_id,
                                "source_epoch": session.source_epoch,
                            }
                        )
                        if fault_not_recovered:
                            break
                        continue
                    if (
                        _input_kind(resolved) == "interaction_script"
                        and cursor_range.duration_ms != segment.duration_ms
                    ):
                        raise StageRunnerError(
                            "interaction script cannot be sliced across a fault"
                        )
                    verify_resolved_input(resolved)
                    observation = await executor.feed_segment(segment, resolved, cursor_range)
                    if not isinstance(observation, SegmentObservation):
                        raise StageRunnerError("executor returned invalid segment observation")
                    _observation_matches(
                        observation,
                        segment,
                        cursor_range,
                        session,
                        repetition_index=repetition_index,
                        slice_index=slice_index,
                    )
                    cursor = cursor_range.end_ms
                    segment_observations.append(observation)
                    if segment_index < leading_screen_count:
                        screen_observations.append(observation)
                    # Persist the canonical cursor immediately after each feed;
                    # a crash after this point cannot leave state behind the audio.
                    state_phase: StagePhase = (
                        phase
                        if phase in {"preflight", "screen", "confirm", "reliability"}
                        else "reliability"
                    )
                    writer.replace_state(
                        _state(
                            request.run_id,
                            status="running",
                            phase=state_phase,
                            cursor_ms=cursor,
                            start_count=1,
                            session_id=session.session_id,
                            started_at=started_at,
                            finished_at=None,
                        )
                    )
                    writer.append_event(
                        {
                            "event_kind": "feed",
                            "segment_id": segment.segment_id,
                            "repetition_index": repetition_index,
                            "slice_index": observation.slice_index,
                            "cursor_start_ms": cursor_range.start_ms,
                            "cursor_end_ms": cursor_range.end_ms,
                            "session_id": session.session_id,
                            "source_epoch": session.source_epoch,
                        }
                    )
                    if (
                        meeting_composite
                        and meeting_policy is not None
                        and cursor == meeting_policy.stage3_window.end_ms
                        and not stage3_checkpoint_written
                    ):
                        _write_stage3_checkpoint(
                            writer,
                            session=session,
                            observations=segment_observations,
                            policy=meeting_policy,
                        )
                        stage3_checkpoint_written = True
                        writer.append_event(
                            {
                                "event_kind": "stage3_checkpoint",
                                "cursor_ms": cursor,
                                "session_id": session.session_id,
                                "source_epoch": session.source_epoch,
                            }
                        )
                    slice_index += 1
                if fault_not_recovered:
                    break
            if segment_index + 1 == leading_screen_count and leading_screen_count:
                validate_screen_observations(screen_observations)
                decision = policy.evaluate_screen(tuple(screen_observations))
                if decision not in {ScreenDecision.PASS, ScreenDecision.FAIL}:
                    raise StageRunnerError("stage policy returned invalid Screen decision")
                writer.append_event(
                    {
                        "event_kind": "screen_decision",
                        "decision": decision.value,
                        "cursor_ms": cursor,
                    }
                )
                if decision == ScreenDecision.FAIL:
                    screen_failed = True
                    writer.replace_state(
                        _state(
                            request.run_id,
                            status="running",
                            phase="finalizing",
                            cursor_ms=cursor,
                            start_count=1,
                            session_id=session.session_id,
                            started_at=started_at,
                            finished_at=None,
                            stop_reason="screen_fail",
                        )
                    )
                    break
                writer.replace_state(
                    _state(
                        request.run_id,
                        status="running",
                        phase="confirm",
                        cursor_ms=cursor,
                        start_count=1,
                        session_id=session.session_id,
                        started_at=started_at,
                        finished_at=None,
                    )
                )
            if fault_not_recovered:
                break
        if fault_scheduler.pending and not fault_not_recovered and not screen_failed:
            raise StageRunnerError("fault plan extends beyond the schedule")
        if meeting_composite and not stage3_checkpoint_written and not fault_not_recovered:
            raise StageRunnerError("Stage 3 checkpoint cursor was not reached")
        if session is None:
            raise StageRunnerError("executor did not produce a session")
        candidate_runtime = await executor.snapshot()
        if not isinstance(candidate_runtime, RuntimeObservation):
            raise StageRunnerError("executor returned invalid runtime observation")
        runtime_observation = candidate_runtime
        finalization_fault = fault_scheduler.finalization_fault
        if finalization_fault is not None and cursor != finalization_fault.cursor_ms:
            # A non-recovered mid-run fault stops canonical audio before EOF;
            # finalize the active session safely without claiming the EOF delay
            # was attempted.
            finalization_fault = None
        if finalization_fault is not None:
            writer.append_fault(
                fault_payload(finalization_fault, state="planned", session=session)
            )
            writer.append_fault(
                fault_payload(finalization_fault, state="attempt_started", session=session)
            )
        try:
            candidate_final = await executor.finalize(finalization_fault)
            if not isinstance(candidate_final, FinalObservation):
                raise StageRunnerError("executor returned invalid final observation")
            if finalization_fault is None and candidate_final.fault_observation is not None:
                raise StageRunnerError(
                    "executor returned an unexpected finalization fault observation"
                )
        except Exception:
            if finalization_fault is not None:
                writer.append_fault(
                    fault_payload(
                        finalization_fault,
                        state="applied",
                        session=session,
                        observation_available=False,
                        outcome="unknown",
                    )
                )
                writer.append_fault(
                    fault_payload(
                        finalization_fault,
                        state="unknown",
                        session=session,
                        observation_available=False,
                        outcome="unknown",
                    )
                )
            raise
        final_observation = candidate_final
        if finalization_fault is not None:
            finalization_observation = candidate_final.fault_observation
            if finalization_observation is None:
                writer.append_fault(
                    fault_payload(
                        finalization_fault,
                        state="applied",
                        session=session,
                        observation_available=False,
                        outcome="unknown",
                    )
                )
                writer.append_fault(
                    fault_payload(
                        finalization_fault,
                        state="unknown",
                        session=session,
                        observation_available=False,
                        outcome="unknown",
                    )
                )
                fault_not_recovered = True
            else:
                try:
                    next_session = validate_fault_observation(
                        finalization_observation,
                        finalization_fault,
                        cursor=cursor,
                        session=session,
                    )
                except Exception:
                    writer.append_fault(
                        fault_payload(
                            finalization_fault,
                            state="applied",
                            observation=finalization_observation,
                            observation_available=False,
                            outcome="unknown",
                        )
                    )
                    writer.append_fault(
                        fault_payload(
                            finalization_fault,
                            state="unknown",
                            observation=finalization_observation,
                            observation_available=False,
                            outcome="unknown",
                        )
                    )
                    raise
                session = (
                    next_session
                    if finalization_observation.outcome == "recovered"
                    else session
                )
                writer.append_fault(
                    fault_payload(
                        finalization_fault,
                        state="applied",
                        observation=finalization_observation,
                    )
                )
                writer.append_fault(
                    fault_payload(
                        finalization_fault,
                        state=finalization_observation.outcome,
                        observation=finalization_observation,
                    )
                )
                fault_counts.setdefault(finalization_fault.kind, 0)
                if finalization_observation.outcome == "recovered":
                    fault_counts[finalization_fault.kind] += 1
                else:
                    fault_not_recovered = True
    except Exception as exc:
        lifecycle_error = exc

    close_observation, close_error = await _close_executor(executor)
    close_clean = close_error is None and close_observation.released
    if not close_clean:
        # This write intentionally happens before terminal sealing. A failure
        # to publish the marker propagates and cannot be reported as clean.
        write_resource_quarantine(
            request.quarantine_path,
            run_id=request.run_id,
            executor_id=request.executor_id,
            observation=close_observation,
        )

    if isinstance(lifecycle_error, StageArtifactError):
        raise lifecycle_error

    if lifecycle_error is not None:
        failure = lifecycle_error
        stop_reason = "execution_failed"
    elif close_error is not None or not close_clean:
        failure = close_error or StageRunnerError("resource cleanup incomplete")
        stop_reason = "resource_cleanup_incomplete"
    elif final_observation is None or not (
        final_observation.eof_sent and final_observation.terminal_received
    ):
        failure = StageRunnerError("finalization incomplete")
        stop_reason = "finalization_incomplete"
    elif screen_failed:
        failure = None
        stop_reason = "screen_fail"
    elif fault_not_recovered:
        failure = None
        stop_reason = "fault_not_recovered"
    else:
        failure = None
        stop_reason = "schedule_complete"

    status: Literal["completed", "failed"] = "completed" if failure is None else "failed"
    return await _seal_terminal(
        validated,
        writer,
        started_at=started_at,
        cursor=cursor,
        session=session,
        status=status,
        stop_reason=stop_reason,
        failure_code=None if failure is None else type(failure).__name__,
        runtime_observation=runtime_observation,
        final_observation=final_observation,
        failure=failure,
        executed_fault_counts=fault_counts,
        meeting_composite=meeting_composite,
        meeting_policy=meeting_policy,
        observations=segment_observations,
    )


async def _seal_terminal(
    validated: ValidatedStageRunRequest,
    writer: StageArtifactWriter,
    *,
    started_at: datetime,
    cursor: int,
    session: SessionIdentity | None,
    status: Literal["completed", "failed"],
    stop_reason: str,
    failure_code: str | None,
    runtime_observation: RuntimeObservation | None,
    final_observation: FinalObservation | None,
    failure: Exception | None,
    executed_fault_counts: Mapping[FaultKind, int],
    meeting_composite: bool,
    meeting_policy: MeetingStagePolicy | None,
    observations: list[SegmentObservation],
) -> StageRunResult:
    validate_status_transition("running", status)
    normalized_fault_counts = _normalized_fault_counts(
        validated,
        executed_fault_counts,
    )
    finished_at = datetime.now(UTC)
    if failure is not None:
        writer.append_failure(
            {
                "error_type": type(failure).__name__,
                "error": str(failure),
                "failure_code": failure_code or "execution_failed",
            }
        )
    writer.append_event(
        {
            "event_kind": "terminal",
            "status": status,
            "stop_reason": stop_reason,
            "cursor_ms": cursor,
        }
    )
    writer.replace_state(
        _state(
            validated.request.run_id,
            status=status,
            phase="terminal",
            cursor_ms=cursor,
            start_count=1 if session is not None else 0,
            session_id=session.session_id if session is not None else None,
            started_at=started_at,
            finished_at=finished_at,
            stop_reason=stop_reason,
            failure_code=failure_code,
        )
    )
    writer.replace_manifest(_build_manifest(validated, status=status, started_at=started_at))
    metrics: dict[str, object] = {
        "executed_cursor_ms": cursor,
        "canonical_audio_duration_ms": cursor,
        "start_count": 1 if session is not None else 0,
        "executed_fault_counts": dict(normalized_fault_counts),
        "logical_segments": _logical_segments(observations),
        "finalization_eof_sent": final_observation.eof_sent if final_observation else False,
        "finalization_terminal_received": (
            final_observation.terminal_received if final_observation else False
        ),
    }
    if runtime_observation is not None:
        metrics.update(
            {
                "monotonic_ms": runtime_observation.monotonic_ms,
                "rss_bytes": runtime_observation.rss_bytes,
                "file_descriptors": runtime_observation.file_descriptors,
                "background_tasks": runtime_observation.background_tasks,
                "queue_depth": runtime_observation.queue_depth,
            }
        )
        writer.append_resource(
            {
                "background_tasks": runtime_observation.background_tasks,
                "file_descriptors": runtime_observation.file_descriptors,
                "monotonic_ms": runtime_observation.monotonic_ms,
                "queue_depth": runtime_observation.queue_depth,
                "rss_bytes": runtime_observation.rss_bytes,
            }
        )
    writer.write_metrics(metrics)
    summary: dict[str, object] = {
        "status": status,
        "stop_reason": stop_reason,
        "cursor_ms": cursor,
        "covered_stages": list(validated.request.covered_stages),
        "executed_fault_counts": dict(normalized_fault_counts),
    }
    if meeting_composite:
        if meeting_policy is None:
            raise StageRunnerError("meeting composite policy is missing")
        summary.update(
            {
                "stage3_window": {
                    "start_ms": meeting_policy.stage3_window.start_ms,
                    "end_ms": meeting_policy.stage3_window.end_ms,
                },
                "stage5_window": {
                    "start_ms": meeting_policy.stage5_window.start_ms,
                    "end_ms": meeting_policy.stage5_window.end_ms,
                },
            }
        )
    writer.write_summary(summary)
    writer.ensure_empty_streams()
    # The final seal is intentionally outside the lifecycle exception handler.
    # A StageArtifactError therefore propagates once and cannot trigger a second
    # seal or a fabricated artifact index.
    index = writer.seal()
    checkpoint_identity = next(
        (
            identity
            for identity in index.artifacts
            if identity.path == "checkpoints/stage3.json"
        ),
        None,
    )
    return _result(
        validated,
        writer,
        status=status,
        stop_reason=stop_reason,
        executed_fault_counts=normalized_fault_counts,
        stage3_checkpoint_sha256=(
            checkpoint_identity.sha256 if checkpoint_identity is not None else None
        ),
    )


async def run_stage(
    request: StageRunRequest,
    executor_factory: Callable[[], StageExecutor],
    policy: StagePolicy,
) -> StageRunResult:
    """串行执行一份 Stage run，并在 lock 生命周期内完成封存。"""

    validate_request_for_lock(request)
    with exclusive_resource_lock(
        request.lock_path,
        timeout_secs=request.lock_timeout_secs,
        run_id=request.run_id,
    ):
        require_no_resource_quarantine(request.quarantine_path)
        composite_lineage = request.stage == 5 and request.covered_stages == (3, 5)
        if composite_lineage and not isinstance(policy, MeetingStagePolicy):
            raise StageRequestError(
                "Stage 3/5 composite lineage requires MeetingStagePolicy"
            )
        if not composite_lineage and isinstance(policy, MeetingStagePolicy):
            raise StageRequestError(
                "MeetingStagePolicy is only valid for Stage 3/5 composite lineage"
            )
        validated = validate_stage_request(request)
        if composite_lineage:
            try:
                validate_composite_schedule(
                    validated.schedule,
                    covered_stages=request.covered_stages,
                )
            except ValueError as exc:
                raise StageRequestError("invalid Stage 3/5 composite schedule") from exc
        eligibility = validate_stage_eligibility(validated)
        if not eligibility.eligible:
            return _record_deferred(validated, eligibility)
        executor = executor_factory()
        _validate_executor(
            request,
            validated.resolved_inputs,
            validated.schedule,
            executor,
            validated.fault_plan,
        )
        writer = StageArtifactWriter.create(request.output_root, request.run_id)
        return await _run_locked(validated, executor, policy, writer)


__all__ = [
    "ALLOWED_STATUS_TRANSITIONS",
    "FaultScheduler",
    "ResourceQuarantinedError",
    "ScreenDecision",
    "StageEligibilityError",
    "StageEligibilityResult",
    "StagePolicy",
    "StageRequestError",
    "StageRunRequest",
    "StageRunResult",
    "StageRunnerError",
    "StageStateError",
    "ValidatedStageRunRequest",
    "load_stage_run_request",
    "run_stage",
    "validate_request_for_lock",
    "validate_stage_eligibility",
    "validate_stage_request",
    "validate_status_transition",
]
