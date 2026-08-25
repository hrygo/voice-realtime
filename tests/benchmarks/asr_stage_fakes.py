"""Synthetic stage executor used only by stage-runner tests.

This module is deliberately under ``tests``.  Production registries must never
import it or silently fall back to a synthetic runtime.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from voice_realtime.benchmarks.asr.stage_contracts import FaultEvent, ScheduleSegment
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


__all__ = ["SyntheticStageExecutor"]
