"""本地模型快照解析边界测试。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from huggingface_hub.errors import IncompleteSnapshotError

from voice_realtime.model_cache import (
    huggingface_snapshot_path,
    modelscope_snapshot_path,
    resolve_model_snapshot,
)


def test_existing_local_path_never_calls_hub(tmp_path: Path) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()

    with patch("voice_realtime.model_cache.snapshot_download") as download:
        resolved = resolve_model_snapshot(str(model_dir))

    assert resolved == str(model_dir)
    download.assert_not_called()


def test_blank_model_uses_default_repo_in_offline_mode() -> None:
    with patch(
        "voice_realtime.model_cache.snapshot_download",
        return_value="/cache/default",
    ) as download:
        resolved = resolve_model_snapshot("", default_repo="org/default")

    assert resolved == "/cache/default"
    download.assert_called_once_with("org/default", local_files_only=True)


def test_repo_id_uses_cached_snapshot_by_default() -> None:
    with patch(
        "voice_realtime.model_cache.snapshot_download",
        return_value="/cache/custom",
    ) as download:
        resolved = resolve_model_snapshot("org/custom")

    assert resolved == "/cache/custom"
    download.assert_called_once_with("org/custom", local_files_only=True)


def test_incomplete_offline_manifest_returns_existing_snapshot(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshots" / "commit"
    snapshot.mkdir(parents=True)
    error = IncompleteSnapshotError("README.md is missing", str(snapshot))

    with patch(
        "voice_realtime.model_cache.snapshot_download",
        side_effect=error,
    ):
        resolved = resolve_model_snapshot("org/cached-model")

    assert resolved == str(snapshot)


def test_incomplete_snapshot_is_not_accepted_when_downloads_are_allowed(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshots" / "commit"
    snapshot.mkdir(parents=True)
    error = IncompleteSnapshotError("weights are missing", str(snapshot))

    with (
        patch("voice_realtime.model_cache.snapshot_download", side_effect=error),
        pytest.raises(IncompleteSnapshotError),
    ):
        resolve_model_snapshot("org/model", allow_downloads=True)


def test_incomplete_offline_snapshot_requires_existing_directory(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    error = IncompleteSnapshotError("snapshot is missing", str(missing))

    with (
        patch("voice_realtime.model_cache.snapshot_download", side_effect=error),
        pytest.raises(IncompleteSnapshotError),
    ):
        resolve_model_snapshot("org/model")


def test_explicit_download_permission_is_forwarded() -> None:
    with patch(
        "voice_realtime.model_cache.snapshot_download",
        return_value="/cache/downloaded",
    ) as download:
        resolved = resolve_model_snapshot("org/model", allow_downloads=True)

    assert resolved == "/cache/downloaded"
    download.assert_called_once_with("org/model", local_files_only=False)


def test_blank_model_without_default_is_rejected() -> None:
    with pytest.raises(ValueError, match="模型"):
        resolve_model_snapshot("  ")


def test_modelscope_snapshot_path_honors_external_cache_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODELSCOPE_CACHE", str(tmp_path / "modelscope"))

    path = modelscope_snapshot_path("Qwen/Qwen3-ASR-1.7B", revision="master")

    assert path == (
        tmp_path
        / "modelscope/models/Qwen--Qwen3-ASR-1.7B/snapshots/master"
    )


def test_huggingface_snapshot_path_honors_external_cache_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "huggingface" / "hub"))

    path = huggingface_snapshot_path(
        "nvidia/diar_streaming_sortformer_4spk-v2",
        revision="5240a64075176943f677d30fa2171c780229f341",
    )

    assert path == (
        tmp_path
        / "huggingface/hub/models--nvidia--diar_streaming_sortformer_4spk-v2"
        / "snapshots/5240a64075176943f677d30fa2171c780229f341"
    )


@pytest.mark.parametrize("repo_id", ["", "single-part", "../escape", "org/../escape"])
def test_snapshot_path_rejects_unsafe_repo_ids(repo_id: str) -> None:
    with pytest.raises(ValueError, match="repo_id"):
        modelscope_snapshot_path(repo_id)
