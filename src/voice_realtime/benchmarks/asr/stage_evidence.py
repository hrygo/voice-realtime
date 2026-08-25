"""Stable filesystem evidence boundaries for sealed ASR stage runs.

The decision layer only consumes the immutable snapshots exposed here.  Every
regular file is read and hashed through one ``O_NOFOLLOW`` descriptor, while
the run tree is checked before and after the read for same-name replacement.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, cast

from voice_realtime.benchmarks.asr.stage_artifacts import REQUIRED_ARTIFACTS
from voice_realtime.benchmarks.asr.stage_contracts import (
    ArtifactIndex,
    StageDecisionReport,
    StageRunManifest,
    StageRunState,
)

_FILE_MODE = 0o600
_DIRECTORY_MODE = 0o700
_MAX_JSON_BYTES = 64 * 1024 * 1024


class StageEvidenceError(RuntimeError):
    """证据链不可信、被篡改或无法稳定读取。"""


@dataclass(frozen=True, slots=True)
class StableFile:
    path: Path
    raw: bytes
    sha256: str
    identity: tuple[int, int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class VerifiedRun:
    run_dir: Path
    manifest: StageRunManifest
    state: StageRunState
    manifest_file: StableFile
    state_file: StableFile
    index_file: StableFile
    index: ArtifactIndex
    artifact_files: Mapping[str, StableFile]

    @property
    def artifact_hashes(self) -> Mapping[str, str]:
        return MappingProxyType(
            {path: artifact.sha256 for path, artifact in self.artifact_files.items()}
        )


def normalize_path(value: Path) -> Path:
    return Path(os.path.normpath(os.fspath(value))).absolute()


def file_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def lstat(path: Path, *, label: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise StageEvidenceError(f"{label} cannot be inspected") from exc


def guard_components(path: Path, *, label: str) -> None:
    """Reject every caller-supplied symlink component."""

    current = Path(path.anchor or ".")
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise StageEvidenceError(f"{label} cannot be inspected") from exc
        if stat.S_ISLNK(info.st_mode):
            raise StageEvidenceError(f"{label} must not be a symlink")
        if current != path and not stat.S_ISDIR(info.st_mode):
            raise StageEvidenceError(f"{label} has a non-directory path component")


def resolve_repository_root(path: Path) -> Path:
    guard_components(path, label="repository root")
    info = lstat(path, label="repository root")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise StageEvidenceError("repository root must be a directory")
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise StageEvidenceError("repository root cannot be resolved") from exc


def outside_repository(path: Path, repository: Path, *, label: str) -> None:
    guard_components(path, label=label)
    try:
        resolved = path.resolve(strict=False)
    except OSError as exc:
        raise StageEvidenceError(f"{label} cannot be resolved") from exc
    if resolved == repository or repository in resolved.parents:
        raise StageEvidenceError(f"{label} must be outside repository root")


def regular_file(path: Path, *, label: str) -> os.stat_result:
    info = lstat(path, label=label)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise StageEvidenceError(f"{label} must be a regular file")
    if stat.S_IMODE(info.st_mode) != _FILE_MODE:
        raise StageEvidenceError(f"{label} must use mode 0600")
    return info


def stable_file(
    path: Path,
    *,
    label: str,
    max_bytes: int = _MAX_JSON_BYTES,
) -> StableFile:
    """Read, hash and validate a regular file using one descriptor."""

    before = regular_file(path, label=label)
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    after: os.stat_result | None = None
    try:
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise StageEvidenceError(f"{label} cannot be opened safely") from exc
        fd_before = os.fstat(descriptor)
        if file_identity(before) != file_identity(fd_before):
            raise StageEvidenceError(f"{label} changed while opening")
        if stat.S_IMODE(fd_before.st_mode) != _FILE_MODE:
            raise StageEvidenceError(f"{label} must use mode 0600")
        chunks: list[bytes] = []
        digest = hashlib.sha256()
        total = 0
        while True:
            try:
                chunk = os.read(descriptor, 1024 * 1024)
            except OSError as exc:
                raise StageEvidenceError(f"{label} cannot be read") from exc
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise StageEvidenceError(f"{label} exceeds maximum size")
            chunks.append(chunk)
            digest.update(chunk)
        fd_after = os.fstat(descriptor)
        after = regular_file(path, label=label)
        if (
            file_identity(fd_before) != file_identity(fd_after)
            or file_identity(fd_before) != file_identity(after)
            or fd_after.st_size != total
        ):
            raise StageEvidenceError(f"{label} changed while reading")
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if after is None:  # pragma: no cover - descriptor path always sets it
        raise StageEvidenceError(f"{label} has no final identity")
    return StableFile(
        path=path,
        raw=b"".join(chunks),
        sha256=digest.hexdigest(),
        identity=file_identity(after),
    )


def json_object(stable: StableFile, *, label: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(stable.raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StageEvidenceError(f"{label} must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise StageEvidenceError(f"{label} JSON root must be an object")
    return cast(Mapping[str, Any], payload)


def json_lines(stable: StableFile, *, label: str) -> tuple[Mapping[str, Any], ...]:
    if not stable.raw:
        return ()
    if not stable.raw.endswith(b"\n"):
        raise StageEvidenceError(f"{label} must end with a newline")
    rows: list[Mapping[str, Any]] = []
    for line in stable.raw.split(b"\n")[:-1]:
        if not line.strip():
            raise StageEvidenceError(f"{label} contains an empty JSONL row")
        try:
            payload = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StageEvidenceError(f"{label} contains invalid JSONL") from exc
        if not isinstance(payload, dict):
            raise StageEvidenceError(f"{label} rows must be JSON objects")
        rows.append(cast(Mapping[str, Any], payload))
    return tuple(rows)


def _walk_run(run_dir: Path) -> Mapping[str, os.stat_result]:
    """Snapshot every directory and regular file in a run tree."""

    entries: dict[str, os.stat_result] = {}
    directories: list[Path] = [run_dir]
    while directories:
        directory = directories.pop()
        info = lstat(directory, label="run directory")
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise StageEvidenceError("run and nested paths must be directories")
        if stat.S_IMODE(info.st_mode) != _DIRECTORY_MODE:
            raise StageEvidenceError("run and nested directories must use mode 0700")
        relative_directory = directory.relative_to(run_dir).as_posix()
        entries["" if relative_directory == "." else relative_directory] = info
        try:
            scanned = tuple(os.scandir(directory))
        except OSError as exc:
            raise StageEvidenceError("run directory cannot be enumerated") from exc
        for entry in scanned:
            entry_path = Path(entry.path)
            try:
                entry_info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise StageEvidenceError("run entry cannot be inspected") from exc
            relative = entry_path.relative_to(run_dir).as_posix()
            if stat.S_ISLNK(entry_info.st_mode):
                raise StageEvidenceError("run artifacts must not be symlinks")
            if stat.S_ISDIR(entry_info.st_mode):
                directories.append(entry_path)
                continue
            if not stat.S_ISREG(entry_info.st_mode):
                raise StageEvidenceError("run artifacts must be regular files")
            if stat.S_IMODE(entry_info.st_mode) != _FILE_MODE:
                raise StageEvidenceError("run artifact files must use mode 0600")
            entries[relative] = entry_info
    return entries


def _relative_run_path(run_dir: Path, value: str, *, label: str) -> Path:
    normalized = str(PurePosixPath(value))
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or normalized != value
        or path == PurePosixPath(".")
    ):
        raise StageEvidenceError(f"{label} must be a normalized relative path")
    candidate = run_dir.joinpath(*path.parts)
    guard_components(candidate, label=label)
    try:
        candidate.relative_to(run_dir)
    except ValueError as exc:  # pragma: no cover - defensive after path checks
        raise StageEvidenceError(f"{label} escapes run directory") from exc
    return candidate


def _parse_manifest(stable: StableFile) -> StageRunManifest:
    try:
        return StageRunManifest.model_validate_json(stable.raw)
    except ValueError as exc:
        raise StageEvidenceError("run manifest is invalid") from exc


def _parse_state(stable: StableFile) -> StageRunState:
    try:
        return StageRunState.model_validate_json(stable.raw)
    except ValueError as exc:
        raise StageEvidenceError("run state is invalid") from exc


def verify_sealed_run(run_dir: Path) -> VerifiedRun:
    """Verify manifest/state/index and every indexed run artifact."""

    run_dir = normalize_path(run_dir)
    guard_components(run_dir, label="run directory")
    run_info = lstat(run_dir, label="run directory")
    if stat.S_ISLNK(run_info.st_mode) or not stat.S_ISDIR(run_info.st_mode):
        raise StageEvidenceError("run directory must be a directory")
    if stat.S_IMODE(run_info.st_mode) != _DIRECTORY_MODE:
        raise StageEvidenceError("run directory must use mode 0700")

    before = _walk_run(run_dir)
    manifest_file = stable_file(run_dir / "manifest.json", label="manifest")
    state_file = stable_file(run_dir / "state.json", label="state")
    index_file = stable_file(run_dir / "artifact-index.json", label="artifact index")
    manifest = _parse_manifest(manifest_file)
    state = _parse_state(state_file)
    try:
        index = ArtifactIndex.model_validate_json(index_file.raw)
    except ValueError as exc:
        raise StageEvidenceError("artifact index is invalid") from exc

    def assert_snapshot(path: str, identity: tuple[int, int, int, int, int, int]) -> None:
        expected = before.get(path)
        if expected is None or file_identity(expected) != identity:
            raise StageEvidenceError(f"run identity changed before reading: {path}")

    assert_snapshot("manifest.json", manifest_file.identity)
    assert_snapshot("state.json", state_file.identity)
    assert_snapshot("artifact-index.json", index_file.identity)
    if index.run_manifest_sha256 != manifest_file.sha256:
        raise StageEvidenceError("artifact index manifest hash mismatch")
    if manifest.run_id != state.run_id or manifest.run_id != run_dir.name:
        raise StageEvidenceError("run identity mismatch")
    if manifest.status != state.status:
        raise StageEvidenceError("manifest and state status mismatch")
    if state.status not in {"completed", "failed", "deferred"} or state.phase != "terminal":
        raise StageEvidenceError("run is not terminal and sealed")
    if (
        manifest.stage == 5
        and manifest.covered_stages == (3, 5)
        and (manifest.family_id != "meeting" or manifest.arm != "finalist")
    ):
        raise StageEvidenceError("Stage 3/5 composite identity is invalid")

    indexed_paths = {identity.path for identity in index.artifacts}
    if "manifest.json" in indexed_paths or "artifact-index.json" in indexed_paths:
        raise StageEvidenceError("manifest and artifact index must not index themselves")
    required = set(REQUIRED_ARTIFACTS)
    if (
        manifest.stage == 5
        and manifest.covered_stages == (3, 5)
        and state.status == "completed"
    ):
        required.update({"checkpoints/stage3.json", "metrics-stage3.json"})
    missing = sorted(required - indexed_paths)
    if missing:
        raise StageEvidenceError(f"required artifact is missing: {', '.join(missing)}")

    regular_paths = {
        path for path, info in before.items() if stat.S_ISREG(info.st_mode)
    }
    expected_regular = indexed_paths | {"manifest.json", "artifact-index.json"}
    extras = sorted(regular_paths - expected_regular)
    if extras:
        raise StageEvidenceError(f"run contains unindexed regular file: {extras[0]}")
    missing_indexed = sorted(indexed_paths - regular_paths)
    if missing_indexed:
        raise StageEvidenceError(f"indexed artifact is missing: {missing_indexed[0]}")

    artifact_files: dict[str, StableFile] = {}
    for artifact_identity in index.artifacts:
        artifact_path = _relative_run_path(
            run_dir, artifact_identity.path, label="artifact path"
        )
        measured = stable_file(
            artifact_path, label=f"artifact {artifact_identity.path}"
        )
        expected = before.get(artifact_identity.path)
        if expected is None or file_identity(expected) != measured.identity:
            raise StageEvidenceError(
                f"artifact identity mismatch: {artifact_identity.path}"
            )
        if measured.sha256 != artifact_identity.sha256:
            raise StageEvidenceError(
                f"artifact hash mismatch: {artifact_identity.path}"
            )
        if len(measured.raw) != artifact_identity.size_bytes:
            raise StageEvidenceError(
                f"artifact size mismatch: {artifact_identity.path}"
            )
        artifact_files[artifact_identity.path] = measured

    after = _walk_run(run_dir)
    if set(after) != set(before):
        raise StageEvidenceError("run contents changed while verifying")
    for path, before_identity in before.items():
        if file_identity(before_identity) != file_identity(after[path]):
            raise StageEvidenceError(f"run identity changed while verifying: {path}")
    return VerifiedRun(
        run_dir=run_dir,
        manifest=manifest,
        state=state,
        manifest_file=manifest_file,
        state_file=state_file,
        index_file=index_file,
        index=index,
        artifact_files=MappingProxyType(artifact_files),
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_parent(parent: Path, repository: Path) -> None:
    guard_components(parent, label="decision output directory")
    try:
        info = parent.lstat()
    except FileNotFoundError:
        grandparent = parent.parent
        guard_components(grandparent, label="decision output grandparent")
        grandparent_info = lstat(grandparent, label="decision output grandparent")
        if (
            stat.S_ISLNK(grandparent_info.st_mode)
            or not stat.S_ISDIR(grandparent_info.st_mode)
            or stat.S_IMODE(grandparent_info.st_mode) != _DIRECTORY_MODE
        ):
            raise StageEvidenceError(
                "decision output grandparent must use mode 0700"
            ) from None
        outside_repository(parent, repository, label="decision output directory")
        try:
            parent.mkdir(mode=_DIRECTORY_MODE)
        except OSError as exc:
            raise StageEvidenceError("decision output directory cannot be created") from exc
        created = lstat(parent, label="decision output directory")
        if stat.S_ISLNK(created.st_mode) or not stat.S_ISDIR(created.st_mode):
            raise StageEvidenceError(
                "decision output directory must be a directory"
            ) from None
        if stat.S_IMODE(created.st_mode) != _DIRECTORY_MODE:
            raise StageEvidenceError(
                "decision output directory must use mode 0700"
            ) from None
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise StageEvidenceError("decision output directory must be a directory")
    if stat.S_IMODE(info.st_mode) != _DIRECTORY_MODE:
        raise StageEvidenceError("decision output directory must use mode 0700")
    outside_repository(parent, repository, label="decision output directory")


def write_stage_decision_report(
    output: Path,
    report: StageDecisionReport,
    *,
    repository_root: Path,
) -> None:
    """Write exact report JSON with private mode and atomic no-overwrite publish."""

    output = normalize_path(output)
    repository = resolve_repository_root(repository_root)
    outside_repository(output, repository, label="decision output")
    guard_components(output, label="decision output")
    parent = output.parent
    _private_parent(parent, repository)
    try:
        output.lstat()
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(f"stage decision report already exists: {output}")

    payload = report.model_dump_json(indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, _FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        _fsync_directory(parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


__all__ = [
    "StableFile",
    "StageEvidenceError",
    "VerifiedRun",
    "json_lines",
    "json_object",
    "normalize_path",
    "outside_repository",
    "resolve_repository_root",
    "stable_file",
    "verify_sealed_run",
    "write_stage_decision_report",
]
