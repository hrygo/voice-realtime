"""LM Studio 端点与认证 helper 测试。"""

from __future__ import annotations

import pytest

from voice_realtime.lm_studio import (
    DEFAULT_LM_STUDIO_API_KEY,
    lm_studio_auth_headers,
    lm_studio_openai_models_url,
    lm_studio_root_url,
)


def test_lm_studio_default_api_key_is_backward_compatible() -> None:
    assert DEFAULT_LM_STUDIO_API_KEY == "lm-studio"


def test_lm_studio_auth_headers_strip_key_whitespace() -> None:
    assert lm_studio_auth_headers("  test-key  ") == {
        "Authorization": "Bearer test-key"
    }


def test_lm_studio_auth_headers_reject_empty_key() -> None:
    with pytest.raises(ValueError, match="API key"):
        lm_studio_auth_headers("  ")


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://localhost:1234/v1", "http://localhost:1234"),
        ("http://localhost:1234/v1/", "http://localhost:1234"),
        ("http://localhost:1234", "http://localhost:1234"),
    ],
)
def test_lm_studio_root_url_is_normalized(base_url: str, expected: str) -> None:
    assert lm_studio_root_url(base_url) == expected


def test_lm_studio_models_url_keeps_openai_compatible_prefix() -> None:
    assert (
        lm_studio_openai_models_url("http://localhost:1234/v1/")
        == "http://localhost:1234/v1/models"
    )
