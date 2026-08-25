#!/usr/bin/env python3
"""基于已开封 Public Proxy v1 pilot 生成 v2 的 10,000 次功效模拟。"""

from __future__ import annotations

import argparse
import json
import os
import random
from pathlib import Path
from statistics import NormalDist

from voice_realtime.benchmarks.asr.analysis_plan import PowerSimulationArtifact

_ITERATIONS = 10_000
_SEED = 20_260_825
_PILOT_CLUSTER_VARIANCE = 0.0003
_CORE_CLUSTERS = 14
_FINAL_CLUSTERS = 28
_PILOT_BASELINE_CER = {"meeting": 0.1011, "interaction": 0.1369}


def _reject(mean: float, *, variance: float, clusters: int, alpha: float) -> bool:
    standard_error = (variance / clusters) ** 0.5
    critical = NormalDist().inv_cdf(1 - alpha / 2)
    return mean + critical * standard_error < 0


def simulate() -> tuple[PowerSimulationArtifact, dict[str, object]]:
    generator = random.Random(_SEED)
    standard_deviation = _PILOT_CLUSTER_VARIANCE**0.5
    family_core_power: dict[str, float] = {}
    family_final_power: dict[str, float] = {}
    for family, baseline_cer in _PILOT_BASELINE_CER.items():
        effect = -0.05 * baseline_cer
        core_rejections = 0
        final_rejections = 0
        for _ in range(_ITERATIONS):
            values = [
                generator.gauss(effect, standard_deviation)
                for _ in range(_FINAL_CLUSTERS)
            ]
            core_mean = sum(values[:_CORE_CLUSTERS]) / _CORE_CLUSTERS
            final_mean = sum(values) / _FINAL_CLUSTERS
            core_rejected = _reject(
                core_mean,
                variance=_PILOT_CLUSTER_VARIANCE,
                clusters=_CORE_CLUSTERS,
                alpha=0.01,
            )
            final_rejected = _reject(
                final_mean,
                variance=_PILOT_CLUSTER_VARIANCE,
                clusters=_FINAL_CLUSTERS,
                alpha=0.04,
            )
            core_rejections += core_rejected
            final_rejections += core_rejected or final_rejected
        family_core_power[family] = core_rejections / _ITERATIONS
        family_final_power[family] = final_rejections / _ITERATIONS

    null_false_rejections = 0
    for _ in range(_ITERATIONS):
        values = [
            generator.gauss(0.0, standard_deviation)
            for _ in range(_FINAL_CLUSTERS)
        ]
        core_mean = sum(values[:_CORE_CLUSTERS]) / _CORE_CLUSTERS
        final_mean = sum(values) / _FINAL_CLUSTERS
        null_false_rejections += _reject(
            core_mean,
            variance=_PILOT_CLUSTER_VARIANCE,
            clusters=_CORE_CLUSTERS,
            alpha=0.01,
        ) or _reject(
            final_mean,
            variance=_PILOT_CLUSTER_VARIANCE,
            clusters=_FINAL_CLUSTERS,
            alpha=0.04,
        )
    simulated_alpha = null_false_rejections / _ITERATIONS
    artifact = PowerSimulationArtifact(
        iterations=_ITERATIONS,
        pilot_cluster_variance=_PILOT_CLUSTER_VARIANCE,
        core_power=min(family_core_power.values()),
        final_power=min(family_final_power.values()),
        simulated_familywise_alpha=simulated_alpha,
    )
    provenance: dict[str, object] = {
        "schema_version": "1.0",
        "seed": _SEED,
        "pilot_source": "public-proxy-v1-20260825 Core comparisons",
        "pilot_baseline_cer": _PILOT_BASELINE_CER,
        "pilot_cluster_variance": _PILOT_CLUSTER_VARIANCE,
        "variance_basis": (
            "conservative round-up from the wider pilot 95% cluster-bootstrap CI; "
            "approximate variance 0.000259"
        ),
        "assumed_true_effect": "5% relative CER improvement versus superiority null 0",
        "core_clusters": _CORE_CLUSTERS,
        "final_clusters": _FINAL_CLUSTERS,
        "look_alpha": [0.01, 0.04],
        "family_core_power": family_core_power,
        "family_final_cumulative_power": family_final_power,
        "reported_power_rule": "minimum across decision families",
        "simulated_familywise_alpha": simulated_alpha,
    }
    return artifact, provenance


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.output.exists() or args.provenance.exists():
        raise FileExistsError("power simulation output already exists")
    artifact, provenance = simulate()
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.output.write_text(artifact.model_dump_json(indent=2) + "\n", encoding="utf-8")
    args.output.chmod(0o600)
    args.provenance.write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    args.provenance.chmod(0o600)
    print(artifact.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
