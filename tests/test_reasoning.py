"""LmStudioNativeLLMService（LM Studio 原生端点 reasoning 开关）+ 系统提示词测试。

关键背景（QA 实测）：OpenAI 兼容端点忽略 reasoning 参数导致模型始终思考；
本服务走原生 /api/v1/chat 端点（SSE，message.delta 逐字输出）。
核心验证点：payload 构造（input items 转换、无 role 字段、reasoning/stream）
与 SSE → OpenAI 兼容 chunk 的转换（空 delta 过滤）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import EndFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.openai.llm import OpenAILLMService

from voice_realtime.interaction.pipeline import build_system_prompt
from voice_realtime.interaction.reasoning import LmStudioNativeLLMService

SSE_LINES = [
    'data: {"type":"chat.start"}',
    'data: {"type":"message.delta","content":"你"}',
    'data: {"type":"message.delta","content":"好"}',
    'data: {"type":"message.delta","content":""}',
    'data: {"type":"message.complete"}',
]


class FakeSSEResponse:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def raise_for_status(self) -> None:
        pass

    async def aiter_lines(self) -> Any:
        for line in self._lines:
            yield line


def make_context() -> LLMContext:
    return LLMContext(
        messages=[
            {"role": "system", "content": "你是一个中文语音助手"},
            {"role": "user", "content": "你好"},
        ]
    )


class TestLmStudioNativeLLMService:
    def test_reasoning_off_by_default(self) -> None:
        svc = LmStudioNativeLLMService(model="test-model", base_url="http://localhost:1234")
        assert svc._reasoning == "off"

    def test_reasoning_configurable(self) -> None:
        svc = LmStudioNativeLLMService(
            model="test-model", base_url="http://localhost:1234", reasoning="low"
        )
        assert svc._reasoning == "low"

    def test_base_url_normalized_for_native_endpoint(self) -> None:
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234/v1")
        assert str(svc._http.base_url).rstrip("/") == "http://localhost:1234"

    def test_base_url_root_kept_as_is(self) -> None:
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")
        assert str(svc._http.base_url).rstrip("/") == "http://localhost:1234"

    def test_native_client_has_explicit_local_timeouts(self) -> None:
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")
        assert svc._http.timeout.connect == 5
        assert svc._http.timeout.read is None
        assert svc._http.timeout.write == 10
        assert svc._http.timeout.pool == 5

    async def test_native_request_payload_and_sse_conversion(self) -> None:
        svc = LmStudioNativeLLMService(
            model="qwen/qwen3.6-35b-a3b",
            base_url="http://localhost:1234/v1",
            temperature=0.9,
        )
        captured: dict[str, Any] = {}

        @asynccontextmanager
        async def fake_stream(
            method: str, url: str, json: dict[str, Any] | None = None, **_: Any
        ) -> Any:
            captured["method"] = method
            captured["url"] = url
            captured["body"] = json or {}
            yield FakeSSEResponse(SSE_LINES)

        svc._http.stream = fake_stream  # type: ignore[method-assign]
        context = make_context()

        chunks = [chunk async for chunk in await svc.get_chat_completions(context)]

        # payload 构造验证：原生端点请求形状
        payload = captured["body"]
        assert captured["method"] == "POST"
        assert captured["url"].endswith("/api/v1/chat")
        assert payload["model"] == "qwen/qwen3.6-35b-a3b"
        assert payload["reasoning"] == "off"
        assert payload["temperature"] == 0.9
        assert payload["stream"] is True
        # input items：无 role 字段、按序为文本内容
        assert payload["input"] == [
            {"type": "text", "content": "你是一个中文语音助手"},
            {"type": "text", "content": "你好"},
        ]
        # SSE → chunk 转换：空 delta 被过滤
        assert [c.choices[0].delta.content for c in chunks] == ["你", "好"]

    async def test_sse_handles_done_and_malformed_lines(self) -> None:
        """验证 SSE 遇到 [DONE] 正常退出、遇到非 JSON 或空行不崩溃。"""
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")
        lines = [
            ": ping",
            "data: not json",
            'data: {"type":"message.delta","content":"测试"}',
            "data: [DONE]",
            'data: {"type":"message.delta","content":"不会被消费"}',
        ]

        @asynccontextmanager
        async def fake_stream(*_: Any, **__: Any) -> Any:
            yield FakeSSEResponse(lines)

        svc._http.stream = fake_stream  # type: ignore[method-assign]
        chunks = [chunk async for chunk in await svc.get_chat_completions(make_context())]
        assert [c.choices[0].delta.content for c in chunks] == ["测试"]

    async def test_sse_error_event_raises(self) -> None:
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")

        @asynccontextmanager
        async def fake_stream(*_: Any, **__: Any) -> Any:
            yield FakeSSEResponse(['data: {"type":"error","error":"overloaded"}'])

        svc._http.stream = fake_stream  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="reported an error"):
            _ = [chunk async for chunk in await svc.get_chat_completions(make_context())]

    async def test_non_string_delta_content_raises(self) -> None:
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")

        @asynccontextmanager
        async def fake_stream(*_: Any, **__: Any) -> Any:
            yield FakeSSEResponse(['data: {"type":"message.delta","content":42}'])

        svc._http.stream = fake_stream  # type: ignore[method-assign]
        with pytest.raises(TypeError, match="content must be a string"):
            _ = [chunk async for chunk in await svc.get_chat_completions(make_context())]

    async def test_empty_sse_response_raises(self) -> None:
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")

        @asynccontextmanager
        async def fake_stream(*_: Any, **__: Any) -> Any:
            yield FakeSSEResponse(['data: {"type":"message.complete"}'])

        svc._http.stream = fake_stream  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="no message content"):
            _ = [chunk async for chunk in await svc.get_chat_completions(make_context())]

    async def test_close_releases_native_client(self) -> None:
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")
        svc._http.aclose = AsyncMock()
        await svc.close()
        svc._http.aclose.assert_awaited_once()

    async def test_stop_closes_native_client(self) -> None:
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")
        svc.close = AsyncMock()
        with patch.object(OpenAILLMService, "stop", new=AsyncMock()) as parent_stop:
            await svc.stop(EndFrame())
        parent_stop.assert_awaited_once()
        svc.close.assert_awaited_once()


class TestSystemPrompt:
    def test_prompt_enforces_chinese_short_voice_response(self) -> None:
        prompt = build_system_prompt()
        assert "中文" in prompt
        assert "简短" in prompt or "简洁" in prompt
        assert "不要重复" in prompt or "禁止重复" in prompt

    def test_prompt_mentions_voice_assistant_role(self) -> None:
        prompt = build_system_prompt()
        assert "语音助手" in prompt

    def test_custom_persona_appended(self) -> None:
        prompt = build_system_prompt(persona="你是一位耐心的中医养生顾问。")
        assert "中医养生" in prompt
