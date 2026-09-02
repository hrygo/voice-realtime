import socket
import subprocess
from ipaddress import ip_address

import httpx
import pytest

from sona.network import get_lan_ip, local_async_client


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


def test_get_lan_ip_returns_valid_ip() -> None:
    ip = get_lan_ip()
    assert isinstance(ip, str)
    parsed = ip_address(ip)
    assert parsed.version == 4


def test_get_lan_ip_socket_failure_falls_back_to_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    def _mock_socket(*args: object, **kwargs: object) -> object:
        raise OSError("Network is unreachable")

    def _mock_run(
        cmd: list[str], *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if "route" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="interface: en0\n", stderr="")
        if "ipconfig" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="192.168.1.99\n", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr(socket, "socket", _mock_socket)
    monkeypatch.setattr(subprocess, "run", _mock_run)
    monkeypatch.setattr("sys.platform", "darwin")

    assert get_lan_ip() == "192.168.1.99"


def test_get_lan_ip_complete_failure_returns_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    def _mock_socket(*args: object, **kwargs: object) -> object:
        raise OSError("Network is unreachable")

    def _mock_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, stdout="", stderr="")

    def _mock_gethostbyname(*args: object, **kwargs: object) -> str:
        raise socket.gaierror("Hostname lookup failure")

    monkeypatch.setattr(socket, "socket", _mock_socket)
    monkeypatch.setattr(subprocess, "run", _mock_run)
    monkeypatch.setattr(socket, "gethostbyname", _mock_gethostbyname)

    assert get_lan_ip() == "127.0.0.1"

