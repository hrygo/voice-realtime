"""目标域 blind metadata-only 预检契约测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from voice_realtime.benchmarks.asr.cli import main
from voice_realtime.benchmarks.asr.preflight import (
    BlindCandidateMetadata,
    BlindPreflightSpec,
    ReferenceCatalogEntry,
    SourceCatalogEntry,
    evaluate_blind_preflight,
    run_blind_preflight,
)


def _source(source_token: str) -> SourceCatalogEntry:
    return SourceCatalogEntry(
        source_token=source_token,
        source_snapshot_sha256="a" * 64,
        authorization_ref="authorization:approved-001",
        authorization_status="approved",
        deidentification_status="verified",
        human_reviewed=True,
    )


def _candidate(
    sample_id: str,
    *,
    split: str,
    duration_ms: int,
    scenario: str = "near-field",
) -> BlindCandidateMetadata:
    suffix = sample_id.replace("_", "-")
    return BlindCandidateMetadata(
        sample_id=sample_id,
        split=split,
        source_token=f"source:{suffix}",
        source_locator=f"audio/{sample_id}.wav",
        duration_ms=duration_ms,
        session_token=f"session:{suffix}",
        content_group_token=f"content:{suffix}",
        analysis_cluster_token=f"cluster:{suffix}",
        speaker_tokens=(f"speaker:{suffix}",),
        start_frame=0,
        end_frame=duration_ms * 16,
        channel_index=0,
        scenario=scenario,
        language="zh",
        tags=(scenario,),
        synthetic=False,
    )


def _reference(sample_id: str) -> ReferenceCatalogEntry:
    return ReferenceCatalogEntry(
        sample_id=sample_id,
        reference_sha256="b" * 64,
        reference_revision="adjudicated-v1",
        normalization_version="nfkc-casefold-punct-space-v1",
        annotation_status="adjudicated",
        annotator_count=2,
        adjudicated=True,
    )


def _spec() -> BlindPreflightSpec:
    core = _candidate("core_001", split="blind-core", duration_ms=60_000)
    reserve = _candidate("reserve_001", split="blind-reserve", duration_ms=45_000)
    return BlindPreflightSpec(
        corpus_version="target-domain-test-v1",
        sources=(_source(core.source_token), _source(reserve.source_token)),
        candidates=(core, reserve),
        references=(_reference(core.sample_id), _reference(reserve.sample_id)),
        required_duration_ms={"blind-core": 60_000, "blind-reserve": 45_000},
        required_scenario_duration_ms={
            "blind-core": {"near-field": 60_000},
            "blind-reserve": {"near-field": 45_000},
        },
        minimum_speakers={"blind-core": 1, "blind-reserve": 1},
    )


def test_metadata_only_preflight_can_be_ready_without_reading_audio() -> None:
    report = evaluate_blind_preflight(_spec(), metadata_sha256="c" * 64)

    assert report.status == "metadata_ready"
    assert report.blockers == ()
    assert report.unique_duration_ms == {
        "blind-core": 60_000,
        "blind-reserve": 45_000,
    }
    assert "blind_ready" not in report.model_dump_json()


def test_preflight_reports_reference_authorization_and_cross_look_gaps() -> None:
    spec = _spec()
    reserve = spec.candidates[1].model_copy(
        update={"session_token": spec.candidates[0].session_token}
    )
    pending_source = spec.sources[1].model_copy(
        update={"authorization_status": "pending"}
    )
    incomplete = spec.model_copy(
        update={
            "candidates": (spec.candidates[0], reserve),
            "sources": (spec.sources[0], pending_source),
            "references": (spec.references[0],),
        }
    )

    report = evaluate_blind_preflight(incomplete, metadata_sha256="c" * 64)

    assert report.status == "incomplete"
    assert "cross_look_session_overlap" in report.blockers
    assert "source_authorization_not_approved" in report.blockers
    assert "reference_sample_set_mismatch" in report.blockers


def test_preflight_schema_forbids_reference_text_and_non_opaque_identity() -> None:
    payload = _reference("sample").model_dump()
    payload["reference_raw"] = "不得进入 metadata"
    with pytest.raises(ValidationError):
        ReferenceCatalogEntry.model_validate(payload)

    with pytest.raises(ValidationError, match="opaque token"):
        _candidate("sample", split="blind-core", duration_ms=1_000).model_copy(
            update={"session_token": "/Users/private/session"}
        ).__class__.model_validate(
            {
                **_candidate(
                    "sample",
                    split="blind-core",
                    duration_ms=1_000,
                ).model_dump(),
                "session_token": "/Users/private/session",
            }
        )


def test_run_preflight_requires_external_metadata_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    metadata = external / "blind-preflight.json"
    metadata.write_text(_spec().model_dump_json(indent=2), encoding="utf-8")
    output = external / "preflight-report.json"

    report = run_blind_preflight(
        metadata_path=metadata,
        output_path=output,
        repository_root=repository,
    )

    assert report.status == "metadata_ready"
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "metadata_ready"
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        run_blind_preflight(
            metadata_path=metadata,
            output_path=output,
            repository_root=repository,
        )

    private_metadata = repository / "metadata.json"
    private_metadata.write_text(_spec().model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="outside the repository"):
        run_blind_preflight(
            metadata_path=private_metadata,
            output_path=external / "other-report.json",
            repository_root=repository,
        )


def test_preflight_corpus_cli_writes_metadata_status(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    metadata = external / "metadata.json"
    metadata.write_text(_spec().model_dump_json(), encoding="utf-8")
    output = external / "report.json"

    exit_code = main(
        [
            "preflight-corpus",
            "--metadata",
            str(metadata),
            "--output-report",
            str(output),
            "--repo-root",
            str(repository),
        ]
    )

    assert exit_code == 0
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "metadata_ready"
