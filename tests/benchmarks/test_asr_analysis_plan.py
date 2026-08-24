"""ASR Core/Reserve 序贯分析计划冻结测试。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from voice_realtime.benchmarks.asr.analysis_plan import (
    AnalysisPlan,
    AnalysisPlanDesign,
    DecisionFamily,
    freeze_analysis_plan,
    freeze_formal_analysis_plan,
)
from voice_realtime.benchmarks.asr.manifest import (
    CorpusInputManifest,
    CorpusInputSample,
    CorpusReference,
    CorpusReferenceManifest,
    sha256_file,
    write_corpus_input_manifest,
    write_reference_manifest,
)
from voice_realtime.benchmarks.asr.preflight import BlindPreflightReport


def _plan(**updates: object) -> AnalysisPlan:
    values: dict[str, object] = {
        "candidate_ids": ("qwen3-mps", "sensevoice-cpu", "funasr-mps"),
        "core_manifest_sha256": "a" * 64,
        "reserve_manifest_sha256": "b" * 64,
        "core_reference_sha256": "c" * 64,
        "reserve_reference_sha256": "d" * 64,
        "bootstrap_seeds": (2026082501, 2026082502),
        "pilot_baseline_cer": 0.20,
    }
    values.update(updates)
    return AnalysisPlan.model_validate(values)


def test_analysis_plan_freezes_alpha_mde_futility_and_two_seeds() -> None:
    plan = _plan()

    assert plan.look_alpha == (0.01, 0.04)
    assert plan.decision_confidence == (0.99, 0.96)
    assert plan.conditional_power_futility == 0.20
    assert plan.minimum_detectable_effect == pytest.approx(0.01)
    assert plan.bootstrap_seeds == (2026082501, 2026082502)


def test_formal_analysis_plan_requires_preflight_profiles_clusters_and_endpoints() -> None:
    with pytest.raises(ValidationError, match="formal analysis plan"):
        _plan(evidence_tier="formal")

    plan = _plan(
        evidence_tier="formal",
        candidate_profile_sha256=dict.fromkeys(
            ("qwen3-mps", "sensevoice-cpu", "funasr-mps"),
            "e" * 64,
        ),
        preflight_report_sha256="f" * 64,
        core_duration_ms=3_600_000,
        reserve_duration_ms=2_700_000,
        core_analysis_cluster_ids=("cluster:core",),
        reserve_analysis_cluster_ids=("cluster:reserve",),
        analysis_cluster_ids=("cluster:core", "cluster:reserve"),
        primary_endpoints=("macro_cer",),
        normalization_version="nfkc-casefold-punct-space-v1",
        filtering_rules=("retain_all_failures",),
        decision_families=(
            DecisionFamily(
                family_id="meeting",
                baseline_id="qwen3-mps",
                candidate_ids=("funasr-mps",),
                pilot_baseline_cer=0.20,
            ),
        ),
    )

    assert plan.evidence_tier == "formal"
    assert plan.allowed_stopping_states == ("core", "reserve", "completed")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("look_alpha", (0.02, 0.04)),
        ("conditional_power_futility", 0.25),
        ("bootstrap_seeds", (1, 1)),
        ("candidate_ids", ("qwen3-mps", "qwen3-mps")),
    ],
)
def test_analysis_plan_rejects_unregistered_sequential_design(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        _plan(**{field: value})


def test_freeze_analysis_requires_both_sealed_references_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    core_manifest = tmp_path / "blind-core.json"
    reserve_manifest = tmp_path / "blind-reserve.json"
    core_reference = tmp_path / "blind-core.references.json"
    reserve_reference = tmp_path / "blind-reserve.references.json"
    for path, payload in (
        (core_manifest, b"core"),
        (reserve_manifest, b"reserve"),
        (core_reference, b"core-ref"),
        (reserve_reference, b"reserve-ref"),
    ):
        path.write_bytes(payload)
    plan = _plan(
        core_manifest_sha256=sha256_file(core_manifest),
        reserve_manifest_sha256=sha256_file(reserve_manifest),
        core_reference_sha256=sha256_file(core_reference),
        reserve_reference_sha256=sha256_file(reserve_reference),
    )
    core_reference.chmod(0)
    reserve_reference.chmod(0)
    output = tmp_path / "analysis-plan.json"

    freeze_analysis_plan(
        output,
        plan,
        core_manifest=core_manifest,
        reserve_manifest=reserve_manifest,
        core_reference=core_reference,
        reserve_reference=reserve_reference,
    )

    assert output.exists()
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        freeze_analysis_plan(
            output,
            plan,
            core_manifest=core_manifest,
            reserve_manifest=reserve_manifest,
            core_reference=core_reference,
            reserve_reference=reserve_reference,
        )


def test_freeze_analysis_rejects_reference_that_is_not_sealed(tmp_path: Path) -> None:
    paths = [tmp_path / name for name in ("core", "reserve", "core-ref", "reserve-ref")]
    for path in paths:
        path.write_bytes(path.name.encode())
    plan = _plan(
        core_manifest_sha256=sha256_file(paths[0]),
        reserve_manifest_sha256=sha256_file(paths[1]),
        core_reference_sha256=sha256_file(paths[2]),
        reserve_reference_sha256=sha256_file(paths[3]),
    )
    paths[2].chmod(0)

    with pytest.raises(ValueError, match="both reference manifests must be sealed"):
        freeze_analysis_plan(
            tmp_path / "analysis-plan.json",
            plan,
            core_manifest=paths[0],
            reserve_manifest=paths[1],
            core_reference=paths[2],
            reserve_reference=paths[3],
        )


def test_freeze_formal_analysis_binds_preflight_profiles_duration_and_clusters(
    tmp_path: Path,
) -> None:
    core_manifest = tmp_path / "blind-core.json"
    reserve_manifest = tmp_path / "blind-reserve.json"
    core_reference = tmp_path / "blind-core.references.json"
    reserve_reference = tmp_path / "blind-reserve.references.json"

    def write_split(
        manifest_path: Path,
        reference_path: Path,
        *,
        split: str,
        sample_id: str,
        cluster_id: str,
        duration_ms: int,
    ) -> None:
        manifest = CorpusInputManifest(
            corpus_version="target-v1",
            normalization_version="nfkc-casefold-punct-space-v1",
            split=split,
            samples=(
                CorpusInputSample(
                    sample_id=sample_id,
                    audio_path=f"pcm/{sample_id}.pcm",
                    source_sha256="1" * 64,
                    audio_sha256="2" * 64,
                    duration_ms=duration_ms,
                    session_id=f"session:{sample_id}",
                    analysis_cluster_id=cluster_id,
                    scenario="near-field",
                    language="zh",
                    license_or_consent="authorization:approved",
                ),
            ),
        )
        write_corpus_input_manifest(manifest_path, manifest)
        write_reference_manifest(
            reference_path,
            CorpusReferenceManifest(
                corpus_version="target-v1",
                normalization_version="nfkc-casefold-punct-space-v1",
                split=split,
                input_manifest_sha256=sha256_file(manifest_path),
                samples=(
                    CorpusReference(
                        sample_id=sample_id,
                        reference_raw="测试",
                        reference_normalized="测试",
                    ),
                ),
            ),
        )
        reference_path.chmod(0)

    write_split(
        core_manifest,
        core_reference,
        split="blind-core",
        sample_id="core",
        cluster_id="cluster:core",
        duration_ms=60_000,
    )
    write_split(
        reserve_manifest,
        reserve_reference,
        split="blind-reserve",
        sample_id="reserve",
        cluster_id="cluster:reserve",
        duration_ms=45_000,
    )
    cluster_hash = hashlib.sha256(
        b"cluster:core\ncluster:reserve"
    ).hexdigest()
    preflight_path = tmp_path / "preflight-report.json"
    preflight_path.write_text(
        BlindPreflightReport(
            status="metadata_ready",
            metadata_sha256="3" * 64,
            blockers=(),
            sample_count={"blind-core": 1, "blind-reserve": 1},
            unique_duration_ms={"blind-core": 60_000, "blind-reserve": 45_000},
            scenario_duration_ms={
                "blind-core": {"near-field": 60_000},
                "blind-reserve": {"near-field": 45_000},
            },
            cluster_set_sha256=cluster_hash,
            sample_order_sha256=hashlib.sha256(b"core\nreserve").hexdigest(),
        ).model_dump_json(),
        encoding="utf-8",
    )
    profiles: dict[str, Path] = {}
    for candidate_id in ("qwen", "sense", "fun"):
        profile = tmp_path / f"{candidate_id}.profile.json"
        profile.write_text(f'{{"candidate_id":"{candidate_id}"}}', encoding="utf-8")
        profiles[candidate_id] = profile
    design = AnalysisPlanDesign(
        candidate_ids=("qwen", "sense", "fun"),
        bootstrap_seeds=(2026082501, 2026082502),
        pilot_baseline_cer=0.10,
        primary_endpoints=("macro_cer",),
        normalization_version="nfkc-casefold-punct-space-v1",
        filtering_rules=("retain_all_failures",),
        decision_families=(
            DecisionFamily(
                family_id="meeting",
                baseline_id="qwen",
                candidate_ids=("fun",),
                pilot_baseline_cer=0.10,
            ),
            DecisionFamily(
                family_id="interaction",
                baseline_id="sense",
                candidate_ids=("fun",),
                pilot_baseline_cer=0.12,
            ),
        ),
    )
    output = tmp_path / "analysis-plan-formal.json"

    plan = freeze_formal_analysis_plan(
        output,
        design,
        core_manifest=core_manifest,
        reserve_manifest=reserve_manifest,
        core_reference=core_reference,
        reserve_reference=reserve_reference,
        preflight_report=preflight_path,
        profile_paths=profiles,
    )

    assert plan.evidence_tier == "formal"
    assert plan.core_duration_ms == 60_000
    assert plan.reserve_duration_ms == 45_000
    assert plan.analysis_cluster_ids == ("cluster:core", "cluster:reserve")
    assert plan.core_analysis_cluster_ids == ("cluster:core",)
    assert plan.reserve_analysis_cluster_ids == ("cluster:reserve",)
    assert plan.candidate_profile_sha256 == {
        candidate_id: sha256_file(path) for candidate_id, path in profiles.items()
    }
    assert output.exists()
    assert AnalysisPlan.model_validate_json(output.read_text(encoding="utf-8")) == plan
