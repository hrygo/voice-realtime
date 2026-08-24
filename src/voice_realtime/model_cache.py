"""把模型引用解析为可离线加载的本地路径。"""

from __future__ import annotations

import os
from pathlib import Path

from huggingface_hub import snapshot_download
from huggingface_hub.errors import IncompleteSnapshotError


def _cache_repo_name(repo_id: str) -> str:
    normalized = repo_id.strip()
    parts = normalized.split("/")
    if (
        len(parts) != 2
        or any(not part or part in {".", ".."} for part in parts)
        or "\\" in normalized
    ):
        raise ValueError("repo_id 必须使用安全的 owner/name 格式")
    return "--".join(parts)


def _cache_revision(revision: str) -> str:
    normalized = revision.strip()
    if not normalized or normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
        raise ValueError("revision 必须是安全的单一路径组件")
    return normalized


def modelscope_snapshot_path(repo_id: str, *, revision: str = "master") -> Path:
    """返回 ModelScope 标准外部缓存中的 snapshot 路径。"""
    cache_root = Path(
        os.environ.get("MODELSCOPE_CACHE", Path.home() / ".cache" / "modelscope")
    ).expanduser()
    return (
        cache_root
        / "models"
        / _cache_repo_name(repo_id)
        / "snapshots"
        / _cache_revision(revision)
    )


def huggingface_snapshot_path(repo_id: str, *, revision: str) -> Path:
    """返回 Hugging Face Hub 标准外部缓存中的 snapshot 路径。"""
    configured_hub = os.environ.get("HF_HUB_CACHE")
    if configured_hub:
        hub_root = Path(configured_hub).expanduser()
    else:
        hf_home = Path(
            os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")
        ).expanduser()
        hub_root = hf_home / "hub"
    return (
        hub_root
        / f"models--{_cache_repo_name(repo_id)}"
        / "snapshots"
        / _cache_revision(revision)
    )


def resolve_model_snapshot(
    model: str,
    *,
    default_repo: str | None = None,
    allow_downloads: bool = False,
) -> str:
    """返回本地模型路径；仓库 ID 默认只允许命中现有 HF 缓存。"""
    reference = model.strip() or (default_repo or "").strip()
    if not reference:
        raise ValueError("模型路径或仓库 ID 不能为空")

    local_path = Path(reference).expanduser()
    if local_path.exists():
        return str(local_path)

    try:
        return snapshot_download(reference, local_files_only=not allow_downloads)
    except IncompleteSnapshotError as exc:
        # Hub 的完整性清单也包含 README 等非运行文件；旧缓存可能只有模型必需文件。
        # 严格离线时允许模型加载器继续校验其实际所需文件，绝不因此回退联网。
        snapshot_path = Path(exc.snapshot_path)
        if not allow_downloads and snapshot_path.is_dir():
            return str(snapshot_path)
        raise
