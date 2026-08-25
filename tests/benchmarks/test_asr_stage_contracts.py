"""Finalist-only Stage 2–5 共享制品契约测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from voice_realtime.benchmarks.asr.stage_contracts import (
    PROMOTION_HARD_GATES,
    ArtifactIdentity,
    ArtifactIndex,
    FaultEvent,
    FaultPlan,
    FinalistSelectionEvidence,
    InteractionAssetBinding,
    InteractionScriptBinding,
    PCMInputBinding,
    ScheduleManifest,
    ScheduleSegment,
    StageDecisionReport,
    StageEligibilityEvidence,
    StageGateEvidenceBundle,
    StageInputManifest,
    StageModelFile,
    StageModelManifest,
    StageRunManifest,
    StageRunState,
)


def _hash(character: str) -> str:
    return character * 64


def test_schedule_freezes_screen_confirm_order_and_duration() -> None:
    schedule = ScheduleManifest(
        stage=2,
        family_id="meeting",
        segments=(
            ScheduleSegment(
                segment_id="screen-001",
                purpose="screen",
                input_sha256=_hash("a"),
                duration_ms=120_000,
                repetition=1,
            ),
            ScheduleSegment(
                segment_id="confirm-001",
                purpose="confirm",
                input_sha256=_hash("b"),
                duration_ms=600_000,
                repetition=2,
            ),
        ),
    )

    assert schedule.total_duration_ms == 1_320_000
    with pytest.raises(ValidationError, match="screen segments must precede confirm"):
        ScheduleManifest(
            stage=2,
            family_id="meeting",
            segments=tuple(reversed(schedule.segments)),
        )


def test_stage5_fault_plan_requires_three_disconnects_crash_and_finalization_delay() -> None:
    fault_plan = FaultPlan(
        stage=5,
        duration_ms=3_600_000,
        events=(
            FaultEvent(event_id="d1", cursor_ms=300_000, kind="disconnect"),
            FaultEvent(event_id="d2", cursor_ms=900_000, kind="disconnect"),
            FaultEvent(event_id="crash", cursor_ms=1_500_000, kind="asr_crash"),
            FaultEvent(event_id="d3", cursor_ms=2_100_000, kind="disconnect"),
            FaultEvent(
                event_id="delay",
                cursor_ms=3_600_000,
                kind="finalization_delay",
                duration_ms=5_000,
            ),
        ),
    )

    assert len(fault_plan.events) == 5
    with pytest.raises(ValidationError, match="fixed fault counts"):
        FaultPlan(
            stage=5,
            duration_ms=3_600_000,
            events=fault_plan.events[:-1],
        )
    with pytest.raises(ValidationError, match="finalization_delay"):
        FaultPlan(
            stage=5,
            duration_ms=3_600_000,
            events=(*fault_plan.events[:-1], FaultEvent(
                event_id="delay",
                cursor_ms=3_599_999,
                kind="finalization_delay",
                duration_ms=5_000,
            )),
        )


def test_meeting_stage5_is_the_only_multi_stage_lineage() -> None:
    manifest = StageRunManifest(
        run_id="stage5-meeting-fun",
        stage=5,
        covered_stages=(3, 5),
        family_id="meeting",
        arm="finalist",
        candidate_id="fun",
        evidence_tier="formal",
        executor_id="meeting-test",
        git_commit="1" * 40,
        model_sha256=_hash("a"),
        profile_sha256=_hash("b"),
        runtime_config_sha256=_hash("c"),
        schedule_sha256=_hash("d"),
        fault_plan_sha256=_hash("e"),
        started_at=datetime(2026, 8, 25, tzinfo=UTC),
        status="planned",
    )

    assert manifest.covered_stages == (3, 5)
    with pytest.raises(ValidationError, match="covered_stages"):
        StageRunManifest(
            **{
                **manifest.model_dump(),
                "stage": 4,
                "covered_stages": (3, 4),
                "run_id": "stage4-meeting-fun",
                "fault_plan_sha256": None,
            }
        )

    with pytest.raises(ValidationError, match="meeting finalist"):
        StageRunManifest(
            **{
                **manifest.model_dump(),
                "family_id": "interaction",
            }
        )
    with pytest.raises(ValidationError, match="meeting finalist"):
        StageRunManifest(
            **{
                **manifest.model_dump(),
                "arm": "baseline",
            }
        )


def test_formal_non_stage5_run_rejects_fault_plan() -> None:
    with pytest.raises(ValidationError, match="formal non-Stage 5"):
        StageRunManifest(
            run_id="stage2-meeting-fun",
            stage=2,
            covered_stages=(2,),
            family_id="meeting",
            arm="finalist",
            candidate_id="fun",
            evidence_tier="formal",
            executor_id="meeting-test",
            git_commit="1" * 40,
            model_sha256=_hash("a"),
            profile_sha256=_hash("b"),
            runtime_config_sha256=_hash("c"),
            schedule_sha256=_hash("d"),
            fault_plan_sha256=_hash("e"),
            started_at=datetime(2026, 8, 25, tzinfo=UTC),
            status="planned",
        )

    experimental = StageRunManifest(
        run_id="stage2-meeting-fun-experimental",
        stage=2,
        covered_stages=(2,),
        family_id="meeting",
        arm="finalist",
        candidate_id="fun",
        evidence_tier="experimental",
        executor_id="meeting-test",
        git_commit="1" * 40,
        model_sha256=_hash("a"),
        profile_sha256=_hash("b"),
        runtime_config_sha256=_hash("c"),
        schedule_sha256=_hash("d"),
        fault_plan_sha256=_hash("e"),
        started_at=datetime(2026, 8, 25, tzinfo=UTC),
        status="planned",
    )
    assert experimental.fault_plan_sha256 == _hash("e")


def test_stage_input_manifest_requires_unique_segment_binding_ids() -> None:
    manifest = StageInputManifest(
        schedule_sha256=_hash("a"),
        bindings=(
            PCMInputBinding(
                segment_id="screen-001",
                relative_path="pcm/screen-001.pcm",
                input_sha256=_hash("b"),
                size_bytes=32_000,
                duration_ms=1_000,
            ),
        ),
    )

    assert manifest.bindings[0].kind == "pcm"
    with pytest.raises(ValidationError, match="segment_id must be unique"):
        StageInputManifest(
            schedule_sha256=_hash("a"),
            bindings=(
                manifest.bindings[0],
                PCMInputBinding(
                    segment_id="screen-001",
                    relative_path="pcm/screen-002.pcm",
                    input_sha256=_hash("c"),
                    size_bytes=32_000,
                    duration_ms=1_000,
                ),
            ),
        )


def test_model_manifest_rejects_duplicate_relative_paths() -> None:
    model_file = StageModelFile(
        relative_path="weights/model.bin",
        sha256=_hash("f"),
        size_bytes=8,
    )

    with pytest.raises(ValidationError, match="model file paths must be unique"):
        StageModelManifest(
            model_id="test/model",
            model_revision="immutable-revision",
            files=(model_file, model_file),
        )


def test_stage_input_manifest_validates_interaction_asset_paths_and_ids() -> None:
    script = InteractionScriptBinding(
        segment_id="interaction-001",
        relative_path="scripts/interaction-001.json",
        input_sha256=_hash("a"),
        size_bytes=10,
        duration_ms=2_000,
        assets=(
            InteractionAssetBinding(
                asset_id="asset-001",
                relative_path="pcm/asset-001.pcm",
                input_sha256=_hash("b"),
                size_bytes=32,
                duration_ms=1,
            ),
        ),
    )
    manifest = StageInputManifest(schedule_sha256=_hash("c"), bindings=(script,))
    assert manifest.bindings[0].kind == "interaction_script"

    with pytest.raises(ValidationError, match="input paths must be unique"):
        StageInputManifest(
            schedule_sha256=_hash("c"),
            bindings=(
                script,
                PCMInputBinding(
                    segment_id="pcm-001",
                    relative_path="scripts/interaction-001.json",
                    input_sha256=_hash("d"),
                    size_bytes=10,
                    duration_ms=2_000,
                ),
            ),
        )


def test_stage_run_state_requires_terminal_fields_and_aware_times() -> None:
    running = StageRunState(
        run_id="stage2-run",
        status="running",
        phase="screen",
        cursor_ms=100,
        start_count=1,
        started_at=datetime(2026, 8, 25, tzinfo=UTC),
    )
    assert running.finished_at is None

    with pytest.raises(ValidationError, match="terminal"):
        StageRunState(
            run_id="stage2-run",
            status="completed",
            phase="screen",
            cursor_ms=100,
            start_count=1,
            started_at=datetime(2026, 8, 25, tzinfo=UTC),
            finished_at=datetime(2026, 8, 25, 0, 0, 1, tzinfo=UTC),
        )
    with pytest.raises(ValidationError, match="timezone"):
        StageRunState(
            run_id="stage2-run",
            status="running",
            phase="screen",
            cursor_ms=100,
            start_count=1,
            started_at=datetime(2026, 8, 25),
        )


def test_gate_and_eligibility_evidence_is_closed_and_hash_bound() -> None:
    gate_bundle = StageGateEvidenceBundle(
        stage=2,
        family_id="meeting",
        candidate_id="fun",
        gates=dict.fromkeys(PROMOTION_HARD_GATES, "passed"),
        source_artifact_sha256s={
            gate: (_hash("a"),) for gate in PROMOTION_HARD_GATES
        },
    )
    assert set(gate_bundle.gates) == set(PROMOTION_HARD_GATES)

    with pytest.raises(ValidationError, match="source artifact"):
        StageGateEvidenceBundle(
            stage=2,
            family_id="meeting",
            candidate_id="fun",
            gates=dict.fromkeys(PROMOTION_HARD_GATES, "passed"),
            source_artifact_sha256s={
                **{gate: (_hash("a"),) for gate in PROMOTION_HARD_GATES},
                "artifact_traceability": (),
            },
        )

    eligible = StageEligibilityEvidence(
        target_stage=2,
        family_id="meeting",
        candidate_id="fun",
        eligible=True,
        reason="advanced",
        upstream_report_sha256s={"stage1": _hash("b")},
    )
    assert eligible.eligible is True
    with pytest.raises(ValidationError, match="advanced"):
        StageEligibilityEvidence(
            target_stage=2,
            family_id="meeting",
            candidate_id="fun",
            eligible=False,
            reason="advanced",
            upstream_report_sha256s={"stage1": _hash("b")},
        )


def test_finalist_selection_evidence_requires_exact_stage1_to_4_chain() -> None:
    upstream = {
        "stage1": _hash("a"),
        "stage2": _hash("b"),
        "stage3": _hash("c"),
        "stage4": _hash("d"),
    }
    selection = FinalistSelectionEvidence(
        family_id="meeting",
        selected_candidate_id="fun",
        eligible_candidate_ids=("fun",),
        upstream_report_sha256s=upstream,
    )
    assert selection.selected_candidate_id in selection.eligible_candidate_ids
    with pytest.raises(ValidationError, match="unique"):
        FinalistSelectionEvidence(
            family_id="meeting",
            selected_candidate_id="fun",
            eligible_candidate_ids=("fun", "fun"),
            upstream_report_sha256s=upstream,
        )


def test_stage_run_manifest_binds_schedule_and_runtime_identity() -> None:
    manifest = StageRunManifest(
        run_id="stage2-meeting-qwen",
        stage=2,
        covered_stages=(2,),
        family_id="meeting",
        arm="baseline",
        candidate_id="qwen",
        evidence_tier="formal",
        executor_id="meeting-test",
        git_commit="1" * 40,
        model_sha256=_hash("a"),
        profile_sha256=_hash("b"),
        runtime_config_sha256=_hash("c"),
        schedule_sha256=_hash("d"),
        input_manifest_sha256=_hash("f"),
        eligibility_sha256=_hash("0"),
        upstream_report_sha256s={"stage1": _hash("1")},
        fault_plan_sha256=None,
        started_at=datetime(2026, 8, 25, tzinfo=UTC),
        status="planned",
    )

    assert manifest.stage == 2
    assert manifest.input_manifest_sha256 == _hash("f")
    assert manifest.eligibility_sha256 == _hash("0")
    assert manifest.upstream_report_sha256s == {"stage1": _hash("1")}
    with pytest.raises(ValidationError, match="fault plan is required"):
        StageRunManifest(
            **{
                **manifest.model_dump(),
                "stage": 5,
                "covered_stages": (5,),
                "run_id": "stage5-meeting-qwen",
            }
        )


def test_only_stage5_with_all_hard_gates_can_promote() -> None:
    common = {
        "family_id": "meeting",
        "candidate_id": "fun",
        "run_manifest_sha256": _hash("a"),
        "upstream_report_sha256s": {
            "stage1": _hash("b"),
            "stage2": _hash("c"),
            "stage3": _hash("d"),
            "stage4": _hash("e"),
        },
        "required_hard_gates": PROMOTION_HARD_GATES,
        "hard_gates": dict.fromkeys(PROMOTION_HARD_GATES, "passed"),
        "actual_duration_ms": 3_600_000,
        "executed_fault_counts": {
            "disconnect": 3,
            "asr_crash": 1,
            "finalization_delay": 1,
        },
        "artifact_index_sha256": _hash("f"),
        "metrics_sha256": _hash("0"),
        "fault_execution_sha256": _hash("1"),
        "unique_finalist": True,
    }
    with pytest.raises(ValidationError, match="only Stage 5"):
        StageDecisionReport(stage=2, status="Promote", **common)
    with pytest.raises(ValidationError, match="all hard gates"):
        StageDecisionReport(
            stage=5,
            status="Promote",
            **{
                **common,
                "hard_gates": {
                    **common["hard_gates"],
                    "long_run_stability": "failed",
                },
            },
        )
    with pytest.raises(ValidationError):
        StageDecisionReport(
            stage=5,
            status="Promote",
            **{
                **common,
                "required_hard_gates": ("invented",),
                "hard_gates": {"invented": "passed"},
            },
        )
    with pytest.raises(ValidationError, match="60 minutes"):
        StageDecisionReport(
            stage=5,
            status="Promote",
            **{**common, "actual_duration_ms": 3_599_999},
        )
    with pytest.raises(ValidationError, match="fixed fault execution counts"):
        StageDecisionReport(
            stage=5,
            status="Promote",
            **{
                **common,
                "executed_fault_counts": {
                    "disconnect": 2,
                    "asr_crash": 1,
                    "finalization_delay": 1,
                },
            },
        )
    with pytest.raises(ValidationError, match="Stage 1-4 report chain"):
        StageDecisionReport(
            stage=5,
            status="Promote",
            **{**common, "upstream_report_sha256s": {"stage1": _hash("b")}},
        )

    report = StageDecisionReport(stage=5, status="Promote", **common)

    assert report.status == "Promote"


def test_artifact_index_rejects_private_or_duplicate_paths() -> None:
    artifact = ArtifactIdentity(
        path="reports/stage2.json",
        sha256=_hash("a"),
        size_bytes=100,
    )
    index = ArtifactIndex(
        run_manifest_sha256=_hash("b"),
        artifacts=(artifact,),
    )

    assert index.artifacts == (artifact,)
    with pytest.raises(ValidationError, match="relative"):
        ArtifactIdentity(path="/Users/private/report.json", sha256=_hash("a"), size_bytes=1)
    with pytest.raises(ValidationError, match="unique"):
        ArtifactIndex(
            run_manifest_sha256=_hash("b"),
            artifacts=(artifact, artifact),
        )
