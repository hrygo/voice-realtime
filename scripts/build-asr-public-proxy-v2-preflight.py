#!/usr/bin/env python3
"""由 Public Proxy v2 preparation spec 生成不含逐字稿的 preflight metadata。"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

from voice_realtime.benchmarks.asr.corpus import CorpusPreparationSpec
from voice_realtime.benchmarks.asr.manifest import sha256_file
from voice_realtime.benchmarks.asr.metrics import normalize_primary_text
from voice_realtime.benchmarks.asr.preflight import (
    BlindCandidateMetadata,
    BlindPreflightSpec,
    ReferenceCatalogEntry,
    SourceCatalogEntry,
)


def _opaque(prefix: str, identity: str) -> str:
    digest = hashlib.sha256(identity.encode()).hexdigest()[:32]
    return f"{prefix}:{digest}"


def _reference_sha256(raw: str) -> str:
    normalized = normalize_primary_text(raw)
    return hashlib.sha256(f"{raw}\0{normalized}".encode()).hexdigest()


def _resolve_source(source_root: Path, relative_path: str) -> Path:
    root = source_root.resolve(strict=True)
    source = (root / relative_path).resolve(strict=True)
    if not source.is_relative_to(root) or not source.is_file():
        raise ValueError("source path escapes corpus root or is not a file")
    return source


def build_metadata(
    *, spec_path: Path, source_root: Path, output_path: Path
) -> BlindPreflightSpec:
    if output_path.exists():
        raise FileExistsError(f"preflight metadata exists: {output_path}")
    spec = CorpusPreparationSpec.model_validate_json(
        spec_path.read_text(encoding="utf-8")
    )
    source_hashes: dict[Path, str] = {}
    source_entries: dict[str, SourceCatalogEntry] = {}
    candidates: list[BlindCandidateMetadata] = []
    references: list[ReferenceCatalogEntry] = []
    for sample in spec.samples:
        source = _resolve_source(source_root, sample.source_path)
        source_hash = source_hashes.get(source)
        if source_hash is None:
            source_hash = sha256_file(source)
            source_hashes[source] = source_hash
        source_token = _opaque("source", sample.source_id)
        if source_token not in source_entries:
            is_aishell4 = sample.license_or_consent.startswith("OpenSLR SLR111")
            source_entries[source_token] = SourceCatalogEntry(
                source_token=source_token,
                source_snapshot_sha256=source_hash,
                authorization_ref=(
                    "authorization:openslr111-cc-by-sa-4.0"
                    if is_aishell4
                    else "authorization:ascend-b65b9bb-cc-by-sa-4.0"
                ),
                authorization_status="approved",
                deidentification_status="verified",
                human_reviewed=False,
                review_basis="publisher-corpus",
            )
        start_frame = sample.start_frame or 0
        end_frame = sample.end_frame or sample.expected_duration_ms * 16
        candidates.append(
            BlindCandidateMetadata(
                sample_id=sample.sample_id,
                split=sample.split,
                source_token=source_token,
                source_locator=f"source/{source_token.removeprefix('source:')}.audio",
                duration_ms=sample.expected_duration_ms,
                session_token=_opaque("session", sample.session_id),
                content_group_token=_opaque("content", sample.content_group_id),
                analysis_cluster_token=(
                    f"cluster:{sample.analysis_cluster_id or sample.content_group_id}"
                ),
                speaker_tokens=tuple(
                    _opaque("speaker", speaker) for speaker in sample.speakers
                ),
                start_frame=start_frame,
                end_frame=end_frame,
                channel_index=sample.channel_index or 0,
                scenario=sample.scenario,
                language=sample.language,
                tags=sample.tags,
                synthetic=False,
            )
        )
        references.append(
            ReferenceCatalogEntry(
                sample_id=sample.sample_id,
                reference_sha256=_reference_sha256(sample.reference_raw),
                reference_revision=(
                    "publisher-openslr111-v1"
                    if sample.license_or_consent.startswith("OpenSLR SLR111")
                    else "publisher-ascend-b65b9bb"
                ),
                normalization_version=spec.normalization_version,
                annotation_status="publisher_verified",
                annotator_count=0,
                adjudicated=False,
            )
        )
    metadata = BlindPreflightSpec(
        corpus_version=spec.corpus_version,
        evidence_class="public-operational-proxy",
        normalization_version=spec.normalization_version,
        sources=tuple(sorted(source_entries.values(), key=lambda item: item.source_token)),
        candidates=tuple(candidates),
        references=tuple(references),
        required_duration_ms=spec.required_duration_ms,
        required_scenario_duration_ms=spec.required_scenario_duration_ms,
        minimum_speakers={
            "blind-core": spec.minimum_speakers_per_look,
            "blind-reserve": spec.minimum_speakers_per_look,
        },
    )
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_path.parent.chmod(0o700)
    output_path.write_text(metadata.model_dump_json(indent=2) + "\n", encoding="utf-8")
    output_path.chmod(0o600)
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    metadata = build_metadata(
        spec_path=args.spec,
        source_root=args.source_root,
        output_path=args.output,
    )
    print(
        f"corpus_version={metadata.corpus_version} "
        f"candidates={len(metadata.candidates)} sources={len(metadata.sources)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
