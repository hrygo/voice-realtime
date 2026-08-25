"""验证并解析 Stage 2–5 的冻结输入。

该模块只读取调用者在 manifest 中明确绑定的文件。它不发现目录内容、不读取
reference，也不加载模型或启动运行时；返回的 resolved input 只携带经过重新核验
的类型化数据和供 executor 使用的内部路径。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from voice_realtime.benchmarks.asr.stage_contracts import (
    EvidenceTier,
    InteractionAssetBinding,
    InteractionScriptBinding,
    PCMInputBinding,
    ScheduleManifest,
    StageInputManifest,
)

_PCM_BYTES_PER_SECOND = 16_000 * 1 * 2
_PCM_BYTES_PER_MILLISECOND = _PCM_BYTES_PER_SECOND // 1_000
_DEFAULT_FRAME_BYTES = 640
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024


class StageInputError(ValueError):
    """冻结输入不能安全解析或在解析后发生变化。"""


InteractionActionKind = Literal["feed_pcm", "wait", "expect_tts", "barge_in"]


class InteractionAction(BaseModel):
    """interaction script 中唯一允许的动作联合。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: InteractionActionKind
    at_cursor_ms: int = Field(ge=0)
    asset_id: str | None = None
    duration_ms: int = Field(ge=0)


class InteractionScriptPayload(BaseModel):
    """canonical interaction script 的顶层 payload。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actions: tuple[InteractionAction, ...] = Field(min_length=1)


@dataclass(frozen=True)
class ResolvedPCMInput:
    """经过逐字节核验的 PCM 输入。

    ``_path`` 和 ``_root`` 仅供 runner/executor 在受控边界内读取，repr 中不会暴露
    绝对路径。固定的 20 ms frame 是 16 kHz mono s16le 的 640 bytes。
    """

    segment_id: str
    sha256: str
    size_bytes: int
    duration_ms: int
    frame_bytes: int
    _path: Path = field(repr=False, compare=False)
    _root: Path | None = field(default=None, repr=False, compare=False)

    def _validate_slice(
        self,
        *,
        start_offset_ms: int,
        end_offset_ms: int | None,
    ) -> tuple[int, int]:
        final_offset_ms = self.duration_ms if end_offset_ms is None else end_offset_ms
        if not 0 <= start_offset_ms <= final_offset_ms <= self.duration_ms:
            raise StageInputError("PCM slice is outside the resolved input")
        if self.frame_bytes <= 0 or self.frame_bytes % _PCM_BYTES_PER_MILLISECOND:
            raise StageInputError("PCM frame size must be a positive whole number of milliseconds")
        return start_offset_ms, final_offset_ms

    def slice_bytes(
        self,
        *,
        start_offset_ms: int = 0,
        end_offset_ms: int | None = None,
    ) -> bytes:
        """读取一个严格受边界约束的毫秒切片。"""

        start, end = self._validate_slice(
            start_offset_ms=start_offset_ms,
            end_offset_ms=end_offset_ms,
        )
        verify_resolved_input(self)
        offset_bytes = start * _PCM_BYTES_PER_MILLISECOND
        expected_bytes = (end - start) * _PCM_BYTES_PER_MILLISECOND
        try:
            with self._path.open("rb") as stream:
                stream.seek(offset_bytes)
                payload = stream.read(expected_bytes)
                if len(payload) != expected_bytes:
                    raise StageInputError("changed after resolution")
        except StageInputError:
            raise
        except OSError as exc:
            raise StageInputError("changed after resolution") from exc
        verify_resolved_input(self)
        return payload

    def iter_frames(
        self,
        *,
        start_offset_ms: int = 0,
        end_offset_ms: int | None = None,
    ) -> Iterator[bytes]:
        """以固定 frame_bytes 流式读取一个毫秒切片。"""

        start, end = self._validate_slice(
            start_offset_ms=start_offset_ms,
            end_offset_ms=end_offset_ms,
        )
        verify_resolved_input(self)
        offset_bytes = start * _PCM_BYTES_PER_MILLISECOND
        remaining = (end - start) * _PCM_BYTES_PER_MILLISECOND
        try:
            with self._path.open("rb") as stream:
                stream.seek(offset_bytes)
                while remaining > 0:
                    frame = stream.read(min(self.frame_bytes, remaining))
                    if not frame:
                        raise StageInputError("changed after resolution")
                    remaining -= len(frame)
                    yield frame
        except StageInputError:
            raise
        except OSError as exc:
            raise StageInputError("changed after resolution") from exc
        verify_resolved_input(self)


@dataclass(frozen=True)
class ResolvedInteractionInput:
    """经过 canonical JSON、action 与 asset 核验的 interaction 输入。"""

    segment_id: str
    sha256: str
    size_bytes: int
    duration_ms: int
    actions: tuple[InteractionAction, ...]
    assets: Mapping[str, ResolvedPCMInput]
    _path: Path = field(repr=False, compare=False)
    _root: Path | None = field(default=None, repr=False, compare=False)


type ResolvedStageInput = ResolvedPCMInput | ResolvedInteractionInput


def canonical_json_bytes(payload: object) -> bytes:
    """将 JSON payload 序列化为稳定 UTF-8 bytes。

    interaction script 的 canonical 表示不转义中文、按 key 排序、无无意义空白，
    并固定以一个 LF 结束；非标准 JSON 数字（NaN/Infinity）会被拒绝。
    """

    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise StageInputError("payload cannot be represented as canonical JSON") from exc
    return f"{serialized}\n".encode()


def _normalize_relative_path(relative_path: str) -> PurePosixPath:
    normalized = relative_path.strip().replace("\\", "/")
    relative = PurePosixPath(normalized)
    if (
        not normalized
        or relative.is_absolute()
        or relative == PurePosixPath(".")
        or ".." in relative.parts
    ):
        raise StageInputError("stage input path must be relative")
    return relative


def _resolved_directory(root: Path, *, label: str) -> Path:
    try:
        root_info = root.lstat()
        if stat.S_ISLNK(root_info.st_mode):
            raise StageInputError(f"{label} must not be a symlink")
        resolved = root.resolve(strict=True)
    except StageInputError:
        raise
    except OSError as exc:
        raise StageInputError(f"{label} must exist") from exc
    if not resolved.is_dir():
        raise StageInputError(f"{label} must be a directory")
    return resolved


def _assert_regular(info: os.stat_result, *, symlink_message: str) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise StageInputError(symlink_message)
    if not stat.S_ISREG(info.st_mode):
        raise StageInputError("stage input must be a regular file")


def _resolve_regular_file(root: Path, relative_path: str) -> Path:
    """解析相对文件并拒绝所有 path component symlink。"""

    relative = _normalize_relative_path(relative_path)
    resolved_root = _resolved_directory(root, label="stage input root")
    candidate = resolved_root.joinpath(*relative.parts)
    current = resolved_root
    try:
        for part in relative.parts:
            current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise StageInputError("stage input must not be a symlink")
        resolved = candidate.resolve(strict=True)
    except StageInputError:
        raise
    except FileNotFoundError as exc:
        raise StageInputError("stage input file does not exist") from exc
    except OSError as exc:
        raise StageInputError("stage input file cannot be resolved") from exc
    if not resolved.is_relative_to(resolved_root):
        raise StageInputError("stage input escapes the declared root")
    try:
        info = resolved.lstat()
    except OSError as exc:
        raise StageInputError("stage input file does not exist") from exc
    _assert_regular(info, symlink_message="stage input must not be a symlink")
    return resolved


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _measure_file(path: Path) -> tuple[int, str]:
    """在同一个已打开 fd 上读取并重新 stat/hash，降低 TOCTOU 风险。"""

    try:
        path_before = path.lstat()
    except OSError as exc:
        raise StageInputError("changed after resolution") from exc
    _assert_regular(path_before, symlink_message="changed after resolution")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            fd_before = os.fstat(stream.fileno())
            if not _same_file_identity(path_before, fd_before):
                raise StageInputError("changed after resolution")
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            fd_after = os.fstat(stream.fileno())
        path_after = path.lstat()
    except StageInputError:
        raise
    except OSError as exc:
        raise StageInputError("changed after resolution") from exc
    if not _same_file_identity(path_before, fd_after) or not _same_file_identity(
        fd_after, path_after
    ):
        raise StageInputError("changed after resolution")
    return fd_after.st_size, digest.hexdigest()


def _read_file(path: Path, *, max_bytes: int) -> tuple[bytes, int, str]:
    """安全读取小型 JSON 文件，并验证打开期间身份没有改变。"""

    try:
        path_before = path.lstat()
    except OSError as exc:
        raise StageInputError("changed after resolution") from exc
    _assert_regular(path_before, symlink_message="changed after resolution")
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    try:
        with path.open("rb") as stream:
            fd_before = os.fstat(stream.fileno())
            if not _same_file_identity(path_before, fd_before):
                raise StageInputError("changed after resolution")
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise StageInputError("interaction script exceeds the size limit")
                digest.update(chunk)
                chunks.append(chunk)
            fd_after = os.fstat(stream.fileno())
        path_after = path.lstat()
    except StageInputError:
        raise
    except OSError as exc:
        raise StageInputError("changed after resolution") from exc
    if not _same_file_identity(path_before, fd_after) or not _same_file_identity(
        fd_after, path_after
    ):
        raise StageInputError("changed after resolution")
    return b"".join(chunks), total, digest.hexdigest()


def _pcm_duration_ms(size_bytes: int) -> int:
    if size_bytes <= 0:
        raise StageInputError("PCM input must not be empty")
    if size_bytes % _PCM_BYTES_PER_MILLISECOND:
        raise StageInputError("PCM size is not aligned to one millisecond")
    return size_bytes * 1_000 // _PCM_BYTES_PER_SECOND


def _resolve_pcm(
    binding: PCMInputBinding,
    root: Path,
    *,
    segment_id: str | None = None,
) -> ResolvedPCMInput:
    path = _resolve_regular_file(root, binding.relative_path)
    size, digest = _measure_file(path)
    duration = _pcm_duration_ms(size)
    if (size, digest, duration) != (
        binding.size_bytes,
        binding.input_sha256,
        binding.duration_ms,
    ):
        raise StageInputError("PCM bytes do not match the frozen binding")
    return ResolvedPCMInput(
        segment_id=binding.segment_id if segment_id is None else segment_id,
        sha256=digest,
        size_bytes=size,
        duration_ms=duration,
        frame_bytes=_DEFAULT_FRAME_BYTES,
        _path=path,
        _root=root,
    )


def _resolve_pcm_asset(
    binding: InteractionAssetBinding,
    root: Path,
) -> ResolvedPCMInput:
    asset = binding
    pcm_binding = PCMInputBinding(
        segment_id=asset.asset_id,
        relative_path=asset.relative_path,
        input_sha256=asset.input_sha256,
        size_bytes=asset.size_bytes,
        duration_ms=asset.duration_ms,
        sample_rate_hz=asset.sample_rate_hz,
        channels=asset.channels,
        sample_format=asset.sample_format,
    )
    return _resolve_pcm(pcm_binding, root, segment_id=asset.asset_id)


def _parse_interaction_script(raw: bytes) -> tuple[InteractionAction, ...]:
    try:
        raw_payload = json.loads(raw.decode("utf-8"))
        if raw != canonical_json_bytes(raw_payload):
            raise StageInputError("interaction script must use canonical JSON")
        payload = InteractionScriptPayload.model_validate_json(raw)
    except StageInputError:
        raise
    except (TypeError, UnicodeDecodeError, ValueError, ValidationError) as exc:
        raise StageInputError("interaction script is invalid") from exc
    actions = payload.actions
    cursors = tuple(action.at_cursor_ms for action in actions)
    if cursors != tuple(sorted(cursors)):
        raise StageInputError("interaction action cursors must be monotonic")
    for action in actions:
        if action.kind in {"feed_pcm", "barge_in"} and not action.asset_id:
            raise StageInputError("interaction action references an unknown PCM asset")
    return actions


def _resolve_interaction(
    binding: InteractionScriptBinding,
    root: Path,
) -> ResolvedInteractionInput:
    path = _resolve_regular_file(root, binding.relative_path)
    raw, size, digest = _read_file(path, max_bytes=_MAX_MANIFEST_BYTES)
    actions = _parse_interaction_script(raw)
    for action in actions:
        if action.at_cursor_ms + action.duration_ms > binding.duration_ms:
            raise StageInputError("interaction action exceeds the frozen duration")
    assets = {
        asset.asset_id: _resolve_pcm_asset(asset, root) for asset in binding.assets
    }
    referenced = {
        action.asset_id
        for action in actions
        if action.kind in {"feed_pcm", "barge_in"}
    }
    if None in referenced or not referenced.issubset(assets):
        raise StageInputError("interaction action references an unknown PCM asset")
    if (size, digest) != (binding.size_bytes, binding.input_sha256):
        raise StageInputError("interaction script bytes do not match the frozen binding")
    return ResolvedInteractionInput(
        segment_id=binding.segment_id,
        sha256=digest,
        size_bytes=size,
        duration_ms=binding.duration_ms,
        actions=actions,
        assets=MappingProxyType(assets),
        _path=path,
        _root=root,
    )


def load_stage_input_manifest(path: Path) -> StageInputManifest:
    """从一个显式 JSON 文件加载 StageInputManifest。"""

    manifest_path = Path(path)
    try:
        info = manifest_path.lstat()
    except OSError as exc:
        raise StageInputError("stage input manifest does not exist") from exc
    _assert_regular(info, symlink_message="stage input manifest must not be a symlink")
    raw, _, _ = _read_file(manifest_path, max_bytes=_MAX_MANIFEST_BYTES)
    try:
        return StageInputManifest.model_validate_json(raw)
    except (TypeError, ValueError, ValidationError) as exc:
        raise StageInputError("stage input manifest is invalid") from exc


def resolve_stage_inputs(
    schedule: ScheduleManifest,
    schedule_sha256: str,
    manifest: StageInputManifest,
    input_root: Path,
    repository_root: Path,
    evidence_tier: EvidenceTier,
) -> tuple[ResolvedStageInput, ...]:
    """按 schedule 顺序解析并验证全部显式绑定输入。"""

    normalized_schedule_hash = schedule_sha256.strip().lower()
    if manifest.schedule_sha256 != normalized_schedule_hash:
        raise StageInputError("stage input manifest schedule SHA-256 mismatch")
    root = _resolved_directory(Path(input_root), label="stage input root")
    repo = _resolved_directory(Path(repository_root), label="repository root")
    if evidence_tier == "formal" and (root == repo or repo in root.parents):
        raise StageInputError("formal stage input root must be outside the repository")

    bindings = {binding.segment_id: binding for binding in manifest.bindings}
    expected_ids = tuple(segment.segment_id for segment in schedule.segments)
    if set(bindings) != set(expected_ids) or len(bindings) != len(expected_ids):
        raise StageInputError("stage input bindings must exactly match schedule segments")

    resolved: list[ResolvedStageInput] = []
    for segment in schedule.segments:
        binding = bindings[segment.segment_id]
        item: ResolvedStageInput
        if isinstance(binding, PCMInputBinding):
            item = _resolve_pcm(binding, root)
        elif isinstance(binding, InteractionScriptBinding):
            item = _resolve_interaction(binding, root)
        else:
            raise StageInputError("unsupported stage input binding")
        if item.sha256 != segment.input_sha256 or item.duration_ms != segment.duration_ms:
            raise StageInputError("resolved input does not match frozen schedule")
        # A final verification closes the parse-to-return window; the runner will
        # repeat this immediately before every feed as well.
        verify_resolved_input(item)
        resolved.append(item)
    return tuple(resolved)


def _verify_path_against_root(path: Path, root: Path) -> Path:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise StageInputError("changed after resolution") from exc
    return _resolve_regular_file(root, str(PurePosixPath(*relative.parts)))


def _verify_pcm_input(resolved: ResolvedPCMInput) -> None:
    root = resolved._root if resolved._root is not None else resolved._path.parent
    path = _verify_path_against_root(resolved._path, root)
    size, digest = _measure_file(path)
    duration = _pcm_duration_ms(size)
    if (size, digest, duration) != (
        resolved.size_bytes,
        resolved.sha256,
        resolved.duration_ms,
    ):
        raise StageInputError("changed after resolution")


def _verify_interaction_input(resolved: ResolvedInteractionInput) -> None:
    root = resolved._root if resolved._root is not None else resolved._path.parent
    path = _verify_path_against_root(resolved._path, root)
    raw, size, digest = _read_file(path, max_bytes=_MAX_MANIFEST_BYTES)
    actions = _parse_interaction_script(raw)
    if (size, digest) != (resolved.size_bytes, resolved.sha256):
        raise StageInputError("changed after resolution")
    if tuple(actions) != resolved.actions:
        raise StageInputError("changed after resolution")
    for action in actions:
        if action.at_cursor_ms + action.duration_ms > resolved.duration_ms:
            raise StageInputError("changed after resolution")
    for asset in resolved.assets.values():
        _verify_pcm_input(asset)
    referenced = {
        action.asset_id
        for action in actions
        if action.kind in {"feed_pcm", "barge_in"}
    }
    if None in referenced or not referenced.issubset(resolved.assets):
        raise StageInputError("changed after resolution")


def verify_resolved_input(resolved: ResolvedStageInput) -> None:
    """重新 stat/read/hash 一个 resolved input，检测解析后的 TOCTOU 变化。"""

    try:
        if isinstance(resolved, ResolvedPCMInput):
            _verify_pcm_input(resolved)
        elif isinstance(resolved, ResolvedInteractionInput):
            _verify_interaction_input(resolved)
        else:
            raise StageInputError("unsupported resolved stage input")
    except StageInputError as exc:
        if str(exc) == "changed after resolution":
            raise
        raise StageInputError("changed after resolution") from exc


__all__ = [
    "InteractionAction",
    "InteractionActionKind",
    "InteractionScriptPayload",
    "ResolvedInteractionInput",
    "ResolvedPCMInput",
    "ResolvedStageInput",
    "StageInputError",
    "canonical_json_bytes",
    "load_stage_input_manifest",
    "resolve_stage_inputs",
    "verify_resolved_input",
]
