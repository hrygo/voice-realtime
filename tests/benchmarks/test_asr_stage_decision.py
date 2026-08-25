"""Stage decision evidence-chain tests."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest
from asr_stage_fakes import _fault_rows, build_decision_fixture

from voice_realtime.benchmarks.asr import stage_evidence
from voice_realtime.benchmarks.asr.stage_decision import (
    StageEvidenceError,
    verify_stage_decision,
    write_stage_decision_report,
)


def test_stage_decision_module_exposes_frozen_path_request() -> None:
    from voice_realtime.benchmarks.asr.stage_decision import StageDecisionRequest

    request = StageDecisionRequest(
        stage=3,
        family_id="meeting",
        candidate_id="fun",
        run_dir=Path("/tmp/run"),
        gate_evidence_path=Path("/tmp/gates.json"),
        finalist_selection_path=Path("/tmp/selection.json"),
        upstream_report_paths={"stage1": Path("/tmp/stage1.json")},
        output_path=Path("/tmp/decision.json"),
        repository_root=Path("/repo"),
    )

    assert request.stage == 3
    with pytest.raises((TypeError, AttributeError)):
        request.stage = 4  # type: ignore[misc]
    with pytest.raises(TypeError):
        request.upstream_report_paths["stage2"] = Path("/tmp/stage2.json")  # type: ignore[index]
    with pytest.raises(TypeError):
        request.upstream_report_paths["stage1"] = Path("/tmp/other.json")  # type: ignore[index]
    with pytest.raises(ValueError):
        type(request)(
            stage=3,
            family_id="meeting",
            candidate_id="fun",
            run_dir=Path("/tmp/run"),
            gate_evidence_path=Path("/tmp/gates.json"),
            finalist_selection_path=Path("/tmp/selection.json"),
            upstream_report_paths={"future": Path("/tmp/future.json")},  # type: ignore[dict-item]
            output_path=Path("/tmp/decision.json"),
            repository_root=Path("/repo"),
        )


def test_promote_is_derived_from_sealed_sources(tmp_path: Path) -> None:
    fixture = build_decision_fixture(tmp_path)

    report = verify_stage_decision(fixture.request)

    assert report.status == "Promote"
    assert report.actual_duration_ms == 3_600_000
    assert report.executed_fault_counts == {
        "disconnect": 3,
        "asr_crash": 1,
        "finalization_delay": 1,
    }
    assert report.unique_finalist is True


def test_stage3_uses_only_the_sealed_slice_and_does_not_read_future_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = build_decision_fixture(tmp_path)
    request = dataclasses.replace(
        fixture.request,
        stage=3,
        upstream_report_paths={
            **fixture.request.upstream_report_paths,
            "stage3": tmp_path / "must-not-open-stage3.json",
            "stage4": tmp_path / "must-not-open-stage4.json",
        },
    )
    native_open = Path.open

    def reject_future_open(self: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if self.name in {"must-not-open-stage3.json", "must-not-open-stage4.json"}:
            raise AssertionError("future upstream path was opened")
        return native_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_future_open)

    report = verify_stage_decision(request)

    assert report.status == "Finalist / Reliability Pending"
    assert report.metrics_sha256 == hashlib.sha256(
        fixture.stage3_metrics_path.read_bytes()
    ).hexdigest()


def test_tampered_metrics_and_extra_file_fail_closed(tmp_path: Path) -> None:
    fixture = build_decision_fixture(tmp_path)
    fixture.metrics_path.write_text("{}\n", encoding="utf-8")
    fixture.metrics_path.chmod(0o600)
    with pytest.raises(StageEvidenceError, match="hash mismatch"):
        verify_stage_decision(fixture.request)

    fixture = build_decision_fixture(tmp_path / "extra")
    extra = fixture.run_dir / "extra.json"
    extra.write_text("{}\n", encoding="utf-8")
    extra.chmod(0o600)
    with pytest.raises(StageEvidenceError, match="unindexed"):
        verify_stage_decision(fixture.request)


def test_index_mode_and_symlink_are_rejected(tmp_path: Path) -> None:
    fixture = build_decision_fixture(tmp_path)
    index_path = fixture.run_dir / "artifact-index.json"
    index_path.chmod(0o644)
    with pytest.raises(StageEvidenceError, match="mode 0600"):
        verify_stage_decision(fixture.request)

    fixture = build_decision_fixture(tmp_path / "symlink")
    metrics = fixture.run_dir / "metrics.json"
    target = tmp_path / "symlink-target.json"
    target.write_text(metrics.read_text(encoding="utf-8"), encoding="utf-8")
    target.chmod(0o600)
    metrics.unlink()
    metrics.symlink_to(target)
    with pytest.raises(StageEvidenceError, match="symlink"):
        verify_stage_decision(fixture.request)


def test_same_name_artifact_replacement_during_verification_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = build_decision_fixture(tmp_path)
    native_stable_file = stage_evidence.stable_file
    replaced = False

    def read_then_replace(path: Path, **kwargs: object):  # type: ignore[no-untyped-def]
        nonlocal replaced
        stable = native_stable_file(path, **kwargs)  # type: ignore[arg-type]
        if path.name == "metrics.json" and not replaced:
            replaced = True
            raw = path.read_bytes().replace(b"3600000", b"3600001", 1)
            path.write_bytes(raw)
            path.chmod(0o600)
        return stable

    monkeypatch.setattr(stage_evidence, "stable_file", read_then_replace)
    with pytest.raises(StageEvidenceError, match="identity changed"):
        verify_stage_decision(fixture.request)


def test_experimental_run_cannot_promote(tmp_path: Path) -> None:
    fixture = build_decision_fixture(tmp_path, evidence_tier="experimental")

    with pytest.raises(StageEvidenceError, match="formal evidence"):
        verify_stage_decision(fixture.request)


@pytest.mark.parametrize(
    ("run_status", "expected_status"),
    (("failed", "Reject"), ("deferred", "deferred")),
)
def test_terminal_non_promote_status_is_derived_from_sealed_state(
    tmp_path: Path,
    run_status: str,
    expected_status: str,
) -> None:
    fixture = build_decision_fixture(tmp_path, run_status=run_status)  # type: ignore[arg-type]

    report = verify_stage_decision(fixture.request)

    assert report.status == expected_status
    assert report.unique_finalist is True


def test_stage3_invalid_slice_duration_is_reject_not_finalist(tmp_path: Path) -> None:
    fixture = build_decision_fixture(tmp_path, stage3_duration_ms=1)
    request = dataclasses.replace(fixture.request, stage=3)

    report = verify_stage_decision(request)

    assert report.status == "Reject"
    assert report.actual_duration_ms == 1


def test_selection_must_have_one_derived_finalist(tmp_path: Path) -> None:
    fixture = build_decision_fixture(tmp_path)
    fixture.write_selection(eligible=("fun", "qwen"), selected="fun")

    with pytest.raises(StageEvidenceError, match="unique finalist"):
        verify_stage_decision(fixture.request)


def test_gate_unknown_source_hash_fails_closed(tmp_path: Path) -> None:
    fixture = build_decision_fixture(tmp_path)
    payload = json.loads(fixture.gate_evidence_path.read_text(encoding="utf-8"))
    payload["source_artifact_sha256s"]["long_run_stability"] = ["f" * 64]
    fixture.gate_evidence_path.write_text(
        json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
    )
    fixture.gate_evidence_path.chmod(0o600)

    with pytest.raises(StageEvidenceError, match="unknown gate source hash"):
        verify_stage_decision(fixture.request)


def test_stage3_reusable_gate_cannot_use_stage5_only_artifact(tmp_path: Path) -> None:
    fixture = build_decision_fixture(tmp_path)
    index = json.loads(
        (fixture.run_dir / "artifact-index.json").read_text(encoding="utf-8")
    )
    metrics_hash = next(
        item["sha256"] for item in index["artifacts"] if item["path"] == "metrics.json"
    )
    payload = json.loads(fixture.gate_evidence_path.read_text(encoding="utf-8"))
    payload["source_artifact_sha256s"]["long_run_stability"] = [metrics_hash]
    fixture.gate_evidence_path.write_text(
        json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
    )
    fixture.gate_evidence_path.chmod(0o600)

    request = dataclasses.replace(fixture.request, stage=3)
    with pytest.raises(StageEvidenceError, match="unknown gate source hash"):
        verify_stage_decision(request)


def test_selection_upstream_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    fixture = build_decision_fixture(tmp_path)
    payload = json.loads(fixture.selection_path.read_text(encoding="utf-8"))
    payload["upstream_report_sha256s"]["stage1"] = "f" * 64
    fixture.selection_path.write_text(
        json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
    )
    fixture.selection_path.chmod(0o600)

    with pytest.raises(StageEvidenceError, match="selection upstream hash mismatch"):
        verify_stage_decision(fixture.request)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("status", "Reject", "status order"),
        ("family_id", "other", "family/candidate"),
    ),
)
def test_upstream_identity_and_status_are_ordered(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    fixture = build_decision_fixture(tmp_path)
    stage2_path = fixture.request.upstream_report_paths["stage2"]
    payload = json.loads(stage2_path.read_text(encoding="utf-8"))
    payload[field] = value
    stage2_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    stage2_path.chmod(0o600)

    with pytest.raises(StageEvidenceError, match=message):
        verify_stage_decision(fixture.request)


def test_upstream_report_hash_and_order_are_verified(tmp_path: Path) -> None:
    fixture = build_decision_fixture(tmp_path)
    stage2_path = fixture.request.upstream_report_paths["stage2"]
    stage2_payload = json.loads(stage2_path.read_text(encoding="utf-8"))
    stage2_payload["actual_duration_ms"] = 2
    stage2_path.write_text(
        json.dumps(stage2_payload, sort_keys=True) + "\n", encoding="utf-8"
    )
    stage2_path.chmod(0o600)
    with pytest.raises(StageEvidenceError, match="upstream hash mismatch"):
        verify_stage_decision(fixture.request)

    fixture = build_decision_fixture(tmp_path / "order")
    stage3_path = fixture.request.upstream_report_paths["stage3"]
    stage3_payload = json.loads(stage3_path.read_text(encoding="utf-8"))
    stage3_payload["upstream_report_sha256s"] = {
        key: stage3_payload["upstream_report_sha256s"][key]
        for key in ("stage2", "stage1")
    }
    stage3_path.write_text(
        json.dumps(stage3_payload, sort_keys=False) + "\n", encoding="utf-8"
    )
    stage3_path.chmod(0o600)
    with pytest.raises(StageEvidenceError, match="upstream order mismatch"):
        verify_stage_decision(fixture.request)


def test_fault_unknown_terminal_is_reject(tmp_path: Path) -> None:
    rows = [dict(row) for row in _fault_rows()]
    rows[3]["state"] = "unknown"
    rows[3]["outcome"] = "unknown"
    fixture = build_decision_fixture(tmp_path, fault_rows=rows)

    report = verify_stage_decision(fixture.request)

    assert report.status == "Reject"
    assert report.executed_fault_counts == {
        "disconnect": 2,
        "asr_crash": 1,
        "finalization_delay": 1,
    }


def test_unavailable_fault_observation_on_failed_run_is_non_promote(
    tmp_path: Path,
) -> None:
    rows = [dict(row) for row in _fault_rows()[:4]]
    rows[2]["outcome"] = "unknown"
    rows[3]["state"] = "unknown"
    rows[3]["outcome"] = "unknown"
    for row in rows[2:]:
        row["observation_available"] = False
        row.pop("session_id_after", None)
        row.pop("source_epoch_after", None)
    fixture = build_decision_fixture(tmp_path, run_status="failed", fault_rows=rows)

    report = verify_stage_decision(fixture.request)

    assert report.status == "Reject"
    assert report.executed_fault_counts == {
        "disconnect": 0,
        "asr_crash": 0,
        "finalization_delay": 0,
    }


def test_metrics_type_is_strictly_validated(tmp_path: Path) -> None:
    fixture = build_decision_fixture(tmp_path, stage5_duration_ms="3600000")
    with pytest.raises(StageEvidenceError, match="duration"):
        verify_stage_decision(fixture.request)


@pytest.mark.parametrize(
    "variant",
    (
        "duplicate",
        "missing",
        "partial",
        "malformed",
        "wrong_cursor",
        "wrong_duration",
        "broken_session",
    ),
)
def test_fault_sequence_anomalies_never_promote(tmp_path: Path, variant: str) -> None:
    rows = [dict(row) for row in _fault_rows()]
    if variant == "duplicate":
        for row in rows[12:16]:
            row["event_id"] = "d2"
    elif variant == "missing":
        rows = rows[:12] + rows[16:]
    elif variant == "partial":
        rows.pop()
    elif variant == "malformed":
        rows[0].pop("event_id")
    elif variant == "wrong_cursor":
        rows[4]["actual_cursor_ms"] = 600_001
    elif variant == "wrong_duration":
        rows[8]["duration_ms"] = 1
    elif variant == "broken_session":
        rows[4]["session_id_before"] = "broken"
        rows[5]["session_id_before"] = "broken"
        rows[6]["session_id_before"] = "broken"
        rows[7]["session_id_before"] = "broken"
    fixture = build_decision_fixture(tmp_path, fault_rows=rows)

    if variant in {
        "duplicate",
        "partial",
        "malformed",
        "wrong_cursor",
        "wrong_duration",
        "broken_session",
    }:
        with pytest.raises(StageEvidenceError):
            verify_stage_decision(fixture.request)
    else:
        report = verify_stage_decision(fixture.request)
        assert report.status == "Reject"


def test_write_stage_decision_report_is_private_atomic_and_non_overwriting(
    tmp_path: Path,
) -> None:
    fixture = build_decision_fixture(tmp_path)
    report = verify_stage_decision(fixture.request)
    output = tmp_path / "external" / "published" / "decision.json"

    write_stage_decision_report(
        output,
        report,
        repository_root=fixture.request.repository_root,
    )

    assert output.stat().st_mode & 0o777 == 0o600
    assert output.parent.stat().st_mode & 0o777 == 0o700
    assert json.loads(output.read_text(encoding="utf-8")) == report.model_dump(mode="json")
    with pytest.raises(FileExistsError):
        write_stage_decision_report(
            output,
            report,
            repository_root=fixture.request.repository_root,
        )


def test_write_stage_decision_report_rejects_repo_and_bad_parent_mode(
    tmp_path: Path,
) -> None:
    fixture = build_decision_fixture(tmp_path)
    report = verify_stage_decision(fixture.request)
    with pytest.raises(StageEvidenceError, match="outside repository"):
        write_stage_decision_report(
            fixture.request.repository_root / "decision.json",
            report,
            repository_root=fixture.request.repository_root,
        )

    bad_parent = tmp_path / "bad-output"
    bad_parent.mkdir(mode=0o755)
    bad_parent.chmod(0o755)
    with pytest.raises(StageEvidenceError, match="0700"):
        write_stage_decision_report(
            bad_parent / "decision.json",
            report,
            repository_root=fixture.request.repository_root,
        )
