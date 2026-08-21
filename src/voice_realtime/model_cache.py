"""把模型引用解析为可离线加载的本地路径。"""

from __future__ import annotations

from pathlib import Path

from huggingface_hub import snapshot_download
from huggingface_hub.errors import IncompleteSnapshotError


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
