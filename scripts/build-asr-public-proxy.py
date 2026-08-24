#!/usr/bin/env python3
"""从固定 AliMeeting/ASCEND 制品生成项目外 public-proxy spec。"""

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
    SpeakerTurnCandidate,
    generate_speaker_turn_candidates,
    parse_long_textgrid,
)

try:
    import pyarrow.parquet as parquet
except ImportError as exc:  # pragma: no cover - CLI 边界
    raise SystemExit(
        "pyarrow is required; run with: uv run --with pyarrow python "
        "scripts/build-asr-public-proxy.py ..."
    ) from exc


Seed = "asr-public-proxy-v1-20260825"
Split = Literal["blind-core", "blind-reserve"]
_LICENSE_ALI = "OpenSLR SLR119; CC BY-SA 4.0"
_LICENSE_ASCEND = (
    "CAiRE/ASCEND@b65b9bb87a0412eb94a659660819060825e74b9f; "
    "CC BY-SA 4.0"
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CORE_ALI = ("R8001_M8004", "R8003_M8001", "R8008_M8013", "R8009_M8019")
_RESERVE_ALI = ("R8007_M8011", "R8009_M8018", "R8009_M8020")
_QUOTAS: dict[Split, dict[str, int]] = {
    "blind-core": {
        "public-meeting": 30 * 60_000,
        "public-code-switch": 10 * 60_000,
        "public-clean": 20 * 60_000,
    },
    "blind-reserve": {
        "public-meeting": 20 * 60_000,
        "public-code-switch": 8 * 60_000,
        "public-clean": 17 * 60_000,
    },
}


@dataclass(frozen=True, slots=True)
class CandidateMaterial:
    selection: SelectionCandidate
    sample: CorpusSourceSample
    wav_bytes: bytes | None = None


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
            raise ValueError("proxy utterance must be 16kHz mono PCM16 WAV")
        frames = stream.getnframes()
    if frames <= 0:
        raise ValueError("proxy utterance must have positive duration")
    if frames % 16:
        return None
    return frames // 16


def _safe_id(value: object) -> str:
    text = str(value).strip()
    if not _SAFE_ID.fullmatch(text):
        raise ValueError("dataset row identity contains unsafe characters")
    return text


def _ali_materials(
    *,
    corpus_root: Path,
    ali_root: Path,
    sessions: tuple[str, ...],
    split: Split,
) -> tuple[CandidateMaterial, ...]:
    far_root = ali_root / "Eval_Ali" / "Eval_Ali_far"
    materials: list[CandidateMaterial] = []
    for session in sessions:
        textgrid_path = far_root / "textgrid_dir" / f"{session}.TextGrid"
        audio_paths = tuple((far_root / "audio_dir").glob(f"{session}_MS*.wav"))
        if len(audio_paths) != 1:
            raise ValueError(f"AliMeeting far audio identity is ambiguous: {session}")
        audio_path = audio_paths[0]
        parsed = parse_long_textgrid(
            textgrid_path.read_text(encoding="utf-8"),
            session=f"ali-{session}",
            content_group=f"ali-{session}",
        )
        turns = generate_speaker_turn_candidates(parsed)
        for turn in turns:
            if not 1_000 <= turn.duration_ms <= 20_000:
                continue
            sample = _ali_sample(
                corpus_root=corpus_root,
                audio_path=audio_path,
                textgrid_path=textgrid_path,
                turn=turn,
                split=split,
            )
            materials.append(
                CandidateMaterial(
                    selection=SelectionCandidate(
                        candidate_id=sample.sample_id,
                        duration_ms=sample.expected_duration_ms,
                        scenario=sample.scenario,
                    ),
                    sample=sample,
                )
            )
    return tuple(materials)


def _ali_sample(
    *,
    corpus_root: Path,
    audio_path: Path,
    textgrid_path: Path,
    turn: SpeakerTurnCandidate,
    split: Split,
) -> CorpusSourceSample:
    source_id = f"ali:{audio_path.name}:{_sha256_bytes(textgrid_path.read_bytes())[:16]}"
    return CorpusSourceSample(
        sample_id=turn.candidate_id,
        split=split,
        source_path=_relative(audio_path, corpus_root),
        expected_duration_ms=turn.duration_ms,
        session_id=turn.session,
        source_id=source_id,
        content_group_id=turn.content_group,
        start_frame=turn.start_frame,
        end_frame=turn.end_frame,
        channel_index=0,
        scenario="public-meeting",
        language="zh",
        reference_raw=turn.reference,
        license_or_consent=_LICENSE_ALI,
        speakers=(f"ali-{turn.speaker}",),
        tags=("public-proxy", "meeting", "far-field", "non-overlap"),
    )


def _ascend_materials(
    *,
    parquet_root: Path,
    derived_relative_root: Path,
    dataset_split: str,
    split: Split,
) -> tuple[CandidateMaterial, ...]:
    materials: list[CandidateMaterial] = []
    for parquet_path in sorted(parquet_root.glob(f"{dataset_split}-*.parquet")):
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
    if duration_ms is None:
        return None
    if not 1_000 <= duration_ms <= 20_000:
        return None
    language = str(row["language"]).strip()
    scenario = "public-code-switch" if language == "mixed" else "public-clean"
    candidate_id = f"asc-{dataset_split}-{row_id}"
    relative_wav = derived_relative_root / "ascend" / f"{candidate_id}.wav"
    speaker = _safe_id(row["original_speaker_id"])
    session = _safe_id(row["session_id"])
    topic = str(row.get("topic") or "").strip()
    sample = CorpusSourceSample(
        sample_id=candidate_id,
        split=split,
        source_path=relative_wav.as_posix(),
        expected_duration_ms=duration_ms,
        session_id=f"asc-{dataset_split}-{session}",
        source_id=f"asc:{dataset_split}:{row_id}",
        content_group_id=f"asc-{dataset_split}-{session}",
        scenario=scenario,
        language="zh-en" if language == "mixed" else language,
        reference_raw=str(row["transcription"]),
        license_or_consent=_LICENSE_ASCEND,
        speakers=(f"asc-spk-{speaker}",),
        tags=("public-proxy", "code-switch" if language == "mixed" else "clean"),
        hotwords=tuple(word for word in (topic,) if word),
    )
    return CandidateMaterial(
        selection=SelectionCandidate(
            candidate_id=candidate_id,
            duration_ms=duration_ms,
            scenario=scenario,
        ),
        sample=sample,
        wav_bytes=payload,
    )


def _select_materials(
    materials: tuple[CandidateMaterial, ...],
    *,
    split: Split,
) -> tuple[CandidateMaterial, ...]:
    by_id = {material.selection.candidate_id: material for material in materials}
    if len(by_id) != len(materials):
        raise ValueError("public proxy candidate IDs must be globally unique")
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
    ali_root: Path,
    ascend_root: Path,
    output_root: Path,
) -> CorpusPreparationSpec:
    resolved_corpus_root = corpus_root.resolve(strict=True)
    if output_root.exists():
        raise FileExistsError(f"public proxy source output exists: {output_root}")
    relative_output = output_root.resolve(strict=False).relative_to(resolved_corpus_root)
    split_materials: dict[Split, tuple[CandidateMaterial, ...]] = {}
    for split, sessions, ascend_split in (
        ("blind-core", _CORE_ALI, "test"),
        ("blind-reserve", _RESERVE_ALI, "validation"),
    ):
        pool = (
            *_ali_materials(
                corpus_root=resolved_corpus_root,
                ali_root=ali_root,
                sessions=sessions,
                split=split,
            ),
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
            corpus_version="public-proxy-v1-20260825",
            samples=samples,
            required_duration_ms={
                "blind-core": 60 * 60_000,
                "blind-reserve": 45 * 60_000,
            },
            required_scenario_duration_ms=_QUOTAS,
            minimum_blind_speakers=20,
            minimum_speakers_per_look=10,
            required_tags={
                "blind-core": ("public-proxy",),
                "blind-reserve": ("public-proxy",),
            },
        )
        (staging / "preparation-spec.json").write_text(
            spec.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        (staging / "preparation-spec.json").chmod(0o600)
        provenance = {
            "schema_version": "1.0",
            "seed": Seed,
            "corpus_version": spec.corpus_version,
            "sample_count": len(samples),
            "split_counts": {
                split: len(split_materials[split]) for split in split_materials
            },
            "scenario_quotas_ms": _QUOTAS,
            "preparation_spec_contains_references": True,
            "provenance_contains_references": False,
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
    parser.add_argument("--ali-root", type=Path, required=True)
    parser.add_argument("--ascend-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    os.umask(0o077)
    spec = build_proxy_spec(
        corpus_root=args.corpus_root,
        ali_root=args.ali_root,
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
