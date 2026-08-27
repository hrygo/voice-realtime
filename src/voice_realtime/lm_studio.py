"""LM Studio endpoint and authentication helpers shared by all consumers."""

from __future__ import annotations

DEFAULT_LM_STUDIO_API_KEY = "lm-studio"
LM_STUDIO_NATIVE_CHAT_PATH = "/api/v1/chat"


def lm_studio_auth_headers(api_key: str) -> dict[str, str]:
    """Build the local LM Studio authentication header without logging the key."""
    normalized = api_key.strip()
    if not normalized:
        raise ValueError("LM Studio API key 不能为空")
    return {"Authorization": f"Bearer {normalized}"}


def lm_studio_root_url(base_url: str) -> str:
    """Normalize an OpenAI-compatible base URL to the LM Studio root URL."""
    root_url = base_url.rstrip("/")
    if root_url.endswith("/v1"):
        return root_url[: -len("/v1")]
    return root_url


def lm_studio_openai_models_url(base_url: str) -> str:
    """Build the OpenAI-compatible models endpoint used by health checks."""
    return base_url.rstrip("/") + "/models"
