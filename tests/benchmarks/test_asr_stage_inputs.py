"""冻结 Stage 输入解析与重新核验测试。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from voice_realtime.benchmarks.asr.stage_contracts import (
    InteractionAssetBinding,
    InteractionScriptBinding,
    PCMInputBinding,
    ScheduleManifest,
    ScheduleSegment,
    StageInputManifest,
)
from voice_realtime.benchmarks.asr.stage_inputs import (
    StageInputError,
    canonical_json_bytes,
    load_stage_input_manifest,
    resolve_stage_inputs,
    verify_resolved_input,
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _pcm_fixture(
    root: Path,
    *,
    pcm: bytes,
    duration_ms: int,
    stage: int = 2,
    family_id: str = "meeting",
    input_name: str = "sample.pcm",
) -> tuple[ScheduleManifest, str, StageInputManifest, Path]:
    input_root = root / "external-input"
    input_root.mkdir(parents=True)
    pcm_path = input_root / input_name
    pcm_path.write_bytes(pcm)
    input_hash = _sha256_bytes(pcm)
    segment_id = "screen-001"
    schedule = ScheduleManifest(
        stage=stage,  # type: ignore[arg-type]
        family_id=family_id,
        segments=(
            ScheduleSegment(
                segment_id=segment_id,
                purpose="screen",
                input_sha256=input_hash,
                duration_ms=duration_ms,
                repetition=1,
            ),
        ),
    )
    schedule_hash = "a" * 64
    manifest = StageInputManifest(
        schedule_sha256=schedule_hash,
        bindings=(
            PCMInputBinding(
                segment_id=segment_id,
                relative_path=input_name,
                input_sha256=input_hash,
                size_bytes=len(pcm),
                duration_ms=duration_ms,
            ),
        ),
    )
    return schedule, schedule_hash, manifest, input_root


def _interaction_fixture(
    root: Path,
) -> tuple[Path, ScheduleManifest, str, StageInputManifest, Path]:
    input_root = root / "external-interaction"
    input_root.mkdir(parents=True)
    pcm = b"\x00\x00" * 16_000
    asset_path = input_root / "utterance-1.pcm"
    asset_path.write_bytes(pcm)
    payload = {
        "actions": [
            {
                "at_cursor_ms": 0,
                "asset_id": "utterance-1",
                "duration_ms": 1_000,
                "kind": "feed_pcm",
            }
        ]
    }
    script_bytes = canonical_json_bytes(payload)
    script_path = input_root / "turn-001.json"
    script_path.write_bytes(script_bytes)
    script_hash = _sha256_bytes(script_bytes)
    schedule = ScheduleManifest(
        stage=4,
        family_id="interaction",
        segments=(
            ScheduleSegment(
                segment_id="screen-turn-001",
                purpose="screen",
                input_sha256=script_hash,
                duration_ms=1_000,
                repetition=1,
            ),
        ),
    )
    schedule_hash = "c" * 64
    manifest = StageInputManifest(
        schedule_sha256=schedule_hash,
        bindings=(
            InteractionScriptBinding(
                segment_id="screen-turn-001",
                relative_path="turn-001.json",
                input_sha256=script_hash,
                size_bytes=len(script_bytes),
                duration_ms=1_000,
                assets=(
                    InteractionAssetBinding(
                        asset_id="utterance-1",
                        relative_path="utterance-1.pcm",
                        input_sha256=_sha256_bytes(pcm),
                        size_bytes=len(pcm),
                        duration_ms=1_000,
                    ),
                ),
            ),
        ),
    )
    return script_path, schedule, schedule_hash, manifest, input_root


def test_resolve_pcm_rechecks_hash_size_and_duration(tmp_path: Path) -> None:
    pcm = b"\x00\x00" * 16_000
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule, schedule_hash, manifest, input_root = _pcm_fixture(
        tmp_path, pcm=pcm, duration_ms=1_000
    )

    resolved = resolve_stage_inputs(
        schedule=schedule,
        schedule_sha256=schedule_hash,
        manifest=manifest,
        input_root=input_root,
        repository_root=repo,
        evidence_tier="formal",
    )

    assert resolved[0].duration_ms == 1_000
    (input_root / "sample.pcm").write_bytes(pcm + b"\x00\x00")
    with pytest.raises(StageInputError, match="changed after resolution"):
        verify_resolved_input(resolved[0])


def test_formal_input_rejects_repository_and_symlink(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule, schedule_hash, manifest, input_root = _pcm_fixture(
        repo, pcm=b"\0\0" * 16_000, duration_ms=1_000
    )
    with pytest.raises(StageInputError, match="outside the repository"):
        resolve_stage_inputs(
            schedule, schedule_hash, manifest, input_root, repo, "formal"
        )

    external_root = tmp_path / "symlink-input"
    external_root.mkdir()
    target = tmp_path / "real.pcm"
    target.write_bytes(b"\0\0" * 16_000)
    link = external_root / "sample.pcm"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    symlink_schedule, symlink_hash, symlink_manifest, _ = _pcm_fixture(
        tmp_path / "symlink-fixture",
        pcm=b"\0\0" * 16_000,
        duration_ms=1_000,
    )
    # Replace the fixture path with a deliberately symlinked binding while
    # retaining a hash/size contract for the target bytes.
    symlink_manifest = StageInputManifest(
        schedule_sha256=symlink_hash,
        bindings=(
            PCMInputBinding(
                segment_id="screen-001",
                relative_path="sample.pcm",
                input_sha256=_sha256_bytes(target.read_bytes()),
                size_bytes=target.stat().st_size,
                duration_ms=1_000,
            ),
        ),
    )
    symlink_schedule = ScheduleManifest(
        stage=2,
        family_id="meeting",
        segments=(
            ScheduleSegment(
                segment_id="screen-001",
                purpose="screen",
                input_sha256=_sha256_bytes(target.read_bytes()),
                duration_ms=1_000,
                repetition=1,
            ),
        ),
    )
    with pytest.raises(StageInputError, match="symlink"):
        resolve_stage_inputs(
            symlink_schedule,
            symlink_hash,
            symlink_manifest,
            external_root,
            repo,
            "formal",
        )


def test_input_root_symlink_is_rejected_even_when_target_is_external(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    fixture_root = tmp_path / "fixture"
    schedule, schedule_hash, manifest, real_input_root = _pcm_fixture(
        fixture_root,
        pcm=b"\0\0" * 16_000,
        duration_ms=1_000,
    )
    input_root_link = tmp_path / "input-root-link"
    try:
        input_root_link.symlink_to(real_input_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    with pytest.raises(StageInputError, match="symlink"):
        resolve_stage_inputs(
            schedule,
            schedule_hash,
            manifest,
            input_root_link,
            repo,
            "formal",
        )


def test_experimental_input_may_live_inside_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule, schedule_hash, manifest, input_root = _pcm_fixture(
        repo, pcm=b"\0\0" * 16_000, duration_ms=1_000
    )
    resolved = resolve_stage_inputs(
        schedule,
        schedule_hash,
        manifest,
        input_root,
        repo,
        "experimental",
    )
    assert resolved[0].size_bytes == 32_000


def test_schedule_hash_and_segment_bindings_are_exact(tmp_path: Path) -> None:
    pcm = b"\0\0" * 16_000
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule, schedule_hash, manifest, input_root = _pcm_fixture(
        tmp_path, pcm=pcm, duration_ms=1_000
    )
    with pytest.raises(StageInputError, match="schedule SHA-256 mismatch"):
        resolve_stage_inputs(
            schedule, "b" * 64, manifest, input_root, repo, "formal"
        )

    missing = StageInputManifest(
        schedule_sha256=schedule_hash,
        bindings=(
            PCMInputBinding(
                segment_id="other-segment",
                relative_path="sample.pcm",
                input_sha256=_sha256_bytes(pcm),
                size_bytes=len(pcm),
                duration_ms=1_000,
            ),
        ),
    )
    with pytest.raises(StageInputError, match="exactly match schedule segments"):
        resolve_stage_inputs(
            schedule, schedule_hash, missing, input_root, repo, "formal"
        )


def test_non_regular_input_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    input_root = tmp_path / "external-input"
    input_root.mkdir()
    fifo = input_root / "sample.pcm"
    try:
        os.mkfifo(fifo)
    except (AttributeError, NotImplementedError, OSError) as exc:
        pytest.skip(f"FIFO unavailable: {exc}")
    digest = _sha256_bytes(b"\0\0" * 16_000)
    schedule = ScheduleManifest(
        stage=2,
        family_id="meeting",
        segments=(
            ScheduleSegment(
                segment_id="screen-001",
                purpose="screen",
                input_sha256=digest,
                duration_ms=1_000,
                repetition=1,
            ),
        ),
    )
    manifest = StageInputManifest(
        schedule_sha256="a" * 64,
        bindings=(
            PCMInputBinding(
                segment_id="screen-001",
                relative_path="sample.pcm",
                input_sha256=digest,
                size_bytes=32_000,
                duration_ms=1_000,
            ),
        ),
    )
    with pytest.raises(StageInputError, match="regular file"):
        resolve_stage_inputs(
            schedule, "a" * 64, manifest, input_root, repo, "formal"
        )


def test_pcm_slice_bytes_and_frame_boundaries(tmp_path: Path) -> None:
    pcm = bytes(range(256)) * 4  # 1,024 bytes = 32 ms at 16 kHz s16le.
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule, schedule_hash, manifest, input_root = _pcm_fixture(
        tmp_path, pcm=pcm, duration_ms=32
    )
    resolved = resolve_stage_inputs(
        schedule, schedule_hash, manifest, input_root, repo, "formal"
    )[0]

    assert resolved.slice_bytes(start_offset_ms=1, end_offset_ms=3) == pcm[32:96]
    assert b"".join(resolved.iter_frames(start_offset_ms=1, end_offset_ms=3)) == pcm[32:96]
    assert list(resolved.iter_frames(start_offset_ms=0, end_offset_ms=32)) == [
        pcm[:640],
        pcm[640:],
    ]
    with pytest.raises(StageInputError, match="outside the resolved input"):
        resolved.slice_bytes(start_offset_ms=31, end_offset_ms=33)
    with pytest.raises(StageInputError, match="outside the resolved input"):
        tuple(resolved.iter_frames(start_offset_ms=4, end_offset_ms=3))


def test_pcm_rejects_unaligned_bytes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    pcm = b"\0" * 33
    input_root = tmp_path / "external-input"
    input_root.mkdir()
    (input_root / "sample.pcm").write_bytes(pcm)
    digest = _sha256_bytes(pcm)
    schedule = ScheduleManifest(
        stage=2,
        family_id="meeting",
        segments=(
            ScheduleSegment(
                segment_id="screen-001",
                purpose="screen",
                input_sha256=digest,
                duration_ms=1,
                repetition=1,
            ),
        ),
    )
    manifest = StageInputManifest(
        schedule_sha256="a" * 64,
        bindings=(
            PCMInputBinding(
                segment_id="screen-001",
                relative_path="sample.pcm",
                input_sha256=digest,
                size_bytes=len(pcm),
                duration_ms=1,
            ),
        ),
    )
    with pytest.raises(StageInputError, match="aligned"):
        resolve_stage_inputs(
            schedule, "a" * 64, manifest, input_root, repo, "formal"
        )


def test_pcm_rejects_aligned_bytes_with_wrong_duration(tmp_path: Path) -> None:
    pcm = b"\0\0" * 16_000
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule, schedule_hash, manifest, input_root = _pcm_fixture(
        tmp_path,
        pcm=pcm,
        duration_ms=999,
    )
    assert len(pcm) == 32_000
    assert manifest.bindings[0].size_bytes == 32_000
    with pytest.raises(StageInputError, match="frozen binding"):
        resolve_stage_inputs(
            schedule, schedule_hash, manifest, input_root, repo, "formal"
        )


def test_interaction_script_must_equal_canonical_json_bytes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    script_path, schedule, schedule_hash, manifest, input_root = _interaction_fixture(tmp_path)
    script_path.write_text('{ "actions": [] }\n', encoding="utf-8")
    with pytest.raises(StageInputError, match="canonical JSON"):
        resolve_stage_inputs(
            schedule, schedule_hash, manifest, input_root, repo, "formal"
        )


def test_interaction_resolves_actions_and_assets_then_rechecks_both(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    script_path, schedule, schedule_hash, manifest, input_root = _interaction_fixture(tmp_path)
    resolved = resolve_stage_inputs(
        schedule, schedule_hash, manifest, input_root, repo, "formal"
    )[0]
    assert resolved.segment_id == "screen-turn-001"
    assert resolved.actions[0].asset_id == "utterance-1"
    assert resolved.assets["utterance-1"].duration_ms == 1_000
    verify_resolved_input(resolved)

    asset_path = input_root / "utterance-1.pcm"
    asset_path.write_bytes(b"\x01\x00" * 16_000)
    with pytest.raises(StageInputError, match="changed after resolution"):
        verify_resolved_input(resolved)

    # Restore the asset, then alter the script while preserving a valid JSON
    # shape; verification must use the frozen script digest, not only parsing.
    asset_path.write_bytes(b"\x00\x00" * 16_000)
    script_path.write_bytes(
        canonical_json_bytes(
            {
                "actions": [
                    {
                        "at_cursor_ms": 1,
                        "asset_id": "utterance-1",
                        "duration_ms": 1_000,
                        "kind": "feed_pcm",
                    }
                ]
            }
        )
    )
    with pytest.raises(StageInputError, match="changed after resolution"):
        verify_resolved_input(resolved)


def test_interaction_rejects_non_monotonic_actions(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    script_path, schedule, schedule_hash, manifest, input_root = _interaction_fixture(tmp_path)
    script_path.write_bytes(
        canonical_json_bytes(
            {
                "actions": [
                    {
                        "at_cursor_ms": 2,
                        "asset_id": "utterance-1",
                        "duration_ms": 1,
                        "kind": "feed_pcm",
                    },
                    {
                        "at_cursor_ms": 1,
                        "asset_id": "utterance-1",
                        "duration_ms": 1,
                        "kind": "barge_in",
                    },
                ]
            }
        )
    )
    with pytest.raises(StageInputError, match="monotonic"):
        resolve_stage_inputs(
            schedule, schedule_hash, manifest, input_root, repo, "formal"
        )


def test_interaction_rejects_unknown_asset_with_fresh_hashes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    script_path, schedule, schedule_hash, manifest, input_root = _interaction_fixture(tmp_path)
    payload = {
        "actions": [
            {
                "at_cursor_ms": 0,
                "asset_id": "missing-asset",
                "duration_ms": 1_000,
                "kind": "feed_pcm",
            }
        ]
    }
    script_bytes = canonical_json_bytes(payload)
    script_path.write_bytes(script_bytes)
    script_hash = _sha256_bytes(script_bytes)
    original_binding = manifest.bindings[0]
    assert isinstance(original_binding, InteractionScriptBinding)
    fresh_manifest = StageInputManifest(
        schedule_sha256=schedule_hash,
        bindings=(
            InteractionScriptBinding(
                segment_id=original_binding.segment_id,
                relative_path=original_binding.relative_path,
                input_sha256=script_hash,
                size_bytes=len(script_bytes),
                duration_ms=original_binding.duration_ms,
                assets=original_binding.assets,
            ),
        ),
    )
    fresh_schedule = ScheduleManifest(
        stage=schedule.stage,
        family_id=schedule.family_id,
        segments=(
            ScheduleSegment(
                segment_id=schedule.segments[0].segment_id,
                purpose=schedule.segments[0].purpose,
                input_sha256=script_hash,
                duration_ms=schedule.segments[0].duration_ms,
                repetition=schedule.segments[0].repetition,
            ),
        ),
    )
    with pytest.raises(StageInputError, match="unknown PCM asset"):
        resolve_stage_inputs(
            fresh_schedule,
            schedule_hash,
            fresh_manifest,
            input_root,
            repo,
            "formal",
        )


def test_load_stage_input_manifest_validates_json_boundary(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": "1.0",
                "schedule_sha256": "a" * 64,
                "bindings": [
                    {
                        "kind": "pcm",
                        "segment_id": "screen-001",
                        "relative_path": "sample.pcm",
                        "input_sha256": "b" * 64,
                        "size_bytes": 32_000,
                        "duration_ms": 1_000,
                        "sample_rate_hz": 16_000,
                        "channels": 1,
                        "sample_format": "s16le",
                    }
                ],
            }
        )
    )
    manifest = load_stage_input_manifest(manifest_path)
    assert manifest.schedule_sha256 == "a" * 64

    symlink = tmp_path / "manifest-link.json"
    try:
        symlink.symlink_to(manifest_path)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    with pytest.raises(StageInputError, match="symlink"):
        load_stage_input_manifest(symlink)


def test_canonical_json_is_utf8_sorted_compact_and_newline_terminated() -> None:
    payload = {"z": "中文", "a": [2, 1]}
    assert canonical_json_bytes(payload) == (
        '{"a":[2,1],"z":"中文"}\n'.encode()
    )


def test_verify_rejects_replaced_symlink_after_resolution(tmp_path: Path) -> None:
    pcm = b"\0\0" * 16_000
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule, schedule_hash, manifest, input_root = _pcm_fixture(
        tmp_path, pcm=pcm, duration_ms=1_000
    )
    resolved = resolve_stage_inputs(
        schedule, schedule_hash, manifest, input_root, repo, "formal"
    )[0]
    replacement = tmp_path / "replacement.pcm"
    replacement.write_bytes(pcm)
    path = input_root / "sample.pcm"
    path.unlink()
    try:
        path.symlink_to(replacement)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    with pytest.raises(StageInputError, match=r"changed after resolution|symlink"):
        verify_resolved_input(resolved)


def test_resolve_does_not_read_unbound_files(tmp_path: Path) -> None:
    pcm = b"\0\0" * 16_000
    repo = tmp_path / "repo"
    repo.mkdir()
    schedule, schedule_hash, manifest, input_root = _pcm_fixture(
        tmp_path, pcm=pcm, duration_ms=1_000
    )
    (input_root / "ignored-reference.txt").write_text("must not be read", encoding="utf-8")
    resolved = resolve_stage_inputs(
        schedule, schedule_hash, manifest, input_root, repo, "formal"
    )
    assert len(resolved) == 1
