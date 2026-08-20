"""仅用于 localhost 服务调用的 HTTP 客户端工厂。"""

from __future__ import annotations

from typing import Any

import httpx


def local_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """创建不继承系统代理的客户端，防止本机请求被转发到代理端口。"""
    return httpx.AsyncClient(trust_env=False, **kwargs)
