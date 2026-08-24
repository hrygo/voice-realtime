"""ASR benchmark 的运行与语料清单契约。"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256_LENGTH = 64
_GIT_SHA_LENGTH = 40
_MAX_JSON_BYTES = 64 * 1024 * 1024


class _FrozenModel(BaseModel):
    """拒绝未知字段并禁止运行中修改身份。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _validate_hex(value: str, *, length: int, field_name: str) -> str:
    normalized = value.strip().lower()
    contains_non_hex = any(
        character not in "0123456789abcdef" for character in normalized
    )
    if len(normalized) != length or contains_non_hex:
        raise ValueError(f"{field_name} 必须是 {length} 位十六进制字符串")
    return normalized


class RuntimeIdentity(_FrozenModel):
    """ASR 运行时的不可变身份。"""

    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=200)

    @field_validator("name", "revision")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()


class EnvironmentIdentity(_FrozenModel):
    """影响可重复性的主机与软件环境。"""

    host: str = Field(min_length=1, max_length=200)
    memory_bytes: int = Field(gt=0)
    macos: str = Field(min_length=1, max_length=100)
    python: str = Field(min_length=1, max_length=100)
    torch: str = Field(min_length=1, max_length=100)

    @field_validator("host", "macos", "python", "torch")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()


RunStatus = Literal["planned", "running", "completed", "failed", "infeasible"]
MetricStatusValue = Literal[
    "supported",
    "unsupported",
    "not_applicable",
    "missing",
]


class ASRRunManifest(_FrozenModel):
    """一次 ASR 实验臂运行的完整身份。"""

    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    git_commit: str
    corpus_manifest_sha256: str
    reference_manifest_sha256: str
    backend_id: str = Field(min_length=1, max_length=200)
    model_id: str = Field(min_length=1, max_length=500)
    model_revision: str = Field(min_length=1, max_length=500)
    model_files_sha256: dict[str, str] = Field(min_length=1)
    runtime: RuntimeIdentity
    device: str = Field(min_length=1, max_length=100)
    dtype: str = Field(min_length=1, max_length=100)
    parameters: dict[str, Any]
    environment: EnvironmentIdentity
    started_at: datetime
    status: RunStatus

    @field_validator("git_commit")
    @classmethod
    def _validate_git_commit(cls, value: str) -> str:
        return _validate_hex(value, length=_GIT_SHA_LENGTH, field_name="git_commit")

    @field_validator("corpus_manifest_sha256", "reference_manifest_sha256")
    @classmethod
    def _validate_corpus_hash(cls, value: str) -> str:
        return _validate_hex(
            value,
            length=_SHA256_LENGTH,
            field_name="corpus_manifest_sha256",
        )

    @field_validator("backend_id", "model_id", "model_revision", "device", "dtype")
    @classmethod
    def _strip_identity(cls, value: str) -> str:
        return value.strip()

    @field_validator("model_files_sha256")
    @classmethod
    def _validate_model_hashes(cls, value: dict[str, str]) -> dict[str, str]:
        validated: dict[str, str] = {}
        for raw_path, raw_hash in value.items():
            path = raw_path.strip()
            if not path:
                raise ValueError("model_files_sha256 路径不能为空")
            validated[path] = _validate_hex(
                raw_hash,
                length=_SHA256_LENGTH,
                field_name=f"model_files_sha256[{path}]",
            )
        return validated

    @field_validator("started_at")
    @classmethod
    def _require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("started_at 必须包含时区")
        return value


class CorpusSample(_FrozenModel):
    """一个外部 PCM 样本的匿名元数据；不承载音频 payload。"""

    sample_id: str = Field(min_length=1, max_length=200)
    audio_path: str = Field(min_length=1, max_length=1000)
    audio_sha256: str
    duration_ms: int = Field(gt=0)
    scenario: str = Field(min_length=1, max_length=200)
    language: str = Field(min_length=1, max_length=64)
    reference_raw: str = Field(max_length=500_000)
    reference_normalized: str = Field(max_length=500_000)
    license_or_consent: str = Field(min_length=1, max_length=500)
    speakers: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    hotwords: tuple[str, ...] = ()

    @field_validator("sample_id", "scenario", "language", "license_or_consent")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("audio_sha256")
    @classmethod
    def _validate_audio_hash(cls, value: str) -> str:
        return _validate_hex(value, length=_SHA256_LENGTH, field_name="audio_sha256")

    @field_validator("audio_path")
    @classmethod
    def _validate_relative_audio_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts or not normalized:
            raise ValueError("audio_path 必须是语料根目录内的相对路径")
        return str(path)

    @field_validator("speakers", "tags", "hotwords")
    @classmethod
    def _normalize_text_tuple(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(item.strip() for item in value if item.strip())


class CorpusManifest(_FrozenModel):
    """冻结的语料版本与样本列表。"""

    schema_version: Literal["1.0"] = "1.0"
    corpus_version: str = Field(min_length=1, max_length=200)
    normalization_version: str = Field(min_length=1, max_length=200)
    samples: tuple[CorpusSample, ...] = Field(min_length=1)

    @field_validator("corpus_version", "normalization_version")
    @classmethod
    def _strip_version(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _reject_duplicate_samples(self) -> Self:
        sample_ids = [sample.sample_id for sample in self.samples]
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("sample_id 必须唯一")
        return self


CorpusSplit = Literal["public", "dev", "blind-core", "blind-reserve", "reliability"]


class CorpusInputSample(_FrozenModel):
    """不含参考文本、可安全交给盲测 runner 的音频输入记录。"""

    sample_id: str = Field(min_length=1, max_length=200)
    audio_path: str = Field(min_length=1, max_length=1000)
    source_sha256: str
    audio_sha256: str
    duration_ms: int = Field(gt=0)
    session_id: str = Field(min_length=1, max_length=200)
    source_id: str | None = Field(default=None, min_length=1, max_length=300)
    content_group_id: str | None = Field(default=None, min_length=1, max_length=300)
    source_sample_rate_hz: Literal[16000] | None = None
    start_frame: int | None = Field(default=None, ge=0)
    end_frame: int | None = Field(default=None, gt=0)
    channel_index: int | None = Field(default=None, ge=0, le=63)
    scenario: str = Field(min_length=1, max_length=200)
    language: str = Field(min_length=1, max_length=64)
    license_or_consent: str = Field(min_length=1, max_length=500)
    speakers: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    hotwords: tuple[str, ...] = ()

    @field_validator(
        "sample_id",
        "session_id",
        "scenario",
        "language",
        "license_or_consent",
    )
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("source_sha256", "audio_sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        return _validate_hex(value, length=_SHA256_LENGTH, field_name="sha256")

    @field_validator("audio_path")
    @classmethod
    def _validate_relative_audio_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts or not normalized:
            raise ValueError("audio_path 必须是语料根目录内的相对路径")
        return str(path)

    @field_validator("speakers", "tags", "hotwords")
    @classmethod
    def _normalize_text_tuple(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value if item.strip())
        if len(normalized) != len(set(normalized)):
            raise ValueError("文本标签必须唯一")
        return normalized

    @field_validator("source_id", "content_group_id")
    @classmethod
    def _strip_optional_identity(cls, value: str | None) -> str | None:
        return None if value is None else value.strip()

    @model_validator(mode="after")
    def _validate_source_segment(self) -> Self:
        if (self.start_frame is None) != (self.end_frame is None):
            raise ValueError("source segment requires both start_frame and end_frame")
        if (
            self.start_frame is not None
            and self.end_frame is not None
            and self.end_frame <= self.start_frame
        ):
            raise ValueError("source segment end must be greater than start")
        return self


class CorpusInputManifest(_FrozenModel):
    """盲测推理输入清单；类型上不允许承载 reference。"""

    schema_version: Literal["1.0"] = "1.0"
    corpus_version: str = Field(min_length=1, max_length=200)
    normalization_version: str = Field(min_length=1, max_length=200)
    split: CorpusSplit
    samples: tuple[CorpusInputSample, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _reject_duplicate_samples(self) -> Self:
        sample_ids = [sample.sample_id for sample in self.samples]
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("sample_id 必须唯一")
        return self


class CorpusReference(_FrozenModel):
    """与推理输入按 sample_id 配对的封存参考文本。"""

    sample_id: str = Field(min_length=1, max_length=200)
    reference_raw: str = Field(max_length=500_000)
    reference_normalized: str = Field(max_length=500_000)


class CorpusReferenceManifest(_FrozenModel):
    """独立封存的 Core 或 Reserve 参考文本清单。"""

    schema_version: Literal["1.0"] = "1.0"
    corpus_version: str = Field(min_length=1, max_length=200)
    normalization_version: str = Field(min_length=1, max_length=200)
    split: CorpusSplit
    input_manifest_sha256: str
    samples: tuple[CorpusReference, ...] = Field(min_length=1)

    @field_validator("input_manifest_sha256")
    @classmethod
    def _validate_input_hash(cls, value: str) -> str:
        return _validate_hex(
            value,
            length=_SHA256_LENGTH,
            field_name="input_manifest_sha256",
        )

    @model_validator(mode="after")
    def _reject_duplicate_samples(self) -> Self:
        sample_ids = [sample.sample_id for sample in self.samples]
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("sample_id 必须唯一")
        return self


BenchmarkSample = CorpusSample | CorpusInputSample


class HypothesisRecord(_FrozenModel):
    """`hypotheses.jsonl` 的稳定逐样本契约。"""

    sample_id: str = Field(min_length=1, max_length=200)
    scenario: str = Field(min_length=1, max_length=200)
    reference_raw: str = Field(max_length=500_000)
    reference_normalized: str = Field(max_length=500_000)
    hypothesis_raw: str = Field(max_length=500_000)
    hypothesis_normalized: str = Field(max_length=500_000)
    language: str = Field(min_length=1, max_length=64)
    duration_ms: int = Field(gt=0)
    substitutions: int | None = Field(default=None, ge=0, alias="S")
    deletions: int | None = Field(default=None, ge=0, alias="D")
    insertions: int | None = Field(default=None, ge=0, alias="I")
    reference_tokens: int | None = Field(default=None, ge=0, alias="N")
    cer_status: MetricStatusValue
    cer: float | None = Field(default=None, ge=0)
    wall_time_ms: float | None = Field(default=None, ge=0)
    rtf: float | None = Field(default=None, ge=0)
    deadline_misses: int | None = Field(default=None, ge=0)
    finalization_latency_ms: float | None = Field(default=None, ge=0)
    error_status: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _validate_metric_state(self) -> Self:
        counts = (
            self.substitutions,
            self.deletions,
            self.insertions,
            self.reference_tokens,
        )
        if self.error_status is not None:
            if self.cer_status != "missing" or self.cer is not None:
                raise ValueError("failed hypothesis must use missing CER state")
            return self
        if self.cer_status == "supported":
            if (
                self.cer is None
                or any(value is None for value in counts)
            ):
                raise ValueError("supported CER requires value and S/D/I/N")
        elif self.cer is not None:
            raise ValueError("non-supported CER cannot carry a value")
        return self


class BlindHypothesisRecord(_FrozenModel):
    """运行阶段的不可变盲输出；结构上不存在 reference 与 CER 字段。"""

    sample_id: str = Field(min_length=1, max_length=200)
    scenario: str = Field(min_length=1, max_length=200)
    hypothesis_raw: str = Field(max_length=500_000)
    hypothesis_normalized: str = Field(max_length=500_000)
    language: str = Field(min_length=1, max_length=64)
    duration_ms: int = Field(gt=0)
    wall_time_ms: float | None = Field(default=None, ge=0)
    rtf: float | None = Field(default=None, ge=0)
    deadline_misses: int | None = Field(default=None, ge=0)
    finalization_latency_ms: float | None = Field(default=None, ge=0)
    error_status: str | None = Field(default=None, min_length=1, max_length=200)


def sha256_file(path: Path, *, block_size: int = 1024 * 1024) -> str:
    """按原始 bytes 计算文件 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def resolve_relative_file(root: Path, relative_path: str) -> Path:
    """解析并约束文件必须位于给定根目录内，包含 symlink 检查。"""
    normalized = relative_path.strip().replace("\\", "/")
    relative = PurePosixPath(normalized)
    if relative.is_absolute() or ".." in relative.parts or not normalized:
        raise ValueError("file path must be relative to its declared root")
    resolved_root = root.resolve(strict=True)
    candidate = (resolved_root / Path(*relative.parts)).resolve(strict=True)
    if not candidate.is_relative_to(resolved_root) or not candidate.is_file():
        raise ValueError("file path must resolve inside its declared root")
    return candidate


def verify_file_hashes(root: Path, expected_hashes: dict[str, str]) -> None:
    """核验模型清单中的每个相对文件及 SHA-256。"""
    for relative_path, expected_hash in expected_hashes.items():
        normalized = relative_path.strip().replace("\\", "/")
        relative = PurePosixPath(normalized)
        if relative.is_absolute() or ".." in relative.parts or not normalized:
            raise ValueError("file path must be relative to its declared root")
        resolved_root = root.resolve(strict=True)
        lexical_path = resolved_root / Path(*relative.parts)
        path = lexical_path.resolve(strict=True)
        if not path.is_file():
            raise ValueError("model file path must resolve to a file")
        if not path.is_relative_to(resolved_root):
            repository_root = resolved_root.parent.parent
            blobs_root = repository_root / "blobs"
            is_hf_snapshot_blob = (
                resolved_root.parent.name == "snapshots"
                and blobs_root.is_dir()
                and path.is_relative_to(blobs_root.resolve(strict=True))
            )
            if not is_hf_snapshot_blob:
                raise ValueError("model file symlink escapes its cache repository")
        if sha256_file(path) != expected_hash:
            raise ValueError(f"model file SHA-256 mismatch: {relative_path}")


def verify_git_checkout(repo_root: Path, expected_commit: str) -> None:
    """确认运行代码来自预注册 commit，且工作树没有未提交内容。"""
    resolved_root = repo_root.resolve(strict=True)
    try:
        head = subprocess.run(
            ["git", "-C", str(resolved_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(resolved_root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("cannot verify benchmark git checkout") from exc
    if head != expected_commit:
        raise ValueError("run manifest git_commit does not match checked-out HEAD")
    if status.strip():
        raise ValueError("benchmark requires a clean git checkout")


def _read_bounded_text(path: Path) -> str:
    if path.stat().st_size > _MAX_JSON_BYTES:
        raise ValueError(f"JSON file exceeds {_MAX_JSON_BYTES} bytes")
    return path.read_text(encoding="utf-8")


def load_run_manifest(path: Path) -> ASRRunManifest:
    """从 JSON 边界校验并读取运行清单。"""
    return ASRRunManifest.model_validate_json(_read_bounded_text(path))


def load_corpus_manifest(path: Path) -> CorpusManifest:
    """从 JSON 边界校验并读取语料清单。"""
    return CorpusManifest.model_validate_json(_read_bounded_text(path))


def load_corpus_input_manifest(path: Path) -> CorpusInputManifest:
    """读取不含参考文本的 runner 输入清单。"""
    return CorpusInputManifest.model_validate_json(_read_bounded_text(path))


def load_reference_manifest(path: Path) -> CorpusReferenceManifest:
    """仅在正式开盲后读取封存参考清单。"""
    return CorpusReferenceManifest.model_validate_json(_read_bounded_text(path))


def _write_frozen_model(path: Path, model: BaseModel, *, mode: int = 0o600) -> None:
    if path.exists():
        raise FileExistsError(f"frozen artifact already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    payload = model.model_dump_json(indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.replace(path)
        path.chmod(mode)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def write_corpus_input_manifest(path: Path, manifest: CorpusInputManifest) -> None:
    """原子写入 runner 输入清单且拒绝覆盖。"""
    _write_frozen_model(path, manifest)


def write_reference_manifest(
    path: Path,
    manifest: CorpusReferenceManifest,
    *,
    mode: int = 0o600,
) -> None:
    """原子写入独立参考清单；制备完成后可直接封存为 000。"""
    _write_frozen_model(path, manifest, mode=mode)


def write_run_manifest(path: Path, manifest: ASRRunManifest) -> None:
    """原子写入运行清单，避免中断后留下半个 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = manifest.model_dump_json(indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
