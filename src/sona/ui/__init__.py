"""Sona Web 控制台子包。"""

from __future__ import annotations

from typing import Any

__all__ = ["create_app", "main"]


def __getattr__(name: str) -> Any:
    """延迟导出入口，避免协议模型导入时反向装配整个 UI 运行时。"""
    if name in __all__:
        from sona.ui import server

        return getattr(server, name)
    raise AttributeError(name)
