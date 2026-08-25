#!/usr/bin/env python3
"""从固定 AISHELL-4/ASCEND 制品生成项目外 Public Operational Proxy v2。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
import wave
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Literal

from voice_realtime.benchmarks.asr.corpus import (
    CorpusPreparationSpec,
    CorpusSourceSample,
)
from voice_realtime.benchmarks.asr.public_proxy import (
    SelectionCandidate,
    select_scenario_quotas,
)
from voice_realtime.benchmarks.asr.public_proxy_sources import (
    generate_non_speech_candidates,
    generate_speaker_turn_candidates,
    normalize_aishell4_reference,
    parse_long_textgrid,
)

try:
    import pyarrow.parquet as parquet
except ImportError as exc:  # pragma: no cover - CLI boundary
    raise SystemExit(
        "pyarrow is required; run with: uv run --with pyarrow python "
        "scripts/build-asr-public-proxy-v2.py ..."
    ) from exc


Seed = "asr-public-operational-proxy-v2-20260825"
CorpusVersion = "public-operational-proxy-v2-20260825"
Split = Literal["blind-core", "blind-reserve"]
_LICENSE_AISHELL4 = "OpenSLR SLR111; CC BY-SA 4.0"
_LICENSE_ASCEND = (
    "CAiRE/ASCEND@b65b9bb87a0412eb94a659660819060825e74b9f; "
    "CC BY-SA 4.0"
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CORE_AISHELL4 = (
    "L_R003S01C02",
    "L_R003S03C02",
    "L_R004S01C01",
    "L_R004S03C01",
    "M_R003S01C01",
    "M_R003S04C01",
    "S_R003S01C01",
    "S_R003S03C01",
    "S_R004S01C01",
    "S_R004S03C01",
)
_RESERVE_AISHELL4 = (
    "L_R003S02C02",
    "L_R003S04C02",
    "L_R004S02C01",
    "L_R004S06C01",
    "M_R003S02C01",
    "M_R003S05C01",
    "S_R003S02C01",
    "S_R003S04C01",
    "S_R004S02C01",
    "S_R004S04C01",
)
_QUOTAS: dict[Split, dict[str, int]] = {
    "blind-core": {
        "public-meeting": 42 * 60_000,
        "public-code-switch": 9 * 60_000,
        "public-clean": 6 * 60_000,
        "public-negative": 3 * 60_000,
    },
    "blind-reserve": {
        "public-meeting": 31 * 60_000 + 30_000,
        "public-code-switch": 6 * 60_000 + 45_000,
        "public-clean": 4 * 60_000 + 30_000,
        "public-negative": 2 * 60_000 + 15_000,
    },
}


@dataclass(frozen=True, slots=True)
class CandidateMaterial:
    selection: SelectionCandidate
    sample: CorpusSourceSample
    wav_bytes: bytes | None = None


@dataclass(frozen=True, slots=True)
class Aishell4Pool:
    materials: tuple[CandidateMaterial, ...]
    collapsed_interval_count: int


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _relative(path: Path, root: Path) -> str:
    resolved = path.resolve(strict=True)
    resolved_root = root.resolve(strict=True)
    if not resolved.is_relative_to(resolved_root):
        raise ValueError(f"source path escapes corpus root: {path}")
    return resolved.relative_to(resolved_root).as_posix()


def _wav_duration_ms(payload: bytes) -> int | None:
    with wave.open(BytesIO(payload), "rb") as stream:
        if (
            stream.getframerate() != 16_000
            or stream.getnchannels() != 1
            or stream.getsampwidth() != 2
            or stream.getcomptype() != "NONE"
        ):
            raise ValueError("ASCEND utterance must be 16kHz mono PCM16 WAV")
        frames = stream.getnframes()
    if frames <= 0:
        raise ValueError("ASCEND utterance must have positive duration")
    if frames % 16:
        return None
    return frames // 16


def _safe_id(value: object) -> str:
    text = str(value).strip()
    if not _SAFE_ID.fullmatch(text):
        raise ValueError("dataset row identity contains unsafe characters")
    return text


def _aishell4_materials(
    *,
    corpus_root: Path,
    aishell4_root: Path,
    sessions: tuple[str, ...],
    split: Split,
) -> Aishell4Pool:
    materials: list[CandidateMaterial] = []
    collapsed_interval_count = 0
    for stem in sessions:
        audio_path = aishell4_root / "wav" / f"{stem}.flac"
        textgrid_path = aishell4_root / "TextGrid" / f"{stem}.TextGrid"
        if not audio_path.is_file() or not textgrid_path.is_file():
            raise FileNotFoundError(f"AISHELL-4 session is incomplete: {stem}")
        session_id = f"a4-{stem}"
        parsed = parse_long_textgrid(
            textgrid_path.read_text(encoding="utf-8"),
            session=session_id,
            content_group=session_id,
            boundary_policy="nearest-ms",
        )
        collapsed_interval_count += parsed.collapsed_interval_count
        source_id = f"aishell4:{stem}:{_sha256_bytes(textgrid_path.read_bytes())[:16]}"
        source_path = _relative(audio_path, corpus_root)
        for turn in generate_speaker_turn_candidates(parsed):
            if not 1_000 <= turn.duration_ms <= 20_000:
                continue
            reference = normalize_aishell4_reference(turn.reference)
            if not reference:
                continue
            sample_id = f"a4-{turn.candidate_id.removeprefix('ali-')}"
            sample = CorpusSourceSample(
                sample_id=sample_id,
                split=split,
                source_path=source_path,
                expected_duration_ms=turn.duration_ms,
                session_id=session_id,
                source_id=source_id,
                content_group_id=session_id,
                analysis_cluster_id=session_id,
                start_frame=turn.start_frame,
                end_frame=turn.end_frame,
                channel_index=0,
                scenario="public-meeting",
                language="zh",
                reference_raw=reference,
                license_or_consent=_LICENSE_AISHELL4,
                speakers=(f"{session_id}-{turn.speaker}",),
                tags=(
                    "public-proxy-v2",
                    "meeting",
                    "far-field",
                    "non-overlap",
                    "aishell4",
                ),
            )
            materials.append(
                CandidateMaterial(
                    selection=SelectionCandidate(
                        candidate_id=sample_id,
                        duration_ms=turn.duration_ms,
                        scenario=sample.scenario,
                    ),
                    sample=sample,
                )
            )
        for negative in generate_non_speech_candidates(parsed):
            sample_id = f"a4-{negative.candidate_id}"
            sample = CorpusSourceSample(
                sample_id=sample_id,
                split=split,
                source_path=source_path,
                expected_duration_ms=negative.duration_ms,
                session_id=session_id,
                source_id=source_id,
                content_group_id=session_id,
                analysis_cluster_id=session_id,
                start_frame=negative.start_frame,
                end_frame=negative.end_frame,
                channel_index=0,
                scenario="public-negative",
                language="zh",
                reference_raw="",
                license_or_consent=_LICENSE_AISHELL4,
                speakers=(),
                tags=(
                    "public-proxy-v2",
                    "negative",
                    "real-background",
                    "aishell4",
                ),
            )
            materials.append(
                CandidateMaterial(
                    selection=SelectionCandidate(
                        candidate_id=sample_id,
                        duration_ms=negative.duration_ms,
                        scenario=sample.scenario,
                    ),
                    sample=sample,
                )
            )
    return Aishell4Pool(
        materials=tuple(materials),
        collapsed_interval_count=collapsed_interval_count,
    )


def _ascend_materials(
    *,
    parquet_root: Path,
    derived_relative_root: Path,
    dataset_split: str,
    split: Split,
) -> tuple[CandidateMaterial, ...]:
    materials: list[CandidateMaterial] = []
    parquet_paths = tuple(sorted(parquet_root.glob(f"{dataset_split}-*.parquet")))
    if not parquet_paths:
        raise FileNotFoundError(f"ASCEND parquet split is missing: {dataset_split}")
    for parquet_path in parquet_paths:
        table = parquet.read_table(
            parquet_path,
            columns=[
                "id",
                "audio",
                "transcription",
                "language",
                "original_speaker_id",
                "session_id",
                "topic",
            ],
        )
        for row in table.to_pylist():
            material = _ascend_row_material(
                row=row,
                derived_relative_root=derived_relative_root,
                dataset_split=dataset_split,
                split=split,
            )
            if material is not None:
                materials.append(material)
    return tuple(materials)


def _ascend_row_material(
    *,
    row: dict[str, Any],
    derived_relative_root: Path,
    dataset_split: str,
    split: Split,
) -> CandidateMaterial | None:
    row_id = _safe_id(row["id"])
    audio = row.get("audio")
    if not isinstance(audio, dict) or not isinstance(audio.get("bytes"), bytes):
        raise ValueError(f"ASCEND audio bytes missing: {row_id}")
    payload = audio["bytes"]
    duration_ms = _wav_duration_ms(payload)
    if duration_ms is None or not 1_000 <= duration_ms <= 20_000:
        return None
    language = str(row["language"]).strip()
    scenario = "public-code-switch" if language == "mixed" else "public-clean"
    sample_id = f"a4p-asc-{dataset_split}-{row_id}"
    relative_wav = derived_relative_root / "ascend" / f"{sample_id}.wav"
    speaker = _safe_id(row["original_speaker_id"])
    session = _safe_id(row["session_id"])
    namespaced_session = f"asc-{dataset_split}-{session}"
    topic = str(row.get("topic") or "").strip()
    sample = CorpusSourceSample(
        sample_id=sample_id,
        split=split,
        source_path=relative_wav.as_posix(),
        expected_duration_ms=duration_ms,
        session_id=namespaced_session,
        source_id=f"asc:{dataset_split}:{row_id}",
        content_group_id=namespaced_session,
        analysis_cluster_id=namespaced_session,
        scenario=scenario,
        language="zh-en" if language == "mixed" else language,
        reference_raw=str(row["transcription"]),
        license_or_consent=_LICENSE_ASCEND,
        speakers=(f"asc-{dataset_split}-spk-{speaker}",),
        tags=(
            "public-proxy-v2",
            "code-switch" if language == "mixed" else "clean",
            "ascend",
        ),
        hotwords=tuple(word for word in (topic,) if word),
    )
    return CandidateMaterial(
        selection=SelectionCandidate(
            candidate_id=sample_id,
            duration_ms=duration_ms,
            scenario=scenario,
        ),
        sample=sample,
        wav_bytes=payload,
    )


def _select_materials(
    materials: tuple[CandidateMaterial, ...], *, split: Split
) -> tuple[CandidateMaterial, ...]:
    by_id = {material.selection.candidate_id: material for material in materials}
    if len(by_id) != len(materials):
        raise ValueError("Public Proxy v2 candidate IDs must be globally unique")
    selected = select_scenario_quotas(
        tuple(material.selection for material in materials),
        quotas_ms=_QUOTAS[split],
        seed=f"{Seed}:{split}",
    )
    return tuple(
        by_id[candidate.candidate_id]
        for scenario in sorted(selected)
        for candidate in selected[scenario]
    )


def build_proxy_spec(
    *,
    corpus_root: Path,
    aishell4_root: Path,
    ascend_root: Path,
    output_root: Path,
) -> CorpusPreparationSpec:
    resolved_corpus_root = corpus_root.resolve(strict=True)
    if output_root.exists():
        raise FileExistsError(f"Public Proxy v2 source output exists: {output_root}")
    relative_output = output_root.resolve(strict=False).relative_to(resolved_corpus_root)
    split_materials: dict[Split, tuple[CandidateMaterial, ...]] = {}
    collapsed_intervals: dict[Split, int] = {}
    for split, sessions, ascend_split in (
        ("blind-core", _CORE_AISHELL4, "train"),
        ("blind-reserve", _RESERVE_AISHELL4, "validation"),
    ):
        aishell4_pool = _aishell4_materials(
            corpus_root=resolved_corpus_root,
            aishell4_root=aishell4_root,
            sessions=sessions,
            split=split,
        )
        collapsed_intervals[split] = aishell4_pool.collapsed_interval_count
        pool = (
            *aishell4_pool.materials,
            *_ascend_materials(
                parquet_root=ascend_root,
                derived_relative_root=relative_output,
                dataset_split=ascend_split,
                split=split,
            ),
        )
        split_materials[split] = _select_materials(tuple(pool), split=split)

    output_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_root.name}.", dir=output_root.parent))
    staging.chmod(0o700)
    try:
        for material in (*split_materials["blind-core"], *split_materials["blind-reserve"]):
            if material.wav_bytes is None:
                continue
            destination = staging / "ascend" / f"{material.sample.sample_id}.wav"
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.write_bytes(material.wav_bytes)
            destination.chmod(0o600)
        samples = tuple(
            material.sample
            for split in ("blind-core", "blind-reserve")
            for material in split_materials[split]
        )
        spec = CorpusPreparationSpec(
            corpus_version=CorpusVersion,
            samples=samples,
            required_duration_ms={
                "blind-core": 60 * 60_000,
                "blind-reserve": 45 * 60_000,
            },
            required_scenario_duration_ms=_QUOTAS,
            minimum_blind_speakers=20,
            minimum_speakers_per_look=10,
            required_tags={
                "blind-core": ("public-proxy-v2",),
                "blind-reserve": ("public-proxy-v2",),
            },
        )
        (staging / "preparation-spec.json").write_text(
            spec.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        (staging / "preparation-spec.json").chmod(0o600)
        provenance = {
            "schema_version": "1.0",
            "seed": Seed,
            "corpus_version": spec.corpus_version,
            "evidence_class": "public-operational-proxy",
            "external_validity": "limited; not private target-domain evidence",
            "aishell4": {
                "source": "OpenSLR SLR111 test.tar.gz",
                "license": "CC BY-SA 4.0",
                "archive_sha256": (
                    "7e5d306b5f18ab66fcd7e0380c90979b47fd9576bfa8e67e6353bdec7c14a35a"
                ),
                "boundary_policy": "nearest-ms",
                "maximum_boundary_error_ms": 0.5,
                "collapsed_interval_count": collapsed_intervals,
                "channel_index": 0,
                "core_sessions": _CORE_AISHELL4,
                "reserve_sessions": _RESERVE_AISHELL4,
                "speaker_identity": "session-local tier ID namespaced by session",
            },
            "ascend": {
                "revision": "b65b9bb87a0412eb94a659660819060825e74b9f",
                "license": "CC BY-SA 4.0",
                "core_source_split": "train",
                "reserve_source_split": "validation",
            },
            "scenario_quotas_ms": _QUOTAS,
            "sample_count": len(samples),
            "split_counts": {
                split: len(split_materials[split]) for split in split_materials
            },
            "preparation_spec_contains_references": True,
            "provenance_contains_references": False,
            "synthetic_samples_in_blind": 0,
        }
        (staging / "provenance.json").write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (staging / "provenance.json").chmod(0o600)
        staging.replace(output_root)
        return spec
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-root", type=Path, required=True)
    parser.add_argument("--aishell4-root", type=Path, required=True)
    parser.add_argument("--ascend-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    os.umask(0o077)
    spec = build_proxy_spec(
        corpus_root=args.corpus_root,
        aishell4_root=args.aishell4_root,
        ascend_root=args.ascend_root,
        output_root=args.output_root,
    )
    print(
        json.dumps(
            {
                "corpus_version": spec.corpus_version,
                "samples": len(spec.samples),
                "output_root": str(args.output_root),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
