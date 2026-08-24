"""外部 ASR 语料制备与盲测切分测试。"""

from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from pydantic import ValidationError

from voice_realtime.benchmarks.asr.corpus import (
    CorpusPreparationSpec,
    CorpusSourceSample,
    prepare_corpus,
    quota_summary,
)
from voice_realtime.benchmarks.asr.manifest import (
    load_corpus_input_manifest,
    load_reference_manifest,
    sha256_file,
)


def _sample(
    sample_id: str,
    *,
    split: str,
    duration_ms: int,
    source_path: str | None = None,
    session_id: str | None = None,
    speakers: tuple[str, ...] = (),
    tags: tuple[str, ...] = ("near-field", "entity"),
    license_or_consent: str = "consent-001",
    source_id: str | None = None,
    content_group_id: str | None = None,
    start_frame: int | None = None,
    end_frame: int | None = None,
    channel_index: int | None = None,
) -> CorpusSourceSample:
    return CorpusSourceSample.model_validate(
        {
            "sample_id": sample_id,
            "split": split,
            "source_path": source_path or f"audio/{sample_id}.wav",
            "expected_duration_ms": duration_ms,
            "session_id": session_id or f"session-{sample_id}",
            "scenario": "near-field",
            "language": "zh",
            "reference_raw": "你好，开放时间。",
            "license_or_consent": license_or_consent,
            "source_id": source_id or f"source-{sample_id}",
            "content_group_id": content_group_id or f"content-{sample_id}",
            "start_frame": start_frame,
            "end_frame": end_frame,
            "channel_index": channel_index,
            "speakers": speakers or (f"speaker-{sample_id}",),
            "tags": tags,
        }
    )


def _spec(samples: tuple[CorpusSourceSample, ...]) -> CorpusPreparationSpec:
    return CorpusPreparationSpec(
        corpus_version="target-domain-v1",
        samples=samples,
        required_duration_ms={"blind-core": 60_000, "blind-reserve": 45_000},
        required_scenario_duration_ms={
            "blind-core": {"near-field": 60_000},
            "blind-reserve": {"near-field": 45_000},
        },
        minimum_blind_speakers=2,
        minimum_speakers_per_look=1,
        required_tags={
            "blind-core": ("near-field", "entity"),
            "blind-reserve": ("near-field", "entity"),
        },
    )


def test_source_sample_rejects_missing_license_and_parent_path() -> None:
    with pytest.raises(ValidationError, match="license_or_consent"):
        _sample(
            "missing-license",
            split="dev",
            duration_ms=20,
            license_or_consent="",
        )
    with pytest.raises(ValidationError, match="source_path"):
        _sample(
            "escape",
            split="dev",
            duration_ms=20,
            source_path="../outside.wav",
        )
    with pytest.raises(ValidationError, match="sample_id"):
        _sample("../escape", split="dev", duration_ms=20)


def test_spec_rejects_duplicate_sample_ids_and_cross_look_cluster_leakage() -> None:
    duplicate = _sample("same", split="blind-core", duration_ms=60_000)
    with pytest.raises(ValidationError, match="sample_id"):
        _spec((duplicate, duplicate))

    core = _sample(
        "core",
        split="blind-core",
        duration_ms=60_000,
        session_id="shared-session",
        speakers=("core-speaker",),
    )
    reserve = _sample(
        "reserve",
        split="blind-reserve",
        duration_ms=45_000,
        session_id="shared-session",
        speakers=("reserve-speaker",),
    )
    with pytest.raises(ValidationError, match="session"):
        _spec((core, reserve))

    reserve = reserve.model_copy(
        update={"session_id": "reserve-session", "speakers": ("core-speaker",)}
    )
    with pytest.raises(ValidationError, match="speaker"):
        _spec((core, reserve))

    reserve = reserve.model_copy(
        update={
            "session_id": "reserve-session",
            "speakers": ("reserve-speaker",),
            "content_group_id": core.content_group_id,
        }
    )
    with pytest.raises(ValidationError, match="content group"):
        _spec((core, reserve))


def test_source_sample_requires_frame_exact_segment_boundaries() -> None:
    segmented = _sample(
        "segment",
        split="dev",
        duration_ms=2_000,
        start_frame=16_000,
        end_frame=48_000,
        channel_index=3,
    )

    assert segmented.start_frame == 16_000
    assert segmented.end_frame == 48_000

    with pytest.raises(ValidationError, match="frame segment"):
        _sample(
            "mismatch",
            split="dev",
            duration_ms=2_001,
            start_frame=16_000,
            end_frame=48_000,
        )


def test_quota_summary_counts_unique_audio_once_across_orthogonal_tags() -> None:
    samples = (
        _sample("a", split="blind-core", duration_ms=20_000),
        _sample("b", split="blind-core", duration_ms=40_000),
        _sample("c", split="blind-reserve", duration_ms=45_000),
    )

    summary = quota_summary(_spec(samples))

    assert summary["blind-core"]["unique_duration_ms"] == 60_000
    assert summary["blind-core"]["sample_count"] == 2
    assert summary["blind-core"]["tag_duration_ms"] == {
        "entity": 60_000,
        "near-field": 60_000,
    }
    assert summary["blind-reserve"]["unique_duration_ms"] == 45_000
    assert summary["blind-core"]["scenario_duration_ms"] == {"near-field": 60_000}


def test_spec_rejects_wrong_primary_scenario_quota_even_when_tags_exist() -> None:
    core = _sample("core", split="blind-core", duration_ms=60_000)
    reserve = _sample("reserve", split="blind-reserve", duration_ms=45_000)

    with pytest.raises(ValidationError, match="scenario duration"):
        CorpusPreparationSpec(
            corpus_version="target-domain-v1",
            samples=(core, reserve),
            required_duration_ms={"blind-core": 60_000, "blind-reserve": 45_000},
            required_scenario_duration_ms={
                "blind-core": {"meeting": 60_000},
                "blind-reserve": {"near-field": 45_000},
            },
            minimum_blind_speakers=2,
            minimum_speakers_per_look=1,
            required_tags={
                "blind-core": ("near-field", "entity"),
                "blind-reserve": ("near-field", "entity"),
            },
        )


def test_prepare_corpus_converts_once_and_freezes_separate_blind_references(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "audio").mkdir()
    wav = source_root / "audio" / "core.wav"
    flac = source_root / "audio" / "reserve.flac"
    wav.write_bytes(b"RIFF-source")
    flac.write_bytes(b"fLaC-source")
    spec = _spec(
        (
            _sample("core", split="blind-core", duration_ms=60_000),
            _sample(
                "reserve",
                split="blind-reserve",
                duration_ms=45_000,
                source_path="audio/reserve.flac",
            ),
        )
    )
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> CompletedProcess[bytes]:
        assert kwargs["check"] is True
        assert kwargs["timeout"] == 120
        assert "shell" not in kwargs
        assert argv[:4] == ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel"]
        calls.append(argv)
        expected_ms = 60_000 if any(item.endswith("core.wav") for item in argv) else 45_000
        Path(argv[-1]).write_bytes(b"\x00\x00" * 16 * expected_ms)
        return CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr("voice_realtime.benchmarks.asr.corpus.subprocess.run", fake_run)
    output_root = tmp_path / "external-corpus"

    bundle = prepare_corpus(
        spec=spec,
        source_root=source_root,
        output_root=output_root,
        repository_root=tmp_path / "repository",
    )

    assert len(calls) == 2
    assert all(argv[-3:-1] == ["-f", "s16le"] for argv in calls)
    core = load_corpus_input_manifest(output_root / "blind-core.json")
    reserve = load_corpus_input_manifest(output_root / "blind-reserve.json")
    assert core.samples[0].duration_ms == 60_000
    assert reserve.samples[0].duration_ms == 45_000
    assert core.samples[0].source_sha256 == sha256_file(wav)
    assert core.samples[0].audio_sha256 == sha256_file(
        output_root / core.samples[0].audio_path
    )
    serialized = (output_root / "blind-core.json").read_text(encoding="utf-8")
    assert "reference" not in serialized

    core_reference_path = output_root / "sealed" / "blind-core.references.json"
    reserve_reference_path = output_root / "sealed" / "blind-reserve.references.json"
    assert core_reference_path.stat().st_mode & 0o777 == 0
    assert reserve_reference_path.stat().st_mode & 0o777 == 0
    core_reference_path.chmod(0o600)
    reserve_reference_path.chmod(0o600)
    assert load_reference_manifest(core_reference_path).samples[0].reference_raw
    checksums = json.loads((output_root / "checksums.json").read_text())
    assert checksums["blind-core.json"] == sha256_file(output_root / "blind-core.json")
    assert checksums["blind-reserve.json"] == sha256_file(
        output_root / "blind-reserve.json"
    )
    assert bundle.core_manifest_sha256 != bundle.reserve_manifest_sha256


def test_prepare_corpus_uses_fixed_frame_and_channel_filter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "sources"
    (source_root / "audio").mkdir(parents=True)
    (source_root / "audio" / "core.wav").write_bytes(b"RIFF-source")
    (source_root / "audio" / "reserve.wav").write_bytes(b"RIFF-source")
    core = _sample(
        "core",
        split="blind-core",
        duration_ms=60_000,
        start_frame=160_000,
        end_frame=1_120_000,
        channel_index=2,
    )
    reserve = _sample("reserve", split="blind-reserve", duration_ms=45_000)
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> CompletedProcess[bytes]:
        calls.append(argv)
        expected_ms = (
            60_000
            if any(
                "atrim=start_sample=160000:end_sample=1120000" in item
                for item in argv
            )
            else 45_000
        )
        Path(argv[-1]).write_bytes(b"\x00\x00" * 16 * expected_ms)
        return CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr("voice_realtime.benchmarks.asr.corpus.subprocess.run", fake_run)

    prepare_corpus(
        spec=_spec((core, reserve)),
        source_root=source_root,
        output_root=tmp_path / "out",
        repository_root=tmp_path / "repo",
    )

    assert "-af" in calls[0]
    audio_filter = calls[0][calls[0].index("-af") + 1]
    assert audio_filter == (
        "pan=mono|c0=c2,atrim=start_sample=160000:end_sample=1120000,"
        "asetpts=PTS-STARTPTS"
    )


def test_prepare_corpus_rejects_symlink_escape_existing_output_and_repo_output(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"outside")
    (source_root / "audio").mkdir()
    (source_root / "audio" / "core.wav").symlink_to(outside)
    spec = _spec(
        (
            _sample("core", split="blind-core", duration_ms=60_000),
            _sample("reserve", split="blind-reserve", duration_ms=45_000),
        )
    )
    (source_root / "audio" / "reserve.wav").write_bytes(b"source")

    with pytest.raises(ValueError, match="inside its declared root"):
        prepare_corpus(
            spec=spec,
            source_root=source_root,
            output_root=tmp_path / "out",
            repository_root=tmp_path / "repo",
        )

    repository = tmp_path / "repo"
    repository.mkdir()
    output = repository / "corpus"
    with pytest.raises(ValueError, match="outside the repository"):
        prepare_corpus(
            spec=spec,
            source_root=source_root,
            output_root=output,
            repository_root=repository,
        )

    output.mkdir(parents=True)
    with pytest.raises(FileExistsError):
        prepare_corpus(
            spec=spec,
            source_root=source_root,
            output_root=output,
            repository_root=tmp_path / "other-repo",
        )
