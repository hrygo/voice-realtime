"""Stage run artifact writer 的原子性、权限和封存测试。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from voice_realtime.benchmarks.asr import stage_artifacts as stage_artifacts_module
from voice_realtime.benchmarks.asr.stage_artifacts import (
    REQUIRED_ARTIFACTS,
    StageArtifactError,
    StageArtifactSealedError,
    StageArtifactWriter,
)
from voice_realtime.benchmarks.asr.stage_contracts import (
    StageRunManifest,
    StageRunState,
)


def _manifest(*, status: str) -> StageRunManifest:
    return StageRunManifest(
        run_id="run-001",
        stage=2,
        covered_stages=(2,),
        family_id="meeting",
        arm="baseline",
        candidate_id="qwen",
        evidence_tier="experimental",
        executor_id="test-synthetic",
        git_commit="1" * 40,
        model_sha256="a" * 64,
        profile_sha256="b" * 64,
        runtime_config_sha256="c" * 64,
        schedule_sha256="d" * 64,
        started_at=datetime(2026, 8, 25, tzinfo=UTC),
        status=status,  # type: ignore[arg-type]
    )


def _state(*, status: str) -> StageRunState:
    terminal = status in {"completed", "failed", "deferred"}
    return StageRunState(
        run_id="run-001",
        status=status,  # type: ignore[arg-type]
        phase="terminal" if terminal else "screen",  # type: ignore[arg-type]
        cursor_ms=1_000 if terminal else 0,
        start_count=1,
        session_id="synthetic-session-1",
        started_at=datetime(2026, 8, 25, tzinfo=UTC),
        finished_at=datetime(2026, 8, 25, 0, 1, tzinfo=UTC) if terminal else None,
        stop_reason="schedule_complete" if status == "completed" else None,
    )


def _ready_writer(tmp_path: Path) -> StageArtifactWriter:
    writer = StageArtifactWriter.create(tmp_path, "run-001")
    writer.replace_manifest(_manifest(status="completed"))
    writer.replace_state(_state(status="completed"))
    writer.append_event({"event_kind": "state", "status": "completed"})
    writer.write_metrics({"wall_elapsed_ms": 1_000})
    writer.write_summary({"stop_reason": "schedule_complete"})
    writer.ensure_empty_streams()
    return writer


def test_writer_creates_private_run_and_refuses_reuse(tmp_path: Path) -> None:
    writer = StageArtifactWriter.create(tmp_path, "run-001")

    assert writer.run_dir == tmp_path / "run-001"
    assert writer.run_dir.stat().st_mode & 0o777 == 0o700
    with pytest.raises(FileExistsError):
        StageArtifactWriter.create(tmp_path, "run-001")


@pytest.mark.parametrize("run_id", ["../run-001", "nested/run-001", "/tmp/run-001", ".."])
def test_writer_rejects_path_traversal_run_id(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(ValueError, match="run_id"):
        StageArtifactWriter.create(tmp_path, run_id)


def test_writer_rejects_symlinked_output_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    symlink_root = tmp_path / "root-link"
    symlink_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(StageArtifactError, match="symlink"):
        StageArtifactWriter.create(symlink_root, "run-001")


def test_writer_rejects_symlinked_run_directory(tmp_path: Path) -> None:
    real_run = tmp_path / "real-run"
    real_run.mkdir()
    (tmp_path / "run-001").symlink_to(real_run, target_is_directory=True)

    with pytest.raises(StageArtifactError, match="symlink"):
        StageArtifactWriter.create(tmp_path, "run-001")


def test_snapshot_replacement_is_stable_and_private(tmp_path: Path) -> None:
    writer = StageArtifactWriter.create(tmp_path, "run-001")
    writer.replace_manifest(_manifest(status="running"))
    writer.replace_state(_state(status="running"))

    manifest_path = writer.run_dir / "manifest.json"
    first = manifest_path.read_bytes()
    writer.replace_manifest(_manifest(status="completed"))
    second = manifest_path.read_bytes()

    assert first != second
    assert json.loads(second)["status"] == "completed"
    assert manifest_path.stat().st_mode & 0o777 == 0o600
    state_path = writer.run_dir / "state.json"
    assert state_path.is_file()
    assert state_path.stat().st_mode & 0o777 == 0o600
    assert not tuple(writer.run_dir.glob("*.tmp"))


def test_non_failure_artifacts_preserve_opaque_strings_exactly(tmp_path: Path) -> None:
    writer = StageArtifactWriter.create(tmp_path, "run-001")
    opaque_url = "https://vendor.example/api?token=keep"
    writer.replace_manifest(
        _manifest(status="running").model_copy(update={"executor_id": opaque_url})
    )
    writer.replace_state(_state(status="running"))
    writer.append_event({"opaque": opaque_url})
    writer.append_fault({"opaque": opaque_url})
    writer.append_resource({"opaque": opaque_url})
    writer.write_metrics({"opaque": opaque_url})
    writer.write_summary({"opaque": opaque_url})

    assert json.loads((writer.run_dir / "manifest.json").read_text())["executor_id"] == opaque_url
    assert json.loads((writer.run_dir / "events.jsonl").read_text())["opaque"] == opaque_url
    assert (
        json.loads((writer.run_dir / "fault-execution.jsonl").read_text())["opaque"]
        == opaque_url
    )
    assert opaque_url in (writer.run_dir / "resources.csv").read_text()
    assert json.loads((writer.run_dir / "metrics.json").read_text())["opaque"] == opaque_url
    assert json.loads((writer.run_dir / "summary.json").read_text())["opaque"] == opaque_url


def test_streams_are_stable_jsonl_csv_and_sanitized(tmp_path: Path) -> None:
    writer = StageArtifactWriter.create(tmp_path, "run-001")
    writer.append_event({"z": 1, "a": "value"})
    writer.append_failure(
        {
            "message": "/Users/alice/private?token=secret",
            "long": "x" * 5000,
        }
    )
    writer.append_fault({"event_id": "disconnect-1", "outcome": "recovered"})
    writer.append_resource({"sample_id": "s-001", "rss_bytes": 12})

    event_line = (writer.run_dir / "events.jsonl").read_text(encoding="utf-8")
    failure_line = (writer.run_dir / "failures.jsonl").read_text(encoding="utf-8")
    fault_line = (writer.run_dir / "fault-execution.jsonl").read_text(encoding="utf-8")
    resource_lines = (writer.run_dir / "resources.csv").read_text(encoding="utf-8").splitlines()

    assert event_line == '{"a": "value", "z": 1}\n'
    assert "alice" not in failure_line
    assert "token=secret" not in failure_line
    assert len(failure_line) < 2500
    assert json.loads(fault_line)["outcome"] == "recovered"
    assert resource_lines == ["rss_bytes,sample_id", "12,s-001"]
    for name in (
        "events.jsonl",
        "failures.jsonl",
        "fault-execution.jsonl",
        "resources.csv",
    ):
        assert (writer.run_dir / name).stat().st_mode & 0o777 == 0o600


def test_ensure_empty_streams_creates_all_required_streams(tmp_path: Path) -> None:
    writer = StageArtifactWriter.create(tmp_path, "run-001")
    writer.ensure_empty_streams()

    assert {
        "events.jsonl",
        "resources.csv",
        "fault-execution.jsonl",
        "failures.jsonl",
    } == {
        path.name for path in writer.run_dir.iterdir()
    }
    assert all(
        path.stat().st_mode & 0o777 == 0o600 for path in writer.run_dir.iterdir()
    )


def test_writer_seals_private_required_artifacts(tmp_path: Path) -> None:
    writer = _ready_writer(tmp_path)
    index = writer.seal()

    index_path = writer.run_dir / "artifact-index.json"
    assert index_path.stat().st_mode & 0o777 == 0o600
    assert {item.path for item in index.artifacts} >= set(REQUIRED_ARTIFACTS)
    assert "manifest.json" not in {item.path for item in index.artifacts}
    assert "artifact-index.json" not in {item.path for item in index.artifacts}
    manifest_path = writer.run_dir / "manifest.json"
    assert index.run_manifest_sha256 == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    state_identity = next(item for item in index.artifacts if item.path == "state.json")
    state_bytes = (writer.run_dir / state_identity.path).read_bytes()
    assert state_identity.sha256 == hashlib.sha256(state_bytes).hexdigest()
    assert state_identity.size_bytes == len(state_bytes)


def test_seal_rejects_symlink(tmp_path: Path) -> None:
    writer = _ready_writer(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    (writer.run_dir / "metrics.json").unlink()
    (writer.run_dir / "metrics.json").symlink_to(outside)

    with pytest.raises(StageArtifactError, match="regular file"):
        writer.seal()


def test_seal_rejects_nested_escape_and_bad_permissions(tmp_path: Path) -> None:
    writer = _ready_writer(tmp_path)
    nested = writer.run_dir / "checkpoints"
    nested.mkdir(mode=0o700)
    (nested / "stage3.json").write_text("{}\n", encoding="utf-8")
    (nested / "stage3.json").chmod(0o600)
    index = writer.seal()
    assert "checkpoints/stage3.json" in {item.path for item in index.artifacts}

    unsafe_dir = tmp_path / "unsafe"
    unsafe_dir.mkdir(mode=0o700)
    writer2 = StageArtifactWriter.create(unsafe_dir, "run-002")
    writer2.replace_manifest(_manifest(status="completed").model_copy(update={"run_id": "run-002"}))
    writer2.replace_state(_state(status="completed").model_copy(update={"run_id": "run-002"}))
    writer2.ensure_empty_streams()
    (writer2.run_dir / "checkpoints").mkdir(mode=0o755)
    with pytest.raises(StageArtifactError, match="0700"):
        writer2.seal()


def test_sealed_writer_rejects_every_mutation(tmp_path: Path) -> None:
    writer = _ready_writer(tmp_path)
    writer.seal()

    with pytest.raises(StageArtifactSealedError):
        writer.append_event({"event_kind": "late"})
    with pytest.raises(StageArtifactSealedError):
        writer.replace_state(_state(status="completed"))
    with pytest.raises(StageArtifactSealedError):
        writer.ensure_empty_streams()
    with pytest.raises(StageArtifactSealedError):
        writer.seal()


def test_seal_refuses_preexisting_index_without_overwrite(tmp_path: Path) -> None:
    writer = _ready_writer(tmp_path)
    index_path = writer.run_dir / "artifact-index.json"
    index_path.write_text("sentinel\n", encoding="utf-8")
    index_path.chmod(0o600)

    with pytest.raises(StageArtifactError, match="artifact-index"):
        writer.seal()
    assert index_path.read_text(encoding="utf-8") == "sentinel\n"


def test_seal_rejects_same_size_artifact_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = _ready_writer(tmp_path)
    target = writer.run_dir / "events.jsonl"
    target_inode = target.stat().st_ino
    original_sha256_file = stage_artifacts_module._sha256_file
    replaced = False

    def hash_then_replace(descriptor: int) -> str:
        nonlocal replaced
        digest = original_sha256_file(descriptor)
        if os.fstat(descriptor).st_ino == target_inode and not replaced:
            replaced = True
            target.write_bytes(target.read_bytes().replace(b'"state"', b'"other"'))
        return digest

    monkeypatch.setattr(stage_artifacts_module, "_sha256_file", hash_then_replace)
    with pytest.raises(StageArtifactError, match="changed while sealing"):
        writer.seal()


def test_seal_hashes_from_verified_fd_not_a_competing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = _ready_writer(tmp_path)
    target = writer.run_dir / "events.jsonl"
    original_bytes = target.read_bytes()
    original_digest = hashlib.sha256(original_bytes).hexdigest()
    original_inode = target.stat().st_ino
    competing_path = writer.run_dir / "events-competing.tmp"
    original_sha256_file = stage_artifacts_module._sha256_file

    def hash_from_verified_fd(descriptor: int) -> str:
        if os.fstat(descriptor).st_ino != original_inode:
            return original_sha256_file(descriptor)
        competing_path.write_bytes(b"competing path bytes")
        try:
            return original_sha256_file(descriptor)
        finally:
            competing_path.unlink()

    monkeypatch.setattr(stage_artifacts_module, "_sha256_file", hash_from_verified_fd)
    index = writer.seal()

    identity = next(item for item in index.artifacts if item.path == "events.jsonl")
    assert identity.sha256 == original_digest
    assert target.read_bytes() == original_bytes


def test_seal_rejects_same_size_manifest_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = _ready_writer(tmp_path)
    target = writer.run_dir / "manifest.json"
    target_inode = target.stat().st_ino
    original_sha256_file = stage_artifacts_module._sha256_file
    replaced = False

    def hash_then_replace(descriptor: int) -> str:
        nonlocal replaced
        digest = original_sha256_file(descriptor)
        if os.fstat(descriptor).st_ino == target_inode and not replaced:
            replaced = True
            target.write_bytes(target.read_bytes().replace(b'"qwen"', b'"test"'))
        return digest

    monkeypatch.setattr(stage_artifacts_module, "_sha256_file", hash_then_replace)
    with pytest.raises(StageArtifactError, match="changed while sealing"):
        writer.seal()
