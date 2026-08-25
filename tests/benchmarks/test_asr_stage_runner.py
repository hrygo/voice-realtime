"""Stage runner quarantine, validation and Screen/Confirm integration tests."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from asr_stage_fakes import (
    StageFixture,
    SyntheticStageExecutor,
    TestStagePolicy,
    build_stage_fixture,
)

from voice_realtime.benchmarks.asr import stage_validation
from voice_realtime.benchmarks.asr.report import (
    CandidateLookDecision,
    FamilyLookDecision,
    Stage1DecisionReport,
)
from voice_realtime.benchmarks.asr.stage_artifacts import StageArtifactError
from voice_realtime.benchmarks.asr.stage_contracts import (
    PROMOTION_HARD_GATES,
    StageDecisionReport,
    StageEligibilityEvidence,
)
from voice_realtime.benchmarks.asr.stage_executors import (
    CloseObservation,
    CursorRange,
    SegmentObservation,
)
from voice_realtime.benchmarks.asr.stage_runner import (
    StageEligibilityError,
    StageRequestError,
    StageStateError,
    run_stage,
    validate_status_transition,
)
from voice_realtime.benchmarks.resource_lock import ResourceBusyError


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_private_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _init_test_git(repository_root: Path) -> None:
    subprocess.run(["git", "-C", str(repository_root), "init"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository_root),
            "-c",
            "user.name=stage-test",
            "-c",
            "user.email=stage-test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def stage_fixture(tmp_path: Path) -> StageFixture:
    return build_stage_fixture(tmp_path)


@pytest.mark.asyncio
async def test_lock_contention_creates_no_executor_or_output(tmp_path: Path) -> None:
    fixture = build_stage_fixture(tmp_path, lock_path=tmp_path / "host.lock")
    calls: list[str] = []

    def factory() -> SyntheticStageExecutor:
        calls.append("created")
        return SyntheticStageExecutor()

    from voice_realtime.benchmarks.resource_lock import exclusive_resource_lock

    with (
        exclusive_resource_lock(fixture.request.lock_path, run_id="owner"),
        pytest.raises(ResourceBusyError, match="RESOURCE_BUSY"),
    ):
        await run_stage(fixture.request, executor_factory=factory, policy=fixture.policy)
    assert calls == []
    assert not (fixture.request.output_root / fixture.request.run_id).exists()


@pytest.mark.asyncio
async def test_formal_deferred_run_never_constructs_executor(tmp_path: Path) -> None:
    fixture = build_stage_fixture(tmp_path)
    await asyncio.to_thread(_init_test_git, fixture.request.repository_root)
    upstream = fixture.request.output_root.parent / "stage1-report.json"
    _write_private_json(
        upstream,
        Stage1DecisionReport(
            look="core",
            alpha=0.05,
            confidence=0.95,
            stopped_at="core",
            decisions=(
                FamilyLookDecision(
                    family_id="meeting",
                    baseline_id="sensevoice",
                    status="Reject-Hard",
                    selected_candidate_id=None,
                    candidates=(
                        CandidateLookDecision(
                            candidate_id="qwen",
                            raw_p_value=1.0,
                            holm_adjusted_p_value=1.0,
                            advance_eligible=False,
                            hard_rejected=True,
                            futility_rejected=False,
                            required_gates_passed=False,
                            reason_codes=("hard_gate_failed",),
                        ),
                    ),
                ),
            ),
        ).model_dump(mode="json"),
    )
    eligibility_path = fixture.request.output_root.parent / "eligibility.json"
    _write_private_json(
        eligibility_path,
        StageEligibilityEvidence(
            target_stage=2,
            family_id="meeting",
            candidate_id="sensevoice",
            eligible=False,
            reason="stage1_not_advanced",
            upstream_report_sha256s={"stage1": _sha256(upstream)},
        ).model_dump(mode="json"),
    )
    request = dataclasses.replace(
        fixture.request,
        evidence_tier="formal",
        executor_id="meeting-real-test",
        candidate_id="sensevoice",
        eligibility_path=eligibility_path,
        upstream_report_paths={"stage1": upstream},
    )
    calls: list[str] = []

    def factory() -> SyntheticStageExecutor:
        calls.append("created")
        return SyntheticStageExecutor()

    result = await run_stage(request, executor_factory=factory, policy=fixture.policy)
    assert result.status == "deferred"
    assert calls == []
    assert (request.output_root / request.run_id / "artifact-index.json").exists()


@pytest.mark.asyncio
async def test_formal_empty_upstream_report_rejected_before_factory(tmp_path: Path) -> None:
    fixture = build_stage_fixture(tmp_path)
    upstream = fixture.request.output_root.parent / "stage1-report.json"
    _write_private_json(upstream, {})
    eligibility_path = fixture.request.output_root.parent / "eligibility.json"
    _write_private_json(
        eligibility_path,
        StageEligibilityEvidence(
            target_stage=2,
            family_id="meeting",
            candidate_id="qwen",
            eligible=False,
            reason="stage1_not_advanced",
            upstream_report_sha256s={"stage1": _sha256(upstream)},
        ).model_dump(mode="json"),
    )
    request = dataclasses.replace(
        fixture.request,
        evidence_tier="formal",
        eligibility_path=eligibility_path,
        upstream_report_paths={"stage1": upstream},
    )
    calls: list[str] = []

    def factory() -> SyntheticStageExecutor:
        calls.append("created")
        return SyntheticStageExecutor()

    with pytest.raises((StageRequestError, StageEligibilityError)):
        await run_stage(request, executor_factory=factory, policy=fixture.policy)
    assert calls == []
    assert not (request.output_root / request.run_id).exists()


def test_stage_status_rejects_skips_and_terminal_reentry() -> None:
    with pytest.raises(StageStateError, match=r"planned.*completed"):
        validate_status_transition("planned", "completed")
    validate_status_transition("planned", "running")
    validate_status_transition("running", "completed")
    with pytest.raises(StageStateError, match=r"completed.*running"):
        validate_status_transition("completed", "running")


@pytest.mark.asyncio
async def test_screen_pass_continues_same_session_without_restart(
    stage_fixture: StageFixture,
) -> None:
    executor = SyntheticStageExecutor()
    result = await run_stage(stage_fixture.request, lambda: executor, stage_fixture.policy)
    assert result.status == "completed"
    assert executor.start_count == 1
    assert executor.session_ids == ("synthetic-session-1",)
    assert executor.fed_segment_ids == ("screen-001", "confirm-001")
    assert executor.cursor_ranges == (CursorRange(0, 1_000), CursorRange(1_000, 2_000))


@pytest.mark.asyncio
async def test_screen_fail_does_not_consume_confirm(stage_fixture: StageFixture) -> None:
    executor = SyntheticStageExecutor()
    result = await run_stage(
        stage_fixture.request,
        lambda: executor,
        TestStagePolicy(pass_screen=False),
    )
    assert result.status == "completed"
    assert result.stop_reason == "screen_fail"
    assert executor.fed_segment_ids == ("screen-001",)


@pytest.mark.asyncio
async def test_close_failure_writes_quarantine_and_failed_run(tmp_path: Path) -> None:
    fixture = build_stage_fixture(tmp_path)
    executor = SyntheticStageExecutor(
        close_observation=CloseObservation(released=False, remaining_process_ids=(123,)),
    )
    result = await run_stage(fixture.request, lambda: executor, fixture.policy)
    assert result.status == "failed"
    assert fixture.request.quarantine_path.exists()
    assert (fixture.request.output_root / fixture.request.run_id / "artifact-index.json").exists()


def test_stable_reader_rejects_same_size_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "identity.json"
    path.write_bytes(b'{"value":"a"}\n')
    path.chmod(0o600)
    native_read = stage_validation.os.read
    mutated = False

    def read_and_mutate(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        payload = native_read(descriptor, size)
        if payload and not mutated:
            mutated = True
            path.write_bytes(b'{"value":"b"}\n')
            path.chmod(0o600)
        return payload

    monkeypatch.setattr(stage_validation.os, "read", read_and_mutate)
    with pytest.raises(StageRequestError, match="changed while reading"):
        stage_validation.read_stable_file(path, label="identity")


def test_streaming_reader_rejects_same_size_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "weights.bin"
    path.write_bytes(b"12345678")
    path.chmod(0o600)
    native_read = stage_validation.os.read
    mutated = False

    def read_and_mutate(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        payload = native_read(descriptor, size)
        if payload and not mutated:
            mutated = True
            path.write_bytes(b"abcdefgh")
            path.chmod(0o600)
        return payload

    monkeypatch.setattr(stage_validation.os, "read", read_and_mutate)
    with pytest.raises(StageRequestError, match="changed while reading"):
        stage_validation.measure_stable_file(path, label="model file")


def test_model_manifest_uses_streaming_measurement_without_raw_capture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = build_stage_fixture(tmp_path)
    model_file = fixture.request.model_root / "weights.bin"
    native_reader = stage_validation.read_stable_file

    def reject_model_raw_reader(path: Path, **kwargs: object) -> object:
        if Path(path) == model_file:
            raise AssertionError("model bytes must use streaming measurement")
        return native_reader(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(stage_validation, "read_stable_file", reject_model_raw_reader)
    validated = stage_validation.validate_stage_request(fixture.request)
    assert validated.model_manifest.files[0].size_bytes == model_file.stat().st_size


def test_stage1_baseline_arm_requires_baseline_identity(tmp_path: Path) -> None:
    fixture = build_stage_fixture(tmp_path)
    report_path = tmp_path / "stage1.json"
    _write_private_json(
        report_path,
        Stage1DecisionReport(
            look="core",
            alpha=0.05,
            confidence=0.95,
            stopped_at="core",
            decisions=(
                FamilyLookDecision(
                    family_id="meeting",
                    baseline_id="sensevoice",
                    status="Finalist / Reliability Pending",
                    selected_candidate_id="qwen",
                    candidates=(
                        CandidateLookDecision(
                            candidate_id="qwen",
                            raw_p_value=0.01,
                            holm_adjusted_p_value=0.01,
                            advance_eligible=True,
                            hard_rejected=False,
                            futility_rejected=False,
                            required_gates_passed=True,
                            reason_codes=(),
                        ),
                    ),
                ),
            ),
        ).model_dump(mode="json"),
    )
    request = dataclasses.replace(fixture.request, arm="baseline")
    evidence = StageEligibilityEvidence(
        target_stage=2,
        family_id="meeting",
        candidate_id="qwen",
        eligible=True,
        reason="advanced",
        upstream_report_sha256s={"stage1": "a" * 64},
    )
    with pytest.raises(StageEligibilityError, match="baseline"):
        stage_validation._validate_stage1_report(
            stage_validation.read_stable_file(report_path),
            request=request,
            evidence=evidence,
            expected_eligible=True,
        )

    baseline_request = dataclasses.replace(
        request,
        candidate_id="sensevoice",
    )
    baseline_evidence = StageEligibilityEvidence(
        target_stage=2,
        family_id="meeting",
        candidate_id="sensevoice",
        eligible=True,
        reason="advanced",
        upstream_report_sha256s={"stage1": "a" * 64},
    )
    stage_validation._validate_stage1_report(
        stage_validation.read_stable_file(report_path),
        request=baseline_request,
        evidence=baseline_evidence,
        expected_eligible=True,
    )


def test_not_unique_finalist_requires_stage5_but_allows_advancing_chain() -> None:
    evidence = StageEligibilityEvidence(
        target_stage=5,
        family_id="meeting",
        candidate_id="qwen",
        eligible=False,
        reason="not_unique_finalist",
        upstream_report_sha256s={
            "stage1": "1" * 64,
            "stage2": "2" * 64,
            "stage3": "3" * 64,
            "stage4": "4" * 64,
        },
    )
    assert all(
        stage_validation._expected_upstream_eligibility(
            evidence,
            stage_name=stage_name,
            last_upstream="stage4",
        )
        for stage_name in ("stage1", "stage2", "stage3", "stage4")
    )

    invalid_target = evidence.model_copy(update={"target_stage": 4})
    with pytest.raises(StageEligibilityError, match="Stage 5"):
        stage_validation._expected_upstream_eligibility(
            invalid_target,
            stage_name="stage3",
            last_upstream="stage3",
        )


def test_upstream_chain_allows_last_failure_but_rejects_early_failure(tmp_path: Path) -> None:
    fixture = build_stage_fixture(tmp_path, stage=4, covered_stages=(4,))
    request = dataclasses.replace(
        fixture.request,
        arm="finalist",
        evidence_tier="formal",
    )

    stage1_path = tmp_path / "stage1.json"
    _write_private_json(
        stage1_path,
        Stage1DecisionReport(
            look="core",
            alpha=0.05,
            confidence=0.95,
            stopped_at="core",
            decisions=(
                FamilyLookDecision(
                    family_id="meeting",
                    baseline_id="sensevoice",
                    status="Finalist / Reliability Pending",
                    selected_candidate_id="qwen",
                    candidates=(
                        CandidateLookDecision(
                            candidate_id="qwen",
                            raw_p_value=0.01,
                            holm_adjusted_p_value=0.01,
                            advance_eligible=True,
                            hard_rejected=False,
                            futility_rejected=False,
                            required_gates_passed=True,
                            reason_codes=(),
                        ),
                    ),
                ),
            ),
        ).model_dump(mode="json"),
    )
    gates = dict.fromkeys(PROMOTION_HARD_GATES, "not_applicable")
    stage_paths: dict[str, Path] = {"stage1": stage1_path}
    for stage, status in ((2, "Confirm-Pass"), (3, "Screen-Fail")):
        path = tmp_path / f"stage{stage}.json"
        _write_private_json(
            path,
            StageDecisionReport(
                stage=stage,
                family_id="meeting",
                candidate_id="qwen",
                status=status,
                run_manifest_sha256="b" * 64,
                hard_gates=gates,
            ).model_dump(mode="json"),
        )
        stage_paths[f"stage{stage}"] = path
    eligibility_path = tmp_path / "eligibility.json"
    _write_private_json(
        eligibility_path,
        StageEligibilityEvidence(
            target_stage=4,
            family_id="meeting",
            candidate_id="qwen",
            eligible=False,
            reason="upstream_incomplete",
            upstream_report_sha256s={
                name: _sha256(path) for name, path in stage_paths.items()
            },
        ).model_dump(mode="json"),
    )
    request = dataclasses.replace(
        request,
        eligibility_path=eligibility_path,
        upstream_report_paths=stage_paths,
    )
    evidence = stage_validation._load_eligibility(request, request.repository_root)
    assert evidence is not None and evidence.eligible is False

    # An earlier failed stage cannot be hidden by a later passing report.
    failed_stage2 = stage_paths["stage2"]
    _write_private_json(
        failed_stage2,
        StageDecisionReport(
            stage=2,
            family_id="meeting",
            candidate_id="qwen",
            status="Screen-Fail",
            run_manifest_sha256="b" * 64,
            hard_gates=gates,
        ).model_dump(mode="json"),
    )
    _write_private_json(
        eligibility_path,
        StageEligibilityEvidence(
            target_stage=4,
            family_id="meeting",
            candidate_id="qwen",
            eligible=False,
            reason="upstream_incomplete",
            upstream_report_sha256s={
                name: _sha256(path) for name, path in stage_paths.items()
            },
        ).model_dump(mode="json"),
    )
    with pytest.raises(StageEligibilityError, match="stage2 report cannot advance"):
        stage_validation._load_eligibility(request, request.repository_root)


@pytest.mark.asyncio
async def test_feed_event_failure_keeps_cursor_and_closes_once_without_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = build_stage_fixture(tmp_path)
    executor = SyntheticStageExecutor()

    from voice_realtime.benchmarks.asr.stage_artifacts import StageArtifactWriter

    original_append = StageArtifactWriter.append_event

    def fail_feed_event(self: object, payload: object) -> None:
        if isinstance(payload, dict) and payload.get("event_kind") == "feed":
            raise StageArtifactError("feed event failed")
        original_append(self, payload)  # type: ignore[arg-type]

    monkeypatch.setattr(StageArtifactWriter, "append_event", fail_feed_event)
    with pytest.raises(StageArtifactError, match="feed event failed"):
        await run_stage(fixture.request, lambda: executor, fixture.policy)
    run_dir = fixture.request.output_root / fixture.request.run_id
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["cursor_ms"] == 1_000
    assert executor.close_count == 1
    assert not (run_dir / "artifact-index.json").exists()


def test_formal_git_head_verification_is_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(StageRequestError, match="formal repository HEAD"):
        stage_validation._git_commit(tmp_path, "formal")
    assert stage_validation._git_commit(tmp_path, "experimental") == "0" * 40


def test_direct_request_validation_rejects_invalid_upstream_timeout_and_lineage(
    tmp_path: Path,
) -> None:
    fixture = build_stage_fixture(tmp_path)
    with pytest.raises(StageRequestError):
        dataclasses.replace(
            fixture.request,
            upstream_report_paths={"stage9": tmp_path / "report.json"},
        )
    with pytest.raises(StageRequestError, match="finite"):
        dataclasses.replace(fixture.request, lock_timeout_secs=float("nan"))
    with pytest.raises(StageRequestError, match=r"covered_stages \(3, 5\)"):
        dataclasses.replace(
            fixture.request,
            stage=5,
            covered_stages=(3, 5),
            family_id="interaction",
            arm="finalist",
        )


@pytest.mark.asyncio
async def test_request_mapping_and_lock_paths_are_defensive(tmp_path: Path) -> None:
    fixture = build_stage_fixture(tmp_path)
    assert fixture.request.upstream_report_paths == {}
    with pytest.raises(TypeError):
        fixture.request.upstream_report_paths["stage1"] = tmp_path / "report.json"  # type: ignore[index]
    with pytest.raises(StageRequestError, match="outside repository"):
        await run_stage(
            dataclasses.replace(
                fixture.request,
                lock_path=fixture.request.repository_root / "unsafe.lock",
            ),
            executor_factory=lambda: SyntheticStageExecutor(),
            policy=fixture.policy,
        )


@pytest.mark.asyncio
async def test_invalid_close_observation_quarantines_and_fails(tmp_path: Path) -> None:
    fixture = build_stage_fixture(tmp_path)
    executor = SyntheticStageExecutor(close_observation="invalid")  # type: ignore[arg-type]
    result = await run_stage(fixture.request, lambda: executor, fixture.policy)
    assert result.status == "failed"
    assert executor.close_count == 1
    assert fixture.request.quarantine_path.exists()


@pytest.mark.asyncio
async def test_quarantine_write_failure_is_not_reported_clean(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = build_stage_fixture(tmp_path)
    executor = SyntheticStageExecutor(
        close_observation=CloseObservation(released=False, remaining_process_ids=(123,)),
    )

    def fail_quarantine(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("quarantine write failed")

    monkeypatch.setattr(
        "voice_realtime.benchmarks.asr.stage_runner.write_resource_quarantine",
        fail_quarantine,
    )
    with pytest.raises(OSError, match="quarantine write failed"):
        await run_stage(fixture.request, lambda: executor, fixture.policy)
    assert not (
        fixture.request.output_root / fixture.request.run_id / "artifact-index.json"
    ).exists()


@pytest.mark.asyncio
async def test_executor_exception_clean_close_is_failed_once(tmp_path: Path) -> None:
    fixture = build_stage_fixture(tmp_path)
    executor = SyntheticStageExecutor(fail_on="feed_segment")
    result = await run_stage(fixture.request, lambda: executor, fixture.policy)
    assert result.status == "failed"
    assert executor.close_count == 1
    assert (fixture.request.output_root / fixture.request.run_id / "artifact-index.json").exists()


@pytest.mark.asyncio
async def test_seal_error_propagates_without_second_seal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = build_stage_fixture(tmp_path)
    executor = SyntheticStageExecutor()
    seal_calls = 0

    def fail_seal(self: object) -> object:
        nonlocal seal_calls
        seal_calls += 1
        raise StageArtifactError("seal failed")

    monkeypatch.setattr(
        "voice_realtime.benchmarks.asr.stage_runner.StageArtifactWriter.seal",
        fail_seal,
    )
    with pytest.raises(StageArtifactError, match="seal failed"):
        await run_stage(fixture.request, lambda: executor, fixture.policy)
    assert seal_calls == 1
    assert not (
        fixture.request.output_root / fixture.request.run_id / "artifact-index.json"
    ).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["repetition", "slice"])
async def test_observation_identity_mismatch_fails(
    field: str,
    tmp_path: Path,
) -> None:
    fixture = build_stage_fixture(tmp_path)
    observation = SegmentObservation(
        segment_id="screen-001",
        repetition_index=1 if field == "repetition" else 0,
        slice_index=1 if field == "slice" else 0,
        cursor=CursorRange(0, 1_000),
        session_id="synthetic-session-1",
        source_epoch=1,
        metrics={},
    )
    executor = SyntheticStageExecutor(segment_observations={"screen-001": observation})
    result = await run_stage(fixture.request, lambda: executor, fixture.policy)
    assert result.status == "failed"
    assert executor.close_count == 1


@pytest.mark.asyncio
async def test_screen_fail_is_recorded_and_resources_are_evidenced(
    tmp_path: Path,
) -> None:
    fixture = build_stage_fixture(tmp_path)
    executor = SyntheticStageExecutor()
    result = await run_stage(
        fixture.request,
        lambda: executor,
        TestStagePolicy(pass_screen=False),
    )
    run_dir = fixture.request.output_root / fixture.request.run_id
    assert result.stop_reason == "screen_fail"
    events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert '"event_kind": "screen_decision"' in events
    resources = (run_dir / "resources.csv").read_text(encoding="utf-8")
    assert "rss_bytes" in resources
