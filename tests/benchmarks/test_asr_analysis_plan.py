"""ASR Core/Reserve 序贯分析计划冻结测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from voice_realtime.benchmarks.asr.analysis_plan import (
    AnalysisPlan,
    freeze_analysis_plan,
)
from voice_realtime.benchmarks.asr.manifest import sha256_file


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
