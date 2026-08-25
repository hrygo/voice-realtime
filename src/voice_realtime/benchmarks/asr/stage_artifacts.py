"""Stage run 制品的私有写入、原子快照和不可变封存。"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any, Self

from voice_realtime.benchmarks.asr.stage_contracts import (
    ArtifactIdentity,
    ArtifactIndex,
    StageRunManifest,
    StageRunState,
)

REQUIRED_ARTIFACTS: tuple[str, ...] = (
    "state.json",
    "events.jsonl",
    "metrics.json",
    "resources.csv",
    "fault-execution.jsonl",
    "failures.jsonl",
    "summary.json",
)

_STREAM_ARTIFACTS: tuple[str, ...] = (
    "events.jsonl",
    "resources.csv",
    "fault-execution.jsonl",
    "failures.jsonl",
)
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_URL_PATTERN = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s]+")
_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:/(?:[^\s/\\'\",;)]*/)*[^\s'\",;)]*)"
)
_MAX_SANITIZED_STRING_LENGTH = 2048
_FILE_MODE = 0o600
_DIRECTORY_MODE = 0o700


class StageArtifactError(RuntimeError):
    """Stage 制品目录不满足安全或封存约束。"""


class StageArtifactSealedError(StageArtifactError):
    """已封存的 writer 不再接受任何写入。"""


def _mode_bits(mode: int) -> int:
    return stat.S_IMODE(mode)


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256_file(descriptor: int) -> str:
    """从已打开并验证身份的 descriptor 读取 SHA-256。"""
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _sanitize_text(value: str) -> str:
    """移除错误信息中常见的本机路径、用户名、query 和超长字段。"""
    text = value.replace("\x00", "<nul>")
    # 先移除完整 URL，避免后续绝对路径正则把 host 的斜杠当成路径；URL
    # 本身也属于外部错误 payload，不需要写入运行制品。
    text = _URL_PATTERN.sub("<url>", text)
    # 即使不是完整 URL，错误消息中的 `path?token=...` 也不应落盘 query。
    text = text.split("?", 1)[0]
    text = _ABSOLUTE_PATH_PATTERN.sub("<path>", text)
    if len(text) > _MAX_SANITIZED_STRING_LENGTH:
        text = text[:_MAX_SANITIZED_STRING_LENGTH] + "…"
    return text


def sanitize_artifact_value(value: Any) -> Any:
    """递归清理将写入 Stage 制品的 JSON 值。"""
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, Mapping):
        return {
            _sanitize_text(str(key)): sanitize_artifact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_artifact_value(item) for item in value]
    if isinstance(value, set):
        return [sanitize_artifact_value(item) for item in sorted(value, key=repr)]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _sanitize_text(repr(value))


def _json_line(payload: Mapping[str, object]) -> str:
    return _serialize_json_line(payload)


def _failure_json_line(payload: Mapping[str, object]) -> str:
    return _serialize_json_line(sanitize_artifact_value(payload))


def _serialize_json_line(
    payload: Mapping[str, object], *, sanitize: bool = False
) -> str:
    serialized_payload: object = (
        sanitize_artifact_value(payload) if sanitize else payload
    )
    return (
        json.dumps(
            serialized_payload,
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    )


def _json_snapshot(payload: Mapping[str, object]) -> str:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


def _lstat(path: Path, *, label: str) -> os.stat_result:
    try:
        result = path.lstat()
    except FileNotFoundError as exc:
        raise StageArtifactError(f"{label} does not exist: {path.name}") from exc
    if stat.S_ISLNK(result.st_mode):
        raise StageArtifactError(f"{label} must not be a symlink")
    return result


def _require_regular_file(path: Path, *, label: str) -> os.stat_result:
    result = _lstat(path, label=label)
    if not stat.S_ISREG(result.st_mode):
        raise StageArtifactError(f"{label} must be a regular file")
    return result


def _check_existing_target(path: Path, *, label: str) -> bool:
    try:
        result = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(result.st_mode):
        raise StageArtifactError(f"{label} must not be a symlink")
    if not stat.S_ISREG(result.st_mode):
        raise StageArtifactError(f"{label} must be a regular file")
    return True


def _atomic_snapshot(path: Path, payload: str, *, overwrite: bool) -> None:
    exists = _check_existing_target(path, label=f"snapshot {path.name}")
    if exists and not overwrite:
        raise FileExistsError(f"artifact already exists: {path.name}")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    descriptor_open = True
    try:
        os.fchmod(descriptor, _FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            descriptor_open = False
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.replace(path)
        path.chmod(_FILE_MODE)
        _fsync_directory(path.parent)
    except BaseException:
        if descriptor_open:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
        raise


def _create_empty_file(path: Path) -> None:
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            _FILE_MODE,
        )
    except FileExistsError:
        _require_regular_file(path, label=f"stream {path.name}")
        return
    try:
        os.fchmod(descriptor, _FILE_MODE)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _append_bytes(path: Path, payload: bytes) -> None:
    _check_existing_target(path, label=f"stream {path.name}")
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        _FILE_MODE,
    )
    try:
        os.fchmod(descriptor, _FILE_MODE)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("stream write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    path.chmod(_FILE_MODE)
    _fsync_directory(path.parent)


def _file_identity(result: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        result.st_dev,
        result.st_ino,
        result.st_mode,
        result.st_size,
        result.st_mtime_ns,
        result.st_ctime_ns,
    )


@dataclass(frozen=True)
class _MeasuredFile:
    sha256: str
    size_bytes: int


def _measure_stable_file(path: Path, *, label: str) -> _MeasuredFile:
    """读取 regular file，并确认读取期间文件身份没有变化。"""
    before = _require_regular_file(path, label=label)
    if _mode_bits(before.st_mode) != _FILE_MODE:
        raise StageArtifactError("stage artifact files must use mode 0600")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        fd_before = os.fstat(descriptor)
        if _file_identity(fd_before) != _file_identity(before):
            raise StageArtifactError(f"{label} changed while sealing")
        digest = _sha256_file(descriptor)
        fd_after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after = _require_regular_file(path, label=label)
    identities = (
        _file_identity(before),
        _file_identity(fd_before),
        _file_identity(fd_after),
        _file_identity(after),
    )
    if any(identity != identities[0] for identity in identities[1:]):
        raise StageArtifactError(f"{label} changed while sealing")
    return _MeasuredFile(sha256=digest, size_bytes=before.st_size)


@dataclass
class StageArtifactWriter:
    """一个物理 Stage run 的唯一、不可接管制品写入器。"""

    run_dir: Path
    _sealed: bool = False
    _resource_fields: tuple[str, ...] | None = field(default=None, init=False, repr=False)

    @classmethod
    def create(cls, output_root: Path, run_id: str) -> Self:
        if not isinstance(run_id, str) or not _RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("run_id must be a single safe path component")
        try:
            root_stat = output_root.lstat()
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"output root does not exist: {output_root}") from exc
        if stat.S_ISLNK(root_stat.st_mode):
            raise StageArtifactError("output root must not be a symlink")
        if not stat.S_ISDIR(root_stat.st_mode):
            raise StageArtifactError("output root must be a directory")

        run_dir = output_root / run_id
        try:
            run_dir.lstat()
        except FileExistsError:
            raise FileExistsError(f"run directory already exists: {run_id}") from None
        except FileNotFoundError:
            pass
        else:
            if run_dir.is_symlink():
                raise StageArtifactError("run directory must not be a symlink")
            raise FileExistsError(f"run directory already exists: {run_id}")
        try:
            run_dir.mkdir(mode=_DIRECTORY_MODE, parents=False, exist_ok=False)
        except FileExistsError:
            # The explicit pre-check is for deterministic errors; mkdir remains
            # the atomic no-overwrite operation in the race case.
            raise
        run_stat = _lstat(run_dir, label="run directory")
        if not stat.S_ISDIR(run_stat.st_mode):
            raise StageArtifactError("run directory must be a directory")
        run_dir.chmod(_DIRECTORY_MODE)
        _fsync_directory(output_root)
        return cls(run_dir=run_dir)

    def _require_open(self) -> None:
        if self._sealed:
            raise StageArtifactSealedError("stage artifact writer is sealed")
        run_stat = _lstat(self.run_dir, label="run directory")
        if not stat.S_ISDIR(run_stat.st_mode):
            raise StageArtifactError("run directory must be a directory")
        if _mode_bits(run_stat.st_mode) != _DIRECTORY_MODE:
            raise StageArtifactError("run directory must use mode 0700")

    def _validate_run_id(self, run_id: str) -> None:
        if run_id != self.run_dir.name:
            raise StageArtifactError("model run_id does not match run directory")

    def replace_manifest(self, manifest: StageRunManifest) -> None:
        self._require_open()
        self._validate_run_id(manifest.run_id)
        payload = _json_snapshot(manifest.model_dump(mode="json"))
        _atomic_snapshot(self.run_dir / "manifest.json", payload, overwrite=True)

    def replace_state(self, state: StageRunState) -> None:
        self._require_open()
        self._validate_run_id(state.run_id)
        payload = _json_snapshot(state.model_dump(mode="json"))
        _atomic_snapshot(self.run_dir / "state.json", payload, overwrite=True)

    def _append_json(
        self,
        name: str,
        payload: Mapping[str, object],
        *,
        sanitize: bool = False,
    ) -> None:
        self._require_open()
        line = _failure_json_line(payload) if sanitize else _json_line(payload)
        _append_bytes(self.run_dir / name, line.encode("utf-8"))

    def append_event(self, payload: Mapping[str, object]) -> None:
        self._append_json("events.jsonl", payload)

    def append_failure(self, payload: Mapping[str, object]) -> None:
        self._append_json("failures.jsonl", payload, sanitize=True)

    def append_fault(self, payload: Mapping[str, object]) -> None:
        self._append_json("fault-execution.jsonl", payload)

    def append_resource(self, payload: Mapping[str, object]) -> None:
        self._require_open()
        if not payload:
            raise ValueError("resource row must not be empty")
        fields = tuple(sorted(str(key) for key in payload))
        if any(str(key) != key for key in payload):
            raise ValueError("resource field names must be strings")
        resource_path = self.run_dir / "resources.csv"
        _create_empty_file(resource_path)
        _check_existing_target(resource_path, label="stream resources.csv")
        if resource_path.stat().st_size == 0:
            if self._resource_fields is not None and self._resource_fields != fields:
                raise ValueError("resource fields cannot change after first row")
            self._resource_fields = fields
            header = ",".join(fields) + "\n"
        else:
            if self._resource_fields is None:
                with resource_path.open("r", encoding="utf-8", newline="") as stream:
                    reader = csv.reader(stream)
                    try:
                        existing_fields = tuple(next(reader))
                    except StopIteration as exc:
                        raise StageArtifactError("resources.csv has no header") from exc
                self._resource_fields = existing_fields
            if fields != self._resource_fields:
                raise ValueError("resource fields must remain stable")
            header = ""

        row_values = dict(payload)
        buffer = StringIO(newline="")
        writer = csv.DictWriter(
            buffer,
            fieldnames=self._resource_fields or fields,
            extrasaction="raise",
            lineterminator="\n",
        )
        if header:
            buffer.write(header)
        writer.writerow(row_values)
        _append_bytes(resource_path, buffer.getvalue().encode("utf-8"))

    def write_metrics(self, payload: Mapping[str, object]) -> None:
        self._write_new_json("metrics.json", payload)

    def write_summary(self, payload: Mapping[str, object]) -> None:
        self._write_new_json("summary.json", payload)

    def _write_new_json(self, name: str, payload: Mapping[str, object]) -> None:
        self._require_open()
        _atomic_snapshot(
            self.run_dir / name,
            _json_snapshot(payload),
            overwrite=False,
        )

    def ensure_empty_streams(self) -> None:
        self._require_open()
        for name in _STREAM_ARTIFACTS:
            _create_empty_file(self.run_dir / name)

    def _identity_for(self, path: Path) -> ArtifactIdentity:
        measured = _measure_stable_file(path, label="stage artifact")
        relative_path = path.relative_to(self.run_dir).as_posix()
        return ArtifactIdentity(
            path=relative_path,
            sha256=measured.sha256,
            size_bytes=measured.size_bytes,
        )

    def seal(self) -> ArtifactIndex:
        self._require_open()
        manifest_path = self.run_dir / "manifest.json"
        _require_regular_file(manifest_path, label="manifest")
        index_path = self.run_dir / "artifact-index.json"
        if _check_existing_target(index_path, label="artifact-index"):
            raise StageArtifactError("artifact-index already exists")

        identities: list[ArtifactIdentity] = []
        for path in sorted(self.run_dir.rglob("*"), key=lambda item: item.as_posix()):
            try:
                result = path.lstat()
            except FileNotFoundError as exc:
                raise StageArtifactError("stage artifact disappeared while sealing") from exc
            if stat.S_ISLNK(result.st_mode):
                raise StageArtifactError("stage artifact must be a regular file")
            if stat.S_ISDIR(result.st_mode):
                if _mode_bits(result.st_mode) != _DIRECTORY_MODE:
                    raise StageArtifactError("stage artifact directory must use mode 0700")
                continue
            if not stat.S_ISREG(result.st_mode):
                raise StageArtifactError("stage artifact must be a regular file")
            relative_path = path.relative_to(self.run_dir).as_posix()
            if relative_path in {"manifest.json", "artifact-index.json"}:
                if relative_path == "manifest.json" and _mode_bits(result.st_mode) != _FILE_MODE:
                    raise StageArtifactError("manifest must use mode 0600")
                continue
            identities.append(self._identity_for(path))

        indexed_paths = {identity.path for identity in identities}
        if not set(REQUIRED_ARTIFACTS).issubset(indexed_paths):
            missing = sorted(set(REQUIRED_ARTIFACTS) - indexed_paths)
            raise StageArtifactError(
                f"required stage artifacts are incomplete: {', '.join(missing)}"
            )

        manifest_measurement = _measure_stable_file(manifest_path, label="manifest")
        index = ArtifactIndex(
            run_manifest_sha256=manifest_measurement.sha256,
            artifacts=tuple(identities),
        )
        _atomic_snapshot(
            index_path,
            _json_snapshot(index.model_dump(mode="json")),
            overwrite=False,
        )
        self._sealed = True
        return index
