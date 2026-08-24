"""ASR benchmark CLI 的稳定命令契约测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voice_realtime.asr.contracts import ASRSessionContext
from voice_realtime.asr.profiles import FunASRNanoWSProfile
from voice_realtime.benchmarks.asr.cli import (
    _build_streaming_registry,
    _loopback_service_url,
    _require_external_model_dir,
    build_parser,
    main,
)
from voice_realtime.benchmarks.asr.replay import load_hypotheses


def _write_hypotheses(run_dir: Path, values: list[tuple[str, float]]) -> None:
    run_dir.mkdir()
    rows = [
        {
            "sample_id": sample_id,
            "scenario": "near-field",
            "reference_raw": "参考",
            "reference_normalized": "参考",
            "hypothesis_raw": "结果",
            "hypothesis_normalized": "结果",
            "language": "zh",
            "duration_ms": 1000,
            "S": 1,
            "D": 0,
            "I": 0,
            "N": 2,
            "cer_status": "supported",
            "cer": value,
            "wall_time_ms": 100,
            "rtf": 0.1,
            "deadline_misses": 0,
            "error_status": None,
        }
        for sample_id, value in values
    ]
    (run_dir / "hypotheses.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_parser_exposes_run_score_compare_subcommands() -> None:
    parser = build_parser()

    assert parser.parse_args(["score", "--run-dir", "run"]).command == "score"
    assert parser.parse_args(
        ["compare", "--baseline", "a", "--candidate", "b", "--output", "out.json"]
    ).command == "compare"


def test_run_target_must_be_loopback() -> None:
    assert _loopback_service_url("127.0.0.1", 8001) == "ws://127.0.0.1:8001"
    assert _loopback_service_url("::1", 8001) == "ws://[::1]:8001"
    with pytest.raises(ValueError, match="loopback"):
        _loopback_service_url("example.com", 8001)


def test_run_registry_selects_funasr_nano_adapter_from_discriminated_profile() -> None:
    profile = FunASRNanoWSProfile(
        model_dir="/model-cache/FunAudioLLM--Fun-ASR-Nano-2512/snapshots/master",
        language="中文",
        host="127.0.0.1",
        port=10095,
    )

    registry = _build_streaming_registry(profile, "ws://127.0.0.1:10095")
    backend = registry.create_streaming(
        profile,
        ASRSessionContext(source_epoch=1, offset_ms=0, purpose="subtitles"),
    )

    assert backend.backend_id == "funasr-nano-ws"
    assert backend.uri == "ws://127.0.0.1:10095"


def test_benchmark_rejects_model_directory_inside_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    internal_model = repository / "runtime" / "model"
    external_model = tmp_path / "model-cache" / "model"
    internal_model.mkdir(parents=True)
    external_model.mkdir(parents=True)

    with pytest.raises(ValueError, match="outside the repository"):
        _require_external_model_dir(internal_model, repository)

    assert _require_external_model_dir(external_model, repository) == external_model


def test_score_command_writes_summary(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_hypotheses(run_dir, [("a", 0.5), ("b", 0.0)])

    exit_code = main(["score", "--run-dir", str(run_dir)])

    summary = json.loads((run_dir / "summary.json").read_text())
    assert exit_code == 0
    assert summary["samples"] == 2
    assert summary["macro_cer"] == 0.25


def test_compare_command_uses_paired_sample_ids(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    output = tmp_path / "comparison.json"
    _write_hypotheses(baseline, [("a", 0.3), ("b", 0.4)])
    _write_hypotheses(candidate, [("a", 0.2), ("b", 0.3), ("extra", 0.1)])

    exit_code = main(
        [
            "compare",
            "--baseline",
            str(baseline),
            "--candidate",
            str(candidate),
            "--output",
            str(output),
            "--bootstrap-iterations",
            "100",
        ]
    )

    comparison = json.loads(output.read_text())
    assert exit_code == 0
    assert comparison["paired_samples"] == 2
    assert comparison["mean_cer_difference"] == -0.1


def test_hypothesis_reader_rejects_duplicate_sample_ids(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_hypotheses(run_dir, [("duplicate", 0.1), ("duplicate", 0.2)])

    with pytest.raises(ValueError, match="duplicate sample_id"):
        load_hypotheses(run_dir / "hypotheses.jsonl")


def test_hypothesis_reader_rejects_inconsistent_metric_state(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_hypotheses(run_dir, [("broken", 0.1)])
    path = run_dir / "hypotheses.jsonl"
    row = json.loads(path.read_text())
    row["cer"] = None
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="supported CER"):
        load_hypotheses(path)
