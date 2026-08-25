"""Canonical-cursor fault scheduling and evidence helpers.

This module owns the fault protocol used by the stage runner.  It deliberately
depends on the executor and artifact contracts, but not on ``stage_runner``;
that keeps fault evidence reusable without introducing a lifecycle import
cycle.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field

from voice_realtime.benchmarks.asr.stage_artifacts import StageArtifactWriter
from voice_realtime.benchmarks.asr.stage_contracts import FaultEvent, FaultPlan
from voice_realtime.benchmarks.asr.stage_executors import (
    CursorRange,
    FaultObservation,
    FaultOutcome,
    SessionIdentity,
    StageExecutor,
)
from voice_realtime.benchmarks.asr.stage_validation import StageRunnerError


@dataclass
class FaultScheduler:
    """Serialize non-finalization faults against the canonical audio cursor."""

    pending: deque[FaultEvent]
    attempted_ids: set[str] = field(default_factory=set)
    finalization_fault: FaultEvent | None = None

    def __post_init__(self) -> None:
        self.pending = deque(self.pending)
        self.attempted_ids = set(self.attempted_ids)
        if any(event.kind == "finalization_delay" for event in self.pending):
            raise ValueError("finalization_delay must be passed separately")
        events = tuple(self.pending)
        if self.finalization_fault is not None:
            events += (self.finalization_fault,)
        self._validate_ordered_events(events)

    @staticmethod
    def _validate_ordered_events(events: Sequence[FaultEvent]) -> None:
        event_ids: set[str] = set()
        previous_cursor: int | None = None
        finalization_count = 0
        for event in events:
            if not isinstance(event, FaultEvent):
                raise ValueError("fault scheduler events must be FaultEvent instances")
            if event.event_id in event_ids:
                raise ValueError("fault event_id must be unique")
            event_ids.add(event.event_id)
            if previous_cursor is not None and event.cursor_ms <= previous_cursor:
                raise ValueError("fault cursors must be strictly increasing")
            previous_cursor = event.cursor_ms
            if event.kind == "finalization_delay":
                finalization_count += 1
        if finalization_count > 1:
            raise ValueError("fault plan cannot contain multiple finalization delays")

    @classmethod
    def from_events(cls, events: Sequence[FaultEvent]) -> FaultScheduler:
        ordered = tuple(events)
        cls._validate_ordered_events(ordered)
        finalization_events = tuple(
            event for event in ordered if event.kind == "finalization_delay"
        )
        non_finalization = tuple(
            event for event in ordered if event.kind != "finalization_delay"
        )
        return cls(
            deque(non_finalization),
            finalization_fault=finalization_events[0] if finalization_events else None,
        )

    @classmethod
    def from_plan(cls, plan: FaultPlan | None) -> FaultScheduler:
        return cls.from_events(()) if plan is None else cls.from_events(plan.events)

    def next_range(self, cursor: int, segment_end_ms: int) -> CursorRange:
        if cursor < 0 or segment_end_ms < cursor:
            raise ValueError("fault scheduler cursor range is invalid")
        next_fault = self.pending[0] if self.pending else None
        if next_fault is not None and next_fault.cursor_ms < cursor:
            raise StageRunnerError("fault scheduler missed a canonical cursor")
        end_ms = (
            segment_end_ms
            if next_fault is None
            else min(segment_end_ms, next_fault.cursor_ms)
        )
        return CursorRange(start_ms=cursor, end_ms=end_ms)

    def pop_due(self, cursor: int) -> FaultEvent | None:
        if not self.pending or self.pending[0].cursor_ms != cursor:
            return None
        event = self.pending.popleft()
        if event.event_id in self.attempted_ids:
            raise StageRunnerError(f"fault event attempted more than once: {event.event_id}")
        self.attempted_ids.add(event.event_id)
        return event


def fault_payload(
    event: FaultEvent,
    *,
    state: str,
    observation: FaultObservation | None = None,
    session: SessionIdentity | None = None,
    observation_available: bool = True,
    outcome: FaultOutcome | None = None,
) -> dict[str, object]:
    """Build a path-free fault evidence row for one lifecycle state."""

    payload: dict[str, object] = {
        "event_id": event.event_id,
        "kind": event.kind,
        "state": state,
        "planned_cursor_ms": event.cursor_ms,
        "duration_ms": event.duration_ms,
        "observation_available": observation_available,
    }
    if observation is not None:
        payload.update(
            {
                "actual_cursor_ms": observation.actual_cursor_ms,
                "outcome": observation.outcome,
                "session_id_before": observation.session_id_before,
                "session_id_after": observation.session_id_after,
                "source_epoch_before": observation.source_epoch_before,
                "source_epoch_after": observation.source_epoch_after,
            }
        )
    elif session is not None:
        payload.update(
            {
                "actual_cursor_ms": event.cursor_ms,
                "session_id_before": session.session_id,
                "source_epoch_before": session.source_epoch,
            }
        )
    else:
        payload["actual_cursor_ms"] = event.cursor_ms
    if outcome is not None:
        payload["outcome"] = outcome
    return payload


def validate_fault_observation(
    observation: FaultObservation,
    event: FaultEvent,
    *,
    cursor: int,
    session: SessionIdentity,
) -> SessionIdentity:
    """Strictly validate fault identity and return the candidate next session."""

    if observation.event_id != event.event_id:
        raise StageRunnerError("fault observation event mismatch")
    if observation.kind != event.kind:
        raise StageRunnerError("fault observation kind mismatch")
    if observation.planned_cursor_ms != event.cursor_ms:
        raise StageRunnerError("fault observation planned cursor mismatch")
    if observation.actual_cursor_ms != cursor:
        raise StageRunnerError("fault observation actual cursor mismatch")
    if observation.session_id_before != session.session_id:
        raise StageRunnerError("fault observation session before mismatch")
    if observation.source_epoch_before != session.source_epoch:
        raise StageRunnerError("fault observation source epoch before mismatch")
    return SessionIdentity(
        session_id=observation.session_id_after,
        process_ids=session.process_ids,
        source_epoch=observation.source_epoch_after,
    )


async def execute_fault(
    executor: StageExecutor,
    writer: StageArtifactWriter,
    event: FaultEvent,
    *,
    cursor: int,
    session: SessionIdentity,
) -> tuple[SessionIdentity, FaultOutcome]:
    """Inject one non-finalization fault and persist every lifecycle state.

    An executor exception or invalid observation gets an explicit
    ``applied/unknown`` row before the original error is re-raised.  A
    non-recovered observation never commits its post-fault identity.
    """

    writer.append_fault(fault_payload(event, state="planned", session=session))
    writer.append_fault(fault_payload(event, state="attempt_started", session=session))
    observation: object | None = None
    try:
        observation = await executor.inject_fault(event)
        if not isinstance(observation, FaultObservation):
            raise StageRunnerError("executor returned invalid fault observation")
        next_session = validate_fault_observation(
            observation,
            event,
            cursor=cursor,
            session=session,
        )
    except Exception:
        writer.append_fault(
            fault_payload(
                event,
                state="applied",
                observation=(
                    observation if isinstance(observation, FaultObservation) else None
                ),
                session=session,
                observation_available=False,
                outcome="unknown",
            )
        )
        writer.append_fault(
            fault_payload(
                event,
                state="unknown",
                observation=(
                    observation if isinstance(observation, FaultObservation) else None
                ),
                session=session,
                observation_available=False,
                outcome="unknown",
            )
        )
        raise
    writer.append_fault(fault_payload(event, state="applied", observation=observation))
    writer.append_fault(
        fault_payload(event, state=observation.outcome, observation=observation)
    )
    if observation.outcome == "recovered":
        return next_session, observation.outcome
    return session, observation.outcome


__all__ = [
    "FaultScheduler",
    "execute_fault",
    "fault_payload",
    "validate_fault_observation",
]
