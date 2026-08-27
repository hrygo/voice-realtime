"""LM Studio endpoint and authentication helpers shared by all consumers."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, cast

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

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> NativeChatRequest:
        """Build the controlled request type from an existing native payload."""
        return cls(
            model=str(payload["model"]),
            input=str(payload.get("input", "")),
            system_prompt=_optional_str(payload.get("system_prompt")),
            reasoning=cast(Reasoning, str(payload.get("reasoning", "off"))),
            stream=bool(payload.get("stream", True)),
            store=bool(payload.get("store", False)),
            previous_response_id=_optional_str(payload.get("previous_response_id")),
            temperature=_optional_float(payload.get("temperature")),
            top_p=_optional_float(payload.get("top_p")),
            top_k=_optional_int(payload.get("top_k")),
            min_p=_optional_float(payload.get("min_p")),
            repeat_penalty=_optional_float(payload.get("repeat_penalty")),
            max_output_tokens=_optional_int(payload.get("max_output_tokens")),
            context_length=_optional_int(payload.get("context_length")),
        )

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
    message: str | None = None


@dataclass(frozen=True, slots=True)
class NativeChatCompletion:
    """已完成的原生 Chat 调用；屏蔽 SSE 事件布局与终态细节。"""

    text: str
    response_id: str | None
    stats: dict[str, Any]


class LMStudioProtocolError(RuntimeError):
    """LM Studio 返回了不完整或无法解释的原生协议流。"""

    def __init__(self, message: str, *, code: str = "invalid_protocol") -> None:
        super().__init__(message)
        self.code = code


class LMStudioResponseError(RuntimeError):
    """LM Studio 通过原生 SSE error 事件拒绝了请求。"""

    def __init__(self, message: str, *, code: str = "lm_studio_error") -> None:
        super().__init__(message)
        self.code = code


class LMStudioOutputLimitError(RuntimeError):
    """客户端输出字符熔断被触发。"""


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
            raise_for_status = getattr(response, "raise_for_status", None)
            if raise_for_status is not None:
                raise_for_status()
            elif getattr(response, "status_code", 200) not in {200, None}:
                failed_request = getattr(response, "request", None)
                if not isinstance(failed_request, httpx.Request):
                    failed_request = httpx.Request(
                        "POST", self._base_url + LM_STUDIO_NATIVE_CHAT_PATH
                    )
                raise httpx.HTTPStatusError(
                    "LM Studio request failed",
                    request=failed_request,
                    response=response,
                )
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
                    raise LMStudioProtocolError(
                        "LM Studio SSE event must be an object with type",
                        code="invalid_event",
                    )
                yield NativeChatEvent(
                    type=value["type"],
                    content=value.get("content") if isinstance(value.get("content"), str) else None,
                    result=value.get("result") if isinstance(value.get("result"), dict) else None,
                    error=value.get("error") if isinstance(value.get("error"), dict) else None,
                    message=(
                        value.get("message") if isinstance(value.get("message"), str) else None
                    ),
                )

    async def complete_chat(
        self,
        request: NativeChatRequest,
        *,
        max_output_chars: int | None = None,
        on_text_delta: Callable[[str], Awaitable[None] | None] | None = None,
        require_chat_end: bool = True,
    ) -> NativeChatCompletion:
        """消费一次完整原生流并返回稳定结果，不向模式层泄漏终态布局。"""
        if max_output_chars is not None and max_output_chars <= 0:
            raise ValueError("max_output_chars must be positive")
        parts: list[str] = []
        output_chars = 0
        terminal_result: Mapping[str, Any] | None = None
        async for event in self.stream_chat(request):
            if event.type == "error" or event.type.endswith(".error"):
                error = event.error or {}
                code_value = error.get("code")
                message_value = error.get("message")
                code = (
                    code_value
                    if isinstance(code_value, str) and code_value
                    else "lm_studio_error"
                )
                message = (
                    message_value
                    if isinstance(message_value, str) and message_value
                    else event.message or "LM Studio request failed"
                )
                raise LMStudioResponseError(message, code=code)
            if event.type == "message.delta":
                if event.content is None:
                    raise LMStudioProtocolError(
                        "LM Studio message.delta content must be text",
                        code="invalid_delta",
                    )
                if event.content:
                    output_chars += len(event.content)
                    if max_output_chars is not None and output_chars > max_output_chars:
                        raise LMStudioOutputLimitError(
                            "LM Studio output exceeded the configured character limit"
                        )
                    parts.append(event.content)
                    if on_text_delta is not None:
                        callback_result = on_text_delta(event.content)
                        if callback_result is not None:
                            await callback_result
                continue
            if event.type == "chat.end":
                terminal_result = event.result or {}
                break
        if terminal_result is None and require_chat_end:
            raise LMStudioProtocolError(
                "LM Studio stream ended without chat.end", code="missing_terminal"
            )
        if terminal_result is None:
            terminal_result = {}
        if not parts:
            fallback = _native_result_output_text(terminal_result)
            if fallback:
                if max_output_chars is not None and len(fallback) > max_output_chars:
                    raise LMStudioOutputLimitError(
                        "LM Studio output exceeded the configured character limit"
                    )
                parts.append(fallback)
        response_id_value = terminal_result.get("response_id")
        stats_value = terminal_result.get("stats")
        return NativeChatCompletion(
            text="".join(parts),
            response_id=(
                response_id_value
                if isinstance(response_id_value, str) and response_id_value
                else None
            ),
            stats=dict(stats_value) if isinstance(stats_value, Mapping) else {},
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


def _native_result_output_text(result: Mapping[str, Any]) -> str:
    output = result.get("output")
    if not isinstance(output, list):
        return ""
    parts: list[str] = []
    for item in output:
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        content = item.get("content")
        if isinstance(content, str):
            parts.append(content)
    return "".join(parts)


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _optional_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


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
