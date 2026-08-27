"""LM Studio endpoint and authentication helpers shared by all consumers."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from voice_realtime.network import local_async_client

DEFAULT_LM_STUDIO_API_KEY = "lm-studio"
LM_STUDIO_NATIVE_CHAT_PATH = "/api/v1/chat"
Reasoning = Literal["off", "low", "medium", "high", "on"]


@dataclass(frozen=True, slots=True)
class NativeChatRequest:
    """受控的原生 Chat 请求；模式层只通过此类型选择可选参数。"""

    model: str
    input: str
    system_prompt: str | None = None
    reasoning: Reasoning = "off"
    stream: bool = True
    store: bool = False
    previous_response_id: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    repeat_penalty: float | None = None
    max_output_tokens: int | None = None
    context_length: int | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "input": self.input,
            "stream": self.stream,
            "store": self.store,
            "reasoning": self.reasoning,
        }
        optional = {
            "system_prompt": self.system_prompt,
            "previous_response_id": self.previous_response_id,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "min_p": self.min_p,
            "repeat_penalty": self.repeat_penalty,
            "max_output_tokens": self.max_output_tokens,
            "context_length": self.context_length,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        return payload


@dataclass(frozen=True, slots=True)
class NativeChatEvent:
    """原生 SSE 事件；content/result 原样保留供上层策略校验。"""

    type: str
    content: str | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class LMStudioClient:
    """LM Studio 原生 v1 REST 传输层，不承担任何业务提示或答案语义。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        http_client: httpx.AsyncClient | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self._base_url = lm_studio_root_url(base_url)
        self._http = http_client or local_async_client(
            base_url=self._base_url,
            headers=lm_studio_auth_headers(api_key),
            timeout=timeout or httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
        )
        self._owns_http = http_client is None

    async def stream_chat(self, request: NativeChatRequest) -> AsyncIterator[NativeChatEvent]:
        async with self.stream_request(request.to_payload()) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line.removeprefix("data:").strip()
                if raw == "[DONE]":
                    return
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(value, dict) or not isinstance(value.get("type"), str):
                    raise ValueError("LM Studio SSE event must be an object with type")
                yield NativeChatEvent(
                    type=value["type"],
                    content=value.get("content") if isinstance(value.get("content"), str) else None,
                    result=value.get("result") if isinstance(value.get("result"), dict) else None,
                    error=value.get("error") if isinstance(value.get("error"), dict) else None,
                )

    @asynccontextmanager
    async def stream_request(self, payload: dict[str, Any]) -> AsyncIterator[httpx.Response]:
        """Expose the native request lifecycle for legacy response parsers."""
        async with self._http.stream(
            "POST", self._base_url + LM_STUDIO_NATIVE_CHAT_PATH, json=payload
        ) as response:
            yield response

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()


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
