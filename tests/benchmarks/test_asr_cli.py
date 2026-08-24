"""ASR benchmark CLI 的稳定命令契约测试。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import voice_realtime.benchmarks.asr.cli as cli_module
from voice_realtime.asr.contracts import ASRSessionContext
from voice_realtime.asr.profiles import FunASRNanoPyTorchProfile, FunASRNanoWSProfile
from voice_realtime.benchmarks.asr.cli import (
    _build_streaming_registry,
    _loopback_service_url,
    _open_reference_manifest,
    _require_compatible_mode,
    _require_external_model_dir,
    _run_command,
    _sample_profile,
    _verify_pytorch_run_identity,
    _verify_replay_identity,
    build_parser,
    main,
)
from voice_realtime.benchmarks.asr.manifest import (
    ASRRunManifest,
    CorpusReference,
    CorpusReferenceManifest,
    EnvironmentIdentity,
    RuntimeIdentity,
    sha256_file,
    write_reference_manifest,
    write_run_manifest,
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
    payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    (run_dir / "hypotheses.jsonl").write_text(payload, encoding="utf-8")
    (run_dir / "scored-hypotheses.jsonl").write_text(payload, encoding="utf-8")


def test_parser_exposes_run_score_compare_subcommands() -> None:
    parser = build_parser()

    assert parser.parse_args(
        ["score", "--run-dir", "run", "--references", "references.json"]
    ).command == "score"
    assert parser.parse_args(
        [
            "prepare-corpus",
            "--spec",
            "spec.json",
            "--source-root",
            "source",
            "--output-root",
            "output",
        ]
    ).command == "prepare-corpus"
    assert parser.parse_args(
        ["compare", "--baseline", "a", "--candidate", "b", "--output", "out.json"]
    ).command == "compare"


def test_run_parser_exposes_resource_lock_controls() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "run",
            "--manifest",
            "run.json",
            "--corpus",
            "corpus.json",
            "--corpus-root",
            "corpus",
            "--profile",
            "profile.json",
            "--resource-lock",
            "/tmp/asr-test.lock",
            "--lock-timeout-secs",
            "1.5",
        ]
    )

    assert args.resource_lock == "/tmp/asr-test.lock"
    assert args.lock_timeout_secs == 1.5


def test_replay_timing_must_match_frozen_manifest() -> None:
    parameters = {"chunk_ms": 20, "final_timeout_secs": 120.0}

    _verify_replay_identity(parameters, chunk_ms=20, final_timeout_secs=120.0)
    with pytest.raises(ValueError, match="chunk_ms"):
        _verify_replay_identity(parameters, chunk_ms=40, final_timeout_secs=120.0)
    with pytest.raises(ValueError, match="final_timeout_secs"):
        _verify_replay_identity(parameters, chunk_ms=20, final_timeout_secs=8.0)


def test_run_command_holds_resource_lock_around_full_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    lock_path = tmp_path / "experiment.lock"

    @contextmanager
    def fake_lock(
        path: Path | None,
        *,
        timeout_secs: float,
        run_id: str | None,
    ) -> Iterator[None]:
        assert path == lock_path
        assert timeout_secs == 2.5
        assert run_id == "frozen-run"
        events.append("lock-enter")
        yield
        events.append("lock-exit")

    def fake_locked_run(args: object) -> int:
        del args
        events.append("run")
        return 0

    monkeypatch.setattr(cli_module, "exclusive_resource_lock", fake_lock)
    monkeypatch.setattr(cli_module, "_run_command_locked", fake_locked_run)
    args = SimpleNamespace(
        resource_lock=str(lock_path),
        lock_timeout_secs=2.5,
        manifest="frozen-run.json",
    )

    assert _run_command(args) == 0
    assert events == ["lock-enter", "run", "lock-exit"]


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


def test_run_registry_selects_shared_funasr_pytorch_engine_without_service_url() -> None:
    profile = FunASRNanoPyTorchProfile(
        model_dir="/model-cache/FunAudioLLM--Fun-ASR-Nano-2512/snapshots/master",
        language="中文",
        device="mps",
    )

    def engine(
        audio: object,
        *,
        language: str,
        hotwords: tuple[str, ...],
        itn: bool,
    ) -> object:
        del audio, language, hotwords, itn
        return [{"text": "结果"}]

    registry = _build_streaming_registry(profile, pytorch_engine=engine)
    backend = registry.create_streaming(
        profile,
        ASRSessionContext(source_epoch=1, offset_ms=0, purpose="subtitles"),
    )

    assert backend.backend_id == "funasr-nano-pytorch"
    assert backend.uri == "offline://funasr-nano-pytorch"


def test_pytorch_profile_requires_offline_mode() -> None:
    profile = FunASRNanoPyTorchProfile(
        model_dir="/model-cache/FunAudioLLM--Fun-ASR-Nano-2512/snapshots/master",
        language="中文",
        device="cpu",
    )

    _require_compatible_mode(profile, "offline")
    with pytest.raises(ValueError, match="offline"):
        _require_compatible_mode(profile, "realtime-1x")


def test_pytorch_profile_must_match_frozen_manifest_identity() -> None:
    profile = FunASRNanoPyTorchProfile(
        model_dir="/model-cache/FunAudioLLM--Fun-ASR-Nano-2512/snapshots/master",
        language="中文",
        language_source="corpus",
        device="mps",
        hotwords=("开放时间",),
        itn=True,
        ncpu=4,
    )
    parameters = {
        "language": "中文",
        "language_source": "corpus",
        "hotwords": ["开放时间"],
        "itn": True,
        "ncpu": 4,
        "chunk_ms": 20,
    }

    _verify_pytorch_run_identity(profile, device="mps", parameters=parameters)
    with pytest.raises(ValueError, match="device"):
        _verify_pytorch_run_identity(profile, device="cpu", parameters=parameters)
    with pytest.raises(ValueError, match="hotwords"):
        _verify_pytorch_run_identity(
            profile,
            device="mps",
            parameters={**parameters, "hotwords": []},
        )


def test_pytorch_profile_can_take_language_from_frozen_corpus_sample() -> None:
    profile = FunASRNanoPyTorchProfile(
        model_dir="/model-cache/FunAudioLLM--Fun-ASR-Nano-2512/snapshots/master",
        language="中文",
        language_source="corpus",
        device="mps",
    )

    effective = _sample_profile(profile, SimpleNamespace(language="英文"))

    assert isinstance(effective, FunASRNanoPyTorchProfile)
    assert effective.language == "英文"
    assert effective.language_source == "corpus"


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
    run_dir.mkdir()
    input_hash = "a" * 64
    blind_rows = [
        {
            "sample_id": sample_id,
            "scenario": "near-field",
            "hypothesis_raw": hypothesis,
            "hypothesis_normalized": hypothesis,
            "language": "zh",
            "duration_ms": 1000,
            "wall_time_ms": 100,
            "rtf": 0.1,
            "deadline_misses": 0,
            "finalization_latency_ms": 10,
            "error_status": None,
        }
        for sample_id, hypothesis in (("a", "参错"), ("b", "参考"))
    ]
    (run_dir / "hypotheses.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in blind_rows),
        encoding="utf-8",
    )
    reference_path = tmp_path / "references.json"
    write_reference_manifest(
        reference_path,
        CorpusReferenceManifest(
            corpus_version="test-v1",
            normalization_version="nfkc-casefold-punct-space-v1",
            split="dev",
            input_manifest_sha256=input_hash,
            samples=tuple(
                CorpusReference(
                    sample_id=sample_id,
                    reference_raw="参考",
                    reference_normalized="参考",
                )
                for sample_id in ("a", "b")
            ),
        ),
    )
    reference_hash = sha256_file(reference_path)
    reference_path.chmod(0)
    write_run_manifest(
        run_dir / "manifest.json",
        ASRRunManifest(
            run_id="score-test",
            git_commit="1" * 40,
            corpus_manifest_sha256=input_hash,
            reference_manifest_sha256=reference_hash,
            backend_id="test-backend",
            model_id="test/model",
            model_revision="revision",
            model_files_sha256={"model.bin": "2" * 64},
            runtime=RuntimeIdentity(name="test", revision="revision"),
            device="cpu",
            dtype="float32",
            parameters={},
            environment=EnvironmentIdentity(
                host="test",
                memory_bytes=1,
                macos="test",
                python="3.12",
                torch="test",
            ),
            started_at=datetime(2026, 8, 25, tzinfo=UTC),
            status="completed",
        ),
    )

    exit_code = main(
        [
            "score",
            "--run-dir",
            str(run_dir),
            "--references",
            str(reference_path),
        ]
    )

    summary = json.loads((run_dir / "scored-summary.json").read_text())
    assert exit_code == 0
    assert summary["samples"] == 2
    assert summary["macro_cer"] == 0.25
    assert (run_dir / "scored-hypotheses.jsonl").exists()
    assert "reference_raw" not in blind_rows[0]

    opened_hash, opened = _open_reference_manifest(reference_path)
    assert opened_hash == reference_hash
    assert opened.input_manifest_sha256 == input_hash
    reference_path.chmod(0o644)
    with pytest.raises(ValueError, match="sealed 000 or opened 0600"):
        _open_reference_manifest(reference_path)


def test_compare_command_uses_paired_sample_ids(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    output = tmp_path / "comparison.json"
    _write_hypotheses(baseline, [("a", 0.3), ("b", 0.4)])
    _write_hypotheses(candidate, [("a", 0.2), ("b", 0.3)])

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


def test_compare_rejects_selective_sample_intersection(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _write_hypotheses(baseline, [("a", 0.3), ("b", 0.4)])
    _write_hypotheses(candidate, [("a", 0.2)])

    exit_code = main(
        [
            "compare",
            "--baseline",
            str(baseline),
            "--candidate",
            str(candidate),
            "--output",
            str(tmp_path / "out.json"),
        ]
    )

    assert exit_code == 2


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
