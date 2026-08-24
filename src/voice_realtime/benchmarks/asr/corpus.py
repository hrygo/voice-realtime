"""把受权源音频确定性制备为项目外的冻结 ASR 语料。"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from voice_realtime.benchmarks.asr.manifest import (
    CorpusInputManifest,
    CorpusInputSample,
    CorpusReference,
    CorpusReferenceManifest,
    CorpusSplit,
    resolve_relative_file,
    sha256_file,
    write_corpus_input_manifest,
    write_reference_manifest,
)
from voice_realtime.benchmarks.asr.metrics import normalize_primary_text

_PCM_BYTES_PER_MILLISECOND = 16_000 * 1 * 2 // 1000
_MAX_SOURCE_BYTES = 8 * 1024 * 1024 * 1024
_FFMPEG_TIMEOUT_SECS = 120
_MAX_SPEC_BYTES = 64 * 1024 * 1024


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _default_blind_durations() -> dict[
    Literal["blind-core", "blind-reserve"], int
]:
    return {"blind-core": 3_600_000, "blind-reserve": 2_700_000}


def _default_blind_tags() -> dict[
    Literal["blind-core", "blind-reserve"], tuple[str, ...]
]:
    required = (
        "near-field",
        "meeting",
        "code-switch",
        "accent",
        "noise",
        "entity",
        "negative",
    )
    return {"blind-core": required, "blind-reserve": required}


class CorpusSourceSample(_FrozenModel):
    """制备前的受权源音频及冻结标注。"""

    sample_id: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    split: CorpusSplit
    source_path: str = Field(min_length=1, max_length=1000)
    expected_duration_ms: int = Field(gt=0)
    session_id: str = Field(min_length=1, max_length=200)
    scenario: str = Field(min_length=1, max_length=200)
    language: str = Field(min_length=1, max_length=64)
    reference_raw: str = Field(max_length=500_000)
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

    @field_validator("source_path")
    @classmethod
    def _relative_source_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts or not normalized:
            raise ValueError("source_path 必须是 source root 内的相对路径")
        if path.suffix.lower() not in {".wav", ".flac"}:
            raise ValueError("source_path 只允许 WAV 或 FLAC")
        return str(path)

    @field_validator("speakers", "tags", "hotwords")
    @classmethod
    def _unique_text_tuple(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value if item.strip())
        if len(normalized) != len(set(normalized)):
            raise ValueError("speaker/tag/hotword 必须唯一")
        return normalized


class CorpusPreparationSpec(_FrozenModel):
    """一次不可变语料制备的全部输入与配额。"""

    schema_version: Literal["1.0"] = "1.0"
    corpus_version: str = Field(min_length=1, max_length=200)
    normalization_version: Literal["nfkc-casefold-punct-space-v1"] = (
        "nfkc-casefold-punct-space-v1"
    )
    samples: tuple[CorpusSourceSample, ...] = Field(min_length=1)
    required_duration_ms: dict[Literal["blind-core", "blind-reserve"], int] = Field(
        default_factory=_default_blind_durations
    )
    minimum_blind_speakers: int = Field(default=20, ge=1)
    minimum_speakers_per_look: int = Field(default=6, ge=1)
    required_tags: dict[
        Literal["blind-core", "blind-reserve"], tuple[str, ...]
    ] = Field(default_factory=_default_blind_tags)

    @model_validator(mode="after")
    def _validate_identity_and_blind_isolation(self) -> Self:
        sample_ids = [sample.sample_id for sample in self.samples]
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("sample_id 必须唯一")
        core = tuple(sample for sample in self.samples if sample.split == "blind-core")
        reserve = tuple(
            sample for sample in self.samples if sample.split == "blind-reserve"
        )
        for split, samples in (("blind-core", core), ("blind-reserve", reserve)):
            expected = self.required_duration_ms.get(
                cast(Literal["blind-core", "blind-reserve"], split)
            )
            actual = sum(sample.expected_duration_ms for sample in samples)
            if expected is None or actual != expected:
                raise ValueError(f"{split} unique duration must equal frozen quota")
        core_sessions = {sample.session_id for sample in core}
        reserve_sessions = {sample.session_id for sample in reserve}
        if core_sessions & reserve_sessions:
            raise ValueError("Core/Reserve session 不得重叠")
        core_speakers = {speaker for sample in core for speaker in sample.speakers}
        reserve_speakers = {speaker for sample in reserve for speaker in sample.speakers}
        if core_speakers & reserve_speakers:
            raise ValueError("Core/Reserve speaker 不得重叠")
        if len(core_speakers) < self.minimum_speakers_per_look or len(
            reserve_speakers
        ) < self.minimum_speakers_per_look:
            raise ValueError("Core/Reserve 每个 look 的 speaker 数量不足")
        if len(core_speakers | reserve_speakers) < self.minimum_blind_speakers:
            raise ValueError("blind 全局唯一 speaker 数量不足")
        for split, samples in (("blind-core", core), ("blind-reserve", reserve)):
            observed_tags = {tag for sample in samples for tag in sample.tags}
            required_tags = set(
                self.required_tags[
                    cast(Literal["blind-core", "blind-reserve"], split)
                ]
            )
            if not required_tags.issubset(observed_tags):
                raise ValueError(f"{split} 缺少预注册场景标签")
        return self


@dataclass(frozen=True)
class PreparedCorpusBundle:
    """冻结后供 run manifest 绑定的关键身份。"""

    output_root: Path
    core_manifest_sha256: str
    reserve_manifest_sha256: str
    core_reference_sha256: str
    reserve_reference_sha256: str


def load_preparation_spec(path: Path) -> CorpusPreparationSpec:
    """在有界 JSON 边界读取严格语料制备规范。"""
    if path.stat().st_size > _MAX_SPEC_BYTES:
        raise ValueError("corpus preparation spec exceeds 64 MiB")
    return CorpusPreparationSpec.model_validate_json(path.read_text(encoding="utf-8"))


def quota_summary(spec: CorpusPreparationSpec) -> dict[str, dict[str, object]]:
    """按唯一样本汇总总时长；正交标签只进入各自维度。"""
    summary: dict[str, dict[str, object]] = {}
    for split in ("public", "dev", "blind-core", "blind-reserve", "reliability"):
        samples = tuple(sample for sample in spec.samples if sample.split == split)
        if not samples:
            continue
        tag_duration: dict[str, int] = {}
        for sample in samples:
            for tag in sample.tags:
                tag_duration[tag] = tag_duration.get(tag, 0) + sample.expected_duration_ms
        summary[split] = {
            "sample_count": len(samples),
            "unique_duration_ms": sum(sample.expected_duration_ms for sample in samples),
            "tag_duration_ms": dict(sorted(tag_duration.items())),
        }
    return summary


def _require_external_new_root(output_root: Path, repository_root: Path) -> None:
    resolved_output = output_root.resolve(strict=False)
    resolved_repository = repository_root.resolve(strict=False)
    if resolved_output.is_relative_to(resolved_repository):
        raise ValueError("corpus output_root must be outside the repository")
    if output_root.exists():
        raise FileExistsError(f"frozen corpus output already exists: {output_root}")


def _convert_to_pcm(source: Path, destination: Path, *, ffmpeg: str) -> None:
    if source.stat().st_size > _MAX_SOURCE_BYTES:
        raise ValueError("source audio exceeds 8 GiB")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-map_metadata",
            "-1",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-acodec",
            "pcm_s16le",
            "-f",
            "s16le",
            str(destination),
        ],
        check=True,
        capture_output=True,
        timeout=_FFMPEG_TIMEOUT_SECS,
    )
    destination.chmod(0o600)


def _write_json(path: Path, payload: object) -> None:
    if path.exists():
        raise FileExistsError(f"frozen artifact already exists: {path}")
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _write_split_manifests(
    *,
    staging_root: Path,
    spec: CorpusPreparationSpec,
    input_samples: dict[str, list[CorpusInputSample]],
    references: dict[str, list[CorpusReference]],
) -> tuple[Path, Path, Path, Path]:
    blind_paths: dict[str, Path] = {}
    reference_paths: dict[str, Path] = {}
    for split in ("blind-core", "blind-reserve"):
        manifest_path = staging_root / f"{split}.json"
        reference_path = staging_root / "sealed" / f"{split}.references.json"
        write_corpus_input_manifest(
            manifest_path,
            CorpusInputManifest(
                corpus_version=spec.corpus_version,
                normalization_version=spec.normalization_version,
                split=split,
                samples=tuple(input_samples[split]),
            ),
        )
        write_reference_manifest(
            reference_path,
            CorpusReferenceManifest(
                corpus_version=spec.corpus_version,
                normalization_version=spec.normalization_version,
                split=split,
                input_manifest_sha256=sha256_file(manifest_path),
                samples=tuple(references[split]),
            ),
        )
        blind_paths[split] = manifest_path
        reference_paths[split] = reference_path
    for split in ("public", "dev", "reliability"):
        samples = input_samples.get(split, [])
        if samples:
            manifest_path = staging_root / f"{split}.json"
            write_corpus_input_manifest(
                manifest_path,
                CorpusInputManifest(
                    corpus_version=spec.corpus_version,
                    normalization_version=spec.normalization_version,
                    split=cast(CorpusSplit, split),
                    samples=tuple(samples),
                ),
            )
            write_reference_manifest(
                staging_root / "references" / f"{split}.references.json",
                CorpusReferenceManifest(
                    corpus_version=spec.corpus_version,
                    normalization_version=spec.normalization_version,
                    split=cast(CorpusSplit, split),
                    input_manifest_sha256=sha256_file(manifest_path),
                    samples=tuple(references[split]),
                ),
            )
    return (
        blind_paths["blind-core"],
        blind_paths["blind-reserve"],
        reference_paths["blind-core"],
        reference_paths["blind-reserve"],
    )


def prepare_corpus(
    *,
    spec: CorpusPreparationSpec,
    source_root: Path,
    output_root: Path,
    repository_root: Path,
    ffmpeg: str = "ffmpeg",
) -> PreparedCorpusBundle:
    """一次转码并原子冻结清单、参考和 checksum；拒绝覆盖。"""
    _require_external_new_root(output_root, repository_root)
    resolved_source_root = source_root.resolve(strict=True)
    output_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging_root = Path(
        tempfile.mkdtemp(prefix=f".{output_root.name}.", dir=output_root.parent)
    )
    staging_root.chmod(0o700)
    splits = ("public", "dev", "blind-core", "blind-reserve", "reliability")
    input_samples: dict[str, list[CorpusInputSample]] = {split: [] for split in splits}
    references: dict[str, list[CorpusReference]] = {split: [] for split in splits}
    try:
        for source_sample in spec.samples:
            source = resolve_relative_file(resolved_source_root, source_sample.source_path)
            relative_pcm = f"pcm/{source_sample.sample_id}.pcm"
            pcm = staging_root / relative_pcm
            _convert_to_pcm(source, pcm, ffmpeg=ffmpeg)
            pcm_size = pcm.stat().st_size
            if pcm_size == 0 or pcm_size % _PCM_BYTES_PER_MILLISECOND:
                raise ValueError(f"invalid s16le PCM length: {source_sample.sample_id}")
            duration_ms = pcm_size // _PCM_BYTES_PER_MILLISECOND
            if duration_ms != source_sample.expected_duration_ms:
                raise ValueError(f"audio duration mismatch: {source_sample.sample_id}")
            reference_normalized = normalize_primary_text(source_sample.reference_raw)
            split = source_sample.split
            input_samples[split].append(
                CorpusInputSample(
                    sample_id=source_sample.sample_id,
                    audio_path=relative_pcm,
                    source_sha256=sha256_file(source),
                    audio_sha256=sha256_file(pcm),
                    duration_ms=duration_ms,
                    session_id=source_sample.session_id,
                    scenario=source_sample.scenario,
                    language=source_sample.language,
                    license_or_consent=source_sample.license_or_consent,
                    speakers=source_sample.speakers,
                    tags=source_sample.tags,
                    hotwords=source_sample.hotwords,
                )
            )
            references[split].append(
                CorpusReference(
                    sample_id=source_sample.sample_id,
                    reference_raw=source_sample.reference_raw,
                    reference_normalized=reference_normalized,
                )
            )
        core, reserve, core_reference, reserve_reference = _write_split_manifests(
            staging_root=staging_root,
            spec=spec,
            input_samples=input_samples,
            references=references,
        )
        hashes = {
            str(path.relative_to(staging_root)): sha256_file(path)
            for path in sorted(staging_root.rglob("*"))
            if path.is_file()
        }
        _write_json(staging_root / "quotas.json", quota_summary(spec))
        _write_json(staging_root / "checksums.json", hashes)
        core_hash = sha256_file(core)
        reserve_hash = sha256_file(reserve)
        core_reference_hash = sha256_file(core_reference)
        reserve_reference_hash = sha256_file(reserve_reference)
        core_reference.chmod(0)
        reserve_reference.chmod(0)
        staging_root.replace(output_root)
    except BaseException:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise
    return PreparedCorpusBundle(
        output_root=output_root,
        core_manifest_sha256=core_hash,
        reserve_manifest_sha256=reserve_hash,
        core_reference_sha256=core_reference_hash,
        reserve_reference_sha256=reserve_reference_hash,
    )
