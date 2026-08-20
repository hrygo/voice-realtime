"""本机 HTTP 客户端不得继承系统代理。"""

from __future__ import annotations

import httpx

from voice_realtime.network import local_async_client


async def test_local_async_client_ignores_environment_proxies() -> None:
    client = local_async_client(timeout=1.0)
    try:
        assert client._mounts == {}
    finally:
        await client.aclose()


async def test_local_async_client_is_a_real_httpx_client() -> None:
    client = local_async_client()
    try:
        assert isinstance(client, httpx.AsyncClient)
    finally:
        await client.aclose()
