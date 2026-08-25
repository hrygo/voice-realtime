"""Stage executor boundary and registry tests."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from asr_stage_fakes import SyntheticStageExecutor

from voice_realtime.benchmarks.asr.stage_contracts import (
    FaultEvent,
    ScheduleSegment,
    StageModelFile,
    StageModelManifest,
)
from voice_realtime.benchmarks.asr.stage_executors import (
    CloseObservation,
    CursorRange,
    FaultObservation,
    FinalObservation,
    RuntimeObservation,
    SegmentObservation,
    StageExecutionContext,
    StageExecutorCapabilities,
    StageExecutorCapabilityError,
    StageExecutorRegistry,
    UnknownStageExecutorError,
    ValidatedRuntimeInputs,
    validate_executor_capabilities,
)
from voice_realtime.benchmarks.asr.stage_inputs import ResolvedPCMInput


def _runtime_inputs(tmp_path: Path) -> ValidatedRuntimeInputs:
    return ValidatedRuntimeInputs(
        model_root=tmp_path / "private-model-root",
        model_manifest=StageModelManifest(
            model_id="test-model",
            model_revision="revision-1",
            files=(
                StageModelFile(
                    relative_path="weights.bin",
                    sha256="a" * 64,
                    size_bytes=1,
                ),
            ),
        ),
        profile={"secret_profile": "do-not-print", "nested": {"value": 1}},
        runtime_config={"secret_runtime": "do-not-print"},
    )


def _context(tmp_path: Path) -> StageExecutionContext:
    return StageExecutionContext(
        run_id="run-001",
        stage=2,
        covered_stages=(2,),
        family_id="meeting",
        candidate_id="candidate-1",
        evidence_tier="experimental",
        identity_sha256s={"model": "b" * 64},
        runtime_inputs=_runtime_inputs(tmp_path),
    )


def test_registry_normalizes_ids_and_rejects_empty_duplicate_and_unknown() -> None:
    registry = StageExecutorRegistry()
    executor = SyntheticStageExecutor()
    registry.register("  test-synthetic  ", lambda: executor)

    assert registry.create("test-synthetic") is executor
    with pytest.raises(ValueError, match="already registered"):
        registry.register("test-synthetic", lambda: SyntheticStageExecutor())
    with pytest.raises(ValueError, match="executor_id"):
        registry.register("   ", lambda: SyntheticStageExecutor())
    with pytest.raises(UnknownStageExecutorError, match="UNKNOWN_STAGE_EXECUTOR"):
        registry.create("missing")


def test_registry_rejects_factory_identity_mismatch() -> None:
    registry = StageExecutorRegistry()
    registry.register("expected", lambda: SyntheticStageExecutor(executor_id="actual"))

    with pytest.raises(StageExecutorCapabilityError, match="identity mismatch"):
        registry.create("expected")


def test_registry_rejects_factory_missing_protocol_methods() -> None:
    class IncompleteExecutor:
        executor_id = "incomplete"
        capabilities = SyntheticStageExecutor.capabilities

    registry = StageExecutorRegistry()
    registry.register("incomplete", IncompleteExecutor)

    with pytest.raises(StageExecutorCapabilityError, match="protocol"):
        registry.create("incomplete")


def test_capabilities_require_non_empty_valid_sets() -> None:
    with pytest.raises(ValueError, match="supported_stages"):
        StageExecutorCapabilities(
            supported_stages=frozenset(),
            supported_inputs=frozenset({"pcm"}),
            supports_continuation=False,
            supported_faults=frozenset(),
            is_synthetic=True,
        )
    with pytest.raises(ValueError, match="supported_inputs"):
        StageExecutorCapabilities(
            supported_stages=frozenset({2}),
            supported_inputs=frozenset(),
            supports_continuation=False,
            supported_faults=frozenset(),
            is_synthetic=True,
        )
    with pytest.raises(ValueError, match="unsupported stage"):
        StageExecutorCapabilities(
            supported_stages=frozenset({1}),
            supported_inputs=frozenset({"pcm"}),
            supports_continuation=False,
            supported_faults=frozenset(),
            is_synthetic=True,
        )


def test_cursor_range_and_observations_reject_invalid_values() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        CursorRange(start_ms=-1, end_ms=10)
    with pytest.raises(ValueError, match="end_ms"):
        CursorRange(start_ms=10, end_ms=9)
    assert CursorRange(start_ms=10, end_ms=10).duration_ms == 0

    with pytest.raises(ValueError, match="repetition_index"):
        SegmentObservation(
            segment_id="segment",
            repetition_index=-1,
            slice_index=0,
            cursor=CursorRange(0, 1),
            session_id="session",
            source_epoch=0,
            metrics={},
        )
    with pytest.raises(ValueError, match="non-negative"):
        RuntimeObservation(
            monotonic_ms=0,
            rss_bytes=-1,
            file_descriptors=0,
            background_tasks=0,
            queue_depth=0,
        )
    with pytest.raises(ValueError, match="terminal_received"):
        FinalObservation(
            eof_sent=False,
            terminal_received=True,
            finalization_latency_ms=0,
            metrics={},
        )
    with pytest.raises(ValueError, match="released"):
        CloseObservation(
            released=True,
            remaining_process_ids=(123,),
        )


def test_metrics_allow_signed_finite_scientific_values() -> None:
    observation = SegmentObservation(
        segment_id="segment",
        repetition_index=0,
        slice_index=0,
        cursor=CursorRange(0, 1),
        session_id="session",
        source_epoch=0,
        metrics={"dbfs": -42.0, "bias": -1, "finite": 1.5},
    )
    assert observation.metrics["dbfs"] == -42.0
    assert observation.metrics["bias"] == -1

    with pytest.raises(ValueError, match="finite"):
        SegmentObservation(
            segment_id="segment",
            repetition_index=0,
            slice_index=0,
            cursor=CursorRange(0, 1),
            session_id="session",
            source_epoch=0,
            metrics={"nan": math.nan},
        )
    with pytest.raises(ValueError, match="finite"):
        FinalObservation(
            eof_sent=True,
            terminal_received=True,
            finalization_latency_ms=0,
            metrics={"infinity": math.inf},
        )


def test_final_observation_validates_optional_fault_observation() -> None:
    fault = FaultObservation(
        event_id="delay",
        kind="finalization_delay",
        planned_cursor_ms=100,
        actual_cursor_ms=100,
        outcome="recovered",
        session_id_before="session",
        session_id_after="session",
        source_epoch_before=1,
        source_epoch_after=1,
    )
    observation = FinalObservation(
        eof_sent=True,
        terminal_received=True,
        finalization_latency_ms=5,
        metrics={},
        fault_observation=fault,
    )
    assert observation.fault_observation == fault
    with pytest.raises(TypeError, match="fault_observation"):
        FinalObservation(
            eof_sent=True,
            terminal_received=True,
            finalization_latency_ms=0,
            metrics={},
            fault_observation=object(),  # type: ignore[arg-type]
        )
def test_context_validates_hashes_and_stage_lineage(tmp_path: Path) -> None:
    context = StageExecutionContext(
        run_id="run",
        stage=2,
        covered_stages=(2,),
        family_id="family",
        candidate_id="candidate",
        evidence_tier="formal",
        identity_sha256s={"model": "A" * 64},
        runtime_inputs=_runtime_inputs(tmp_path),
    )
    assert context.identity_sha256s["model"] == "a" * 64

    for invalid_hash in ("a" * 63, "g" * 64):
        with pytest.raises(ValueError, match="SHA-256"):
            StageExecutionContext(
                run_id="run",
                stage=2,
                covered_stages=(2,),
                family_id="family",
                candidate_id="candidate",
                evidence_tier="formal",
                identity_sha256s={"model": invalid_hash},
                runtime_inputs=_runtime_inputs(tmp_path),
            )

    with pytest.raises(ValueError, match="covered_stages"):
        StageExecutionContext(
            run_id="run",
            stage=2,
            covered_stages=(2, 3),
            family_id="family",
            candidate_id="candidate",
            evidence_tier="formal",
            identity_sha256s={"model": "a" * 64},
            runtime_inputs=_runtime_inputs(tmp_path),
        )
    with pytest.raises(ValueError, match="meeting"):
        StageExecutionContext(
            run_id="run",
            stage=5,
            covered_stages=(3, 5),
            family_id="interaction",
            candidate_id="candidate",
            evidence_tier="formal",
            identity_sha256s={"model": "a" * 64},
            runtime_inputs=_runtime_inputs(tmp_path),
        )


def test_mappings_are_copied_and_path_runtime_details_are_hidden(tmp_path: Path) -> None:
    profile = {"nested": {"value": 1}}
    runtime_config = {"private": "runtime-secret"}
    inputs = ValidatedRuntimeInputs(
        model_root=tmp_path / "private-model-root",
        model_manifest=StageModelManifest(
            model_id="model",
            model_revision="revision",
            files=(StageModelFile(relative_path="model.bin", sha256="c" * 64, size_bytes=1),),
        ),
        profile=profile,
        runtime_config=runtime_config,
    )
    profile["new"] = "outside"
    runtime_config["new"] = "outside"

    assert "new" not in inputs.profile
    assert inputs.profile["nested"] == {"value": 1}
    with pytest.raises(TypeError):
        inputs.profile["new"] = "mutation"  # type: ignore[index]
    with pytest.raises(TypeError):
        inputs.profile["nested"]["value"] = 2  # type: ignore[index]

    context = StageExecutionContext(
        run_id="run",
        stage=2,
        covered_stages=(2,),
        family_id="family",
        candidate_id="candidate",
        evidence_tier="formal",
        identity_sha256s={"runtime": "d" * 64},
        runtime_inputs=inputs,
    )
    rendered = repr(context)
    assert str(inputs.model_root) not in rendered
    assert "runtime-secret" not in rendered
    assert "private" not in rendered

    identities = {"model": "e" * 64}
    copied = StageExecutionContext(
        run_id="run",
        stage=2,
        covered_stages=(2,),
        family_id="family",
        candidate_id="candidate",
        evidence_tier="formal",
        identity_sha256s=identities,
        runtime_inputs=inputs,
    )
    identities["new"] = "f" * 64
    assert "new" not in copied.identity_sha256s


def test_formal_runs_reject_synthetic_executor() -> None:
    synthetic = StageExecutorCapabilities(
        supported_stages=frozenset({2, 3, 4, 5}),
        supported_inputs=frozenset({"pcm", "interaction_script"}),
        supports_continuation=True,
        supported_faults=frozenset({"disconnect", "asr_crash", "finalization_delay"}),
        is_synthetic=True,
    )
    with pytest.raises(StageExecutorCapabilityError, match="SYNTHETIC_EXECUTOR_NOT_ALLOWED"):
        validate_executor_capabilities(
            synthetic,
            stage=2,
            input_kind="pcm",
            evidence_tier="formal",
        )

    validate_executor_capabilities(
        synthetic,
        stage=2,
        input_kind="pcm",
        evidence_tier="experimental",
    )


@pytest.mark.asyncio
async def test_synthetic_executor_lifecycle_and_idempotent_close(tmp_path: Path) -> None:
    executor = SyntheticStageExecutor()
    context = _context(tmp_path)
    segment = ScheduleSegment(
        segment_id="screen-001",
        purpose="screen",
        input_sha256="1" * 64,
        duration_ms=20,
        repetition=1,
    )
    resolved_input = ResolvedPCMInput(
        segment_id="screen-001",
        sha256="1" * 64,
        size_bytes=640,
        duration_ms=20,
        frame_bytes=640,
        _path=tmp_path / "input.pcm",
    )

    await executor.prepare(context)
    session = await executor.start(context)
    segment_observation = await executor.feed_segment(
        segment,
        resolved_input,
        CursorRange(start_ms=0, end_ms=20),
    )
    fault_observation = await executor.inject_fault(
        FaultEvent(event_id="disconnect-1", cursor_ms=20, kind="disconnect")
    )
    runtime_observation = await executor.snapshot()
    final_observation = await executor.finalize(None)
    first_close = await executor.close()
    second_close = await executor.close()

    assert session.session_id == "synthetic-session-1"
    assert segment_observation.cursor == CursorRange(0, 20)
    assert fault_observation.outcome == "recovered"
    assert runtime_observation.queue_depth == 0
    assert final_observation.eof_sent and final_observation.terminal_received
    assert first_close.released and second_close.released
    assert executor.close_count == 2
    assert executor.calls == [
        "prepare",
        "start",
        "feed_segment",
        "inject_fault",
        "snapshot",
        "finalize",
        "close",
        "close",
    ]
