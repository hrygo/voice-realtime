"""网络与 HTTP 客户端工具层。"""

from __future__ import annotations

import contextlib
import socket
import subprocess
import sys
from ipaddress import ip_address
from typing import Any

import httpx


def local_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """创建不继承系统代理的客户端，防止本机请求被转发到代理端口。"""
    return httpx.AsyncClient(trust_env=False, **kwargs)


def get_lan_ip() -> str:
    """获取本机当前活动的局域网 IP 地址。

    优先使用无包 UDP socket 探测出网路由；若离线则尝试系统工具和主机名；
    最终兜底返回 '127.0.0.1'。
    """
    # 1. 优先使用 UDP socket 获取路由出网源 IP（无实际网络发包）
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 80))
            ip = str(s.getsockname()[0])
            if ip and not ip_address(ip).is_loopback:
                return ip
    except Exception:
        pass

    # 2. macOS 专用: 查询默认路由接口或常见网卡
    if sys.platform == "darwin":
        try:
            route_proc = subprocess.run(
                ["route", "-n", "get", "default"],
                capture_output=True,
                text=True,
                check=False,
            )
            default_if = ""
            for line in route_proc.stdout.splitlines():
                if "interface:" in line:
                    default_if = line.split(":", 1)[1].strip()
                    break
            interfaces = [default_if] if default_if else ["en0", "en1", "bridge0", "en2", "en3"]
            for iface in interfaces:
                if not iface:
                    continue
                ip_proc = subprocess.run(
                    ["ipconfig", "getifaddr", iface],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                candidate = ip_proc.stdout.strip()
                if candidate:
                    with contextlib.suppress(ValueError):
                        if not ip_address(candidate).is_loopback:
                            return candidate
        except Exception:
            pass

    # 3. Linux / 通用主机名解析
    try:
        host_ip = socket.gethostbyname(socket.gethostname())
        if host_ip and not ip_address(host_ip).is_loopback:
            return host_ip
    except Exception:
        pass

    return "127.0.0.1"

