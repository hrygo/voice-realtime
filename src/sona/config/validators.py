"""配置通用校验器与常量定义。"""

from __future__ import annotations

import re
from ipaddress import ip_address
from urllib.parse import urlsplit

TTS_OUTPUT_SAMPLE_RATE = 24000  # Qwen3-TTS 原生输出采样率
SPEECHRAIL_TTS_MODEL = "speechrail/qwen3-tts"
SPEECHRAIL_TTS_VOICE_IDS = frozenset({"default", "warm", "bright", "calm"})
SPEECHRAIL_TTS_VOICE_ALIASES = {"alloy": "default"}
ALLOWED_STT_LANGUAGES = frozenset({"zh", "yue", "en", "ja", "ko"})


def normalize_speechrail_tts_voice(value: str) -> str:
    """归一化受控的公共音色预设及其临时别名。"""
    normalized = value.strip().lower()
    normalized = SPEECHRAIL_TTS_VOICE_ALIASES.get(normalized, normalized)
    if normalized not in SPEECHRAIL_TTS_VOICE_IDS:
        raise ValueError(f"不支持的 TTS 音色: {value}")
    return normalized


def validate_listen_host(value: str) -> str:
    """校验并解析监听地址（支持 0.0.0.0、localhost、lan 别名、私网 IP 及合法主机名）。"""
    host = value.strip().removeprefix("[").removesuffix("]")
    lower = host.lower()
    if lower in {"lan", "lan_ip", "local_network"}:
        from sona.network import get_lan_ip

        return get_lan_ip()
    if lower in {"localhost", "local", "loopback"}:
        return host
    try:
        ip_address(host)
        return host
    except ValueError:
        if re.fullmatch(r"[a-zA-Z0-9.\-_]+", host):
            return host
        raise ValueError(f"监听地址无效: {value}") from None


def validate_service_url(value: str) -> str:
    """校验 HTTP/HTTPS 服务 URL 格式与监听地址。"""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError(f"服务 URL 无效: {value}")
    validate_listen_host(parsed.hostname)
    return value.rstrip("/")
