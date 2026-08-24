"""ASR benchmark 运行与语料清单契约测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from pydantic import ValidationError

from voice_realtime.benchmarks.asr.manifest import (
    ASRRunManifest,
    CorpusInputManifest,
    CorpusInputSample,
    CorpusManifest,
    CorpusReference,
    CorpusReferenceManifest,
    CorpusSample,
    EnvironmentIdentity,
    RuntimeIdentity,
    load_corpus_input_manifest,
    load_run_manifest,
    sha256_file,
    verify_file_hashes,
    verify_git_checkout,
    write_corpus_input_manifest,
    write_run_manifest,
)


def _manifest(**updates: object) -> ASRRunManifest:
    values: dict[str, object] = {
        "schema_version": "1.0",
        "run_id": "20260824T120000Z-Q3-WLK-MPS-dev-r1",
        "git_commit": "a" * 40,
        "corpus_manifest_sha256": "b" * 64,
        "reference_manifest_sha256": "c" * 64,
        "backend_id": "wlk-qwen3-streaming",
        "model_id": "Qwen/Qwen3-ASR-1.7B",
        "model_revision": "c" * 40,
        "model_files_sha256": {"config.json": "d" * 64},
        "runtime": RuntimeIdentity(name="WhisperLiveKit", revision="e" * 40),
        "device": "mps",
        "dtype": "float16",
        "parameters": {"chunk_sec": 2.0},
        "environment": EnvironmentIdentity(
            host="Apple M5 Max",
            memory_bytes=128 * 1024**3,
            macos="26.6.2",
            python="3.12.14",
            torch="2.13.0",
        ),
        "started_at": datetime(2026, 8, 24, 12, tzinfo=UTC),
        "status": "planned",
    }
    values.update(updates)
    return ASRRunManifest.model_validate(values)


def test_manifest_round_trip_preserves_required_identity(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    expected = _manifest()

    write_run_manifest(path, expected)

    assert load_run_manifest(path) == expected


@pytest.mark.parametrize(
    "field",
    [
        "git_commit",
        "corpus_manifest_sha256",
        "reference_manifest_sha256",
        "model_files_sha256",
        "device",
    ],
)
def test_manifest_rejects_missing_reproducibility_identity(field: str) -> None:
    values = _manifest().model_dump()
    values.pop(field)

    with pytest.raises(ValidationError):
        ASRRunManifest.model_validate(values)


def test_manifest_rejects_invalid_hash_and_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        _manifest(git_commit="main")

    with pytest.raises(ValidationError):
        _manifest(started_at=datetime(2026, 8, 24, 12))


def test_corpus_manifest_rejects_duplicate_sample_ids() -> None:
    sample = CorpusSample(
        sample_id="sample-001",
        audio_path="external/sample-001.pcm",
        audio_sha256="f" * 64,
        duration_ms=20,
        scenario="near-field",
        language="zh",
        reference_raw="你好",
        reference_normalized="你好",
        license_or_consent="consented",
    )

    with pytest.raises(ValidationError, match="sample_id"):
        CorpusManifest(
            schema_version="1.0",
            corpus_version="dev-v1",
            normalization_version="nfkc-casefold-punct-space-v1",
            samples=(sample, sample),
        )


def test_blind_input_manifest_round_trip_contains_no_reference(tmp_path: Path) -> None:
    sample = CorpusInputSample(
        sample_id="blind-001",
        audio_path="pcm/blind-001.pcm",
        source_sha256="a" * 64,
        audio_sha256="b" * 64,
        duration_ms=20,
        session_id="session-001",
        scenario="near-field",
        language="zh",
        license_or_consent="consent-001",
        speakers=("speaker-001",),
        source_id="source-001",
        content_group_id="content-001",
        start_frame=16_000,
        end_frame=16_320,
        channel_index=0,
    )
    manifest = CorpusInputManifest(
        corpus_version="blind-core-v1",
        normalization_version="nfkc-casefold-punct-space-v1",
        split="blind-core",
        samples=(sample,),
    )
    path = tmp_path / "blind-core.json"

    write_corpus_input_manifest(path, manifest)

    assert load_corpus_input_manifest(path) == manifest
    assert manifest.samples[0].content_group_id == "content-001"
    assert "reference" not in path.read_text(encoding="utf-8")

    payload = manifest.model_dump(mode="json")
    payload["samples"][0]["reference_raw"] = "不得进入 runner"
    with pytest.raises(ValidationError, match="Extra inputs"):
        CorpusInputManifest.model_validate(payload)


def test_negative_input_sample_may_have_no_speaker() -> None:
    sample = CorpusInputSample(
        sample_id="silence-001",
        audio_path="pcm/silence-001.pcm",
        source_sha256="a" * 64,
        audio_sha256="b" * 64,
        duration_ms=3000,
        session_id="negative-session",
        scenario="silence-negative",
        language="zh",
        license_or_consent="generated digital silence",
        tags=("negative", "silence"),
    )

    assert sample.speakers == ()


def test_reference_manifest_binds_input_hash_and_rejects_duplicate_ids() -> None:
    reference = CorpusReference(
        sample_id="blind-001",
        reference_raw="你好",
        reference_normalized="你好",
    )
    with pytest.raises(ValidationError, match="input_manifest_sha256"):
        CorpusReferenceManifest.model_validate(
            {
                "corpus_version": "blind-v1",
                "normalization_version": "nfkc-casefold-punct-space-v1",
                "split": "blind-core",
                "samples": [reference.model_dump(mode="json")],
            }
        )
    with pytest.raises(ValidationError, match="sample_id"):
        CorpusReferenceManifest(
            corpus_version="blind-v1",
            normalization_version="nfkc-casefold-punct-space-v1",
            split="blind-core",
            input_manifest_sha256="a" * 64,
            samples=(reference, reference),
        )


def test_sha256_file_hashes_exact_bytes(tmp_path: Path) -> None:
    path = tmp_path / "sample.pcm"
    path.write_bytes(b"\x00\x01\x02\x03")

    assert sha256_file(path) == (
        "054edec1d0211f624fed0cbca9d4f9400b0e491c43742af2c5b0abebf0c990d8"
    )


def test_verify_model_files_rejects_hash_mismatch_and_path_escape(tmp_path: Path) -> None:
    model_root = tmp_path / "model"
    model_root.mkdir()
    (model_root / "config.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="SHA-256"):
        verify_file_hashes(model_root, {"config.json": "f" * 64})

    with pytest.raises(ValueError, match="relative"):
        verify_file_hashes(model_root, {"../outside": "f" * 64})


def test_verify_model_files_allows_only_same_hf_repository_blob_symlink(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "models--publisher--model"
    snapshot = repository / "snapshots" / "revision"
    blobs = repository / "blobs"
    snapshot.mkdir(parents=True)
    blobs.mkdir()
    blob = blobs / "model-blob"
    blob.write_bytes(b"model")
    (snapshot / "model.bin").symlink_to(Path("../../blobs/model-blob"))

    verify_file_hashes(snapshot, {"model.bin": sha256_file(blob)})

    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    (snapshot / "outside.bin").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        verify_file_hashes(snapshot, {"outside.bin": sha256_file(outside)})


def test_verify_git_checkout_rejects_dirty_worktree(monkeypatch, tmp_path: Path) -> None:
    outputs = iter(("a" * 40 + "\n", " M src/example.py\n"))

    def fake_run(*_args, **_kwargs) -> CompletedProcess[str]:
        return CompletedProcess(args=[], returncode=0, stdout=next(outputs), stderr="")

    monkeypatch.setattr("voice_realtime.benchmarks.asr.manifest.subprocess.run", fake_run)

    with pytest.raises(ValueError, match="clean"):
        verify_git_checkout(tmp_path, "a" * 40)
