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
    ScheduleManifest,
    ScheduleSegment,
    StageDecisionReport,
    StageRunManifest,
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
                cursor_ms=3_000_000,
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


def test_stage_run_manifest_binds_schedule_and_runtime_identity() -> None:
    manifest = StageRunManifest(
        run_id="stage2-meeting-qwen",
        stage=2,
        family_id="meeting",
        arm="baseline",
        candidate_id="qwen",
        git_commit="1" * 40,
        model_sha256=_hash("a"),
        profile_sha256=_hash("b"),
        runtime_config_sha256=_hash("c"),
        schedule_sha256=_hash("d"),
        fault_plan_sha256=None,
        started_at=datetime(2026, 8, 25, tzinfo=UTC),
        status="planned",
    )

    assert manifest.stage == 2
    with pytest.raises(ValidationError, match="fault plan is required"):
        StageRunManifest(
            **{
                **manifest.model_dump(),
                "stage": 5,
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
