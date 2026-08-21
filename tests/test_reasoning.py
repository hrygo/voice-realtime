"""LmStudioNativeLLMService（LM Studio 原生端点 reasoning 开关）+ 系统提示词测试。

关键背景（QA 实测）：OpenAI 兼容端点忽略 reasoning 参数导致模型始终思考；
本服务走原生 /api/v1/chat 端点（SSE，message.delta 逐字输出）。
核心验证点：首轮 system_prompt/current input、后续 previous_response_id/current input、
chat.end 原子提交，以及 SSE → OpenAI 兼容 chunk 的转换。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pipecat.frames.frames import EndFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.openai.llm import OpenAILLMService

from voice_realtime.interaction.pipeline import build_system_prompt
from voice_realtime.interaction.reasoning import (
    LmStudioNativeLLMService,
    NativeChatStats,
)

SSE_STATS = {
    "input_tokens": 6100,
    "total_output_tokens": 12,
    "reasoning_output_tokens": 0,
    "time_to_first_token_seconds": 1.7,
}


def native_result_json(
    content: str,
    response_id: str | None,
    *,
    stats: dict[str, int | float] | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "model_instance_id": "m",
        "output": [{"type": "message", "content": content}],
        "stats": stats
        or {
            "input_tokens": 100,
            "total_output_tokens": 10,
            "reasoning_output_tokens": 0,
            "time_to_first_token_seconds": 0.2,
        },
    }
    if response_id is not None:
        body["response_id"] = response_id
    return body


def stream_lines(content: str, response_id: str, input_tokens: int = 100) -> list[str]:
    result = native_result_json(
        content,
        response_id,
        stats={
            "input_tokens": input_tokens,
            "total_output_tokens": 8,
            "reasoning_output_tokens": 0,
            "time_to_first_token_seconds": 0.2,
        },
    )
    return [
        f'data: {json.dumps({"type": "message.delta", "content": content}, ensure_ascii=False)}',
        f'data: {json.dumps({"type": "chat.end", "result": result}, ensure_ascii=False)}',
    ]


def fake_json_response(data: dict[str, object], status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        request=httpx.Request("POST", "http://localhost:1234/api/v1/chat"),
        json=data,
    )


SSE_LINES = [
    'data: {"type":"chat.start"}',
    'data: {"type":"message.delta","content":"你"}',
    'data: {"type":"message.delta","content":"好"}',
    'data: {"type":"message.delta","content":""}',
    'data: {"type":"message.end"}',
    "data: "
    + json.dumps(
        {"type": "chat.end", "result": native_result_json("你好", "resp_first")},
        ensure_ascii=False,
    ),
]


class FakeSSEResponse:
    def __init__(
        self,
        lines: list[str],
        *,
        status_code: int = 200,
        error_body: dict[str, Any] | None = None,
    ) -> None:
        self._lines = lines
        self._response = httpx.Response(
            status_code,
            request=httpx.Request("POST", "http://localhost:1234/api/v1/chat"),
            json=error_body,
        )

    def raise_for_status(self) -> None:
        self._response.raise_for_status()

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


def make_second_turn_context() -> LLMContext:
    return LLMContext(
        messages=[
            {"role": "system", "content": "你是一个中文语音助手"},
            {"role": "user", "content": "第一轮用户指令"},
            {"role": "assistant", "content": "第一轮助手回答"},
            {"role": "user", "content": "第二轮用户指令"},
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
        assert payload["system_prompt"] == "你是一个中文语音助手"
        assert payload["input"] == "你好"
        assert payload["store"] is True
        assert "previous_response_id" not in payload
        # SSE → chunk 转换：空 delta 被过滤
        assert [c.choices[0].delta.content for c in chunks] == ["你", "好"]
        assert svc._previous_response_id == "resp_first"

    async def test_chat_end_commits_usage_stats_with_response_id(self) -> None:
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")
        result = native_result_json("你好", "resp_first", stats=SSE_STATS)

        @asynccontextmanager
        async def fake_stream(*_: Any, **__: Any) -> Any:
            yield FakeSSEResponse(
                [
                    'data: {"type":"message.delta","content":"你好"}',
                    "data: "
                    + json.dumps(
                        {"type": "chat.end", "result": result}, ensure_ascii=False
                    ),
                ]
            )

        svc._http.stream = fake_stream  # type: ignore[method-assign]
        _ = [chunk async for chunk in await svc.get_chat_completions(make_context())]

        assert svc.last_chat_stats == NativeChatStats(6100, 12, 0, 1.7)
        assert svc.last_assistant_text == "你好"

    async def test_invalid_stats_prevents_chain_commit(self) -> None:
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")
        result = native_result_json("你好", "resp_bad", stats={"input_tokens": -1})

        @asynccontextmanager
        async def fake_stream(*_: Any, **__: Any) -> Any:
            yield FakeSSEResponse(
                [
                    'data: {"type":"message.delta","content":"你好"}',
                    "data: "
                    + json.dumps(
                        {"type": "chat.end", "result": result}, ensure_ascii=False
                    ),
                ]
            )

        svc._http.stream = fake_stream  # type: ignore[method-assign]
        with pytest.raises(ValueError, match="stats"):
            _ = [chunk async for chunk in await svc.get_chat_completions(make_context())]

        assert svc._previous_response_id is None
        assert svc.last_chat_stats is None

    async def test_native_chat_once_returns_validated_result(self) -> None:
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")
        svc._http.post = AsyncMock(
            return_value=fake_json_response(native_result_json("摘要", None))
        )

        result = await svc._native_chat_once(
            {"model": "m", "input": "历史", "store": False}, timeout_seconds=20.0
        )

        assert result.content == "摘要"
        assert result.response_id is None
        svc._http.post.assert_awaited_once()

    async def test_native_chat_once_propagates_timeout(self) -> None:
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")
        svc._http.post = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))

        with pytest.raises(httpx.ReadTimeout):
            await svc._native_chat_once(
                {"model": "m", "input": "历史", "store": False},
                timeout_seconds=1.0,
            )

    async def test_model_context_length_uses_loaded_instance_and_caches(self) -> None:
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")
        svc._http.get = AsyncMock(
            return_value=httpx.Response(
                200,
                request=httpx.Request("GET", "http://localhost:1234/api/v1/models"),
                json={
                    "models": [
                        {
                            "key": "m",
                            "loaded_instances": [
                                {"id": "m", "config": {"context_length": 262144}}
                            ],
                            "max_context_length": 131072,
                        }
                    ]
                },
            )
        )

        assert await svc._get_model_context_length() == 262144
        assert await svc._get_model_context_length() == 262144
        svc._http.get.assert_awaited_once()

    async def test_model_context_length_falls_back_to_model_maximum(self) -> None:
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")
        svc._http.get = AsyncMock(
            return_value=httpx.Response(
                200,
                request=httpx.Request("GET", "http://localhost:1234/api/v1/models"),
                json={
                    "models": [
                        {
                            "key": "m",
                            "loaded_instances": [],
                            "max_context_length": 65536,
                        }
                    ]
                },
            )
        )

        assert await svc._get_model_context_length() == 65536

    async def test_model_context_length_failure_is_cached_as_unavailable(self) -> None:
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")
        svc._http.get = AsyncMock(side_effect=httpx.ConnectError("offline"))

        assert await svc._get_model_context_length() is None
        assert await svc._get_model_context_length() is None
        svc._http.get.assert_awaited_once()

    async def test_second_turn_uses_previous_response_id_and_only_current_user(self) -> None:
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")
        payloads: list[dict[str, Any]] = []
        response_ids = iter(("resp_first", "resp_second"))

        @asynccontextmanager
        async def fake_stream(
            _method: str, _url: str, json: dict[str, Any] | None = None, **_: Any
        ) -> Any:
            payloads.append(json or {})
            response_id = next(response_ids)
            yield FakeSSEResponse(stream_lines("好", response_id))

        svc._http.stream = fake_stream  # type: ignore[method-assign]
        _ = [chunk async for chunk in await svc.get_chat_completions(make_context())]
        _ = [
            chunk
            async for chunk in await svc.get_chat_completions(make_second_turn_context())
        ]

        assert payloads[1]["input"] == "第二轮用户指令"
        assert payloads[1]["previous_response_id"] == "resp_first"
        assert "system_prompt" not in payloads[1]
        assert "第一轮用户指令" not in str(payloads[1])
        assert "第一轮助手回答" not in str(payloads[1])
        assert svc._previous_response_id == "resp_second"

    async def test_reset_conversation_starts_new_chain_with_current_system_prompt(self) -> None:
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")
        payloads: list[dict[str, Any]] = []

        @asynccontextmanager
        async def fake_stream(
            _method: str, _url: str, json: dict[str, Any] | None = None, **_: Any
        ) -> Any:
            payloads.append(json or {})
            yield FakeSSEResponse(SSE_LINES)

        svc._http.stream = fake_stream  # type: ignore[method-assign]
        _ = [chunk async for chunk in await svc.get_chat_completions(make_context())]
        svc.reset_conversation()
        _ = [chunk async for chunk in await svc.get_chat_completions(make_context())]

        assert payloads[1]["system_prompt"] == "你是一个中文语音助手"
        assert "previous_response_id" not in payloads[1]
        assert svc._previous_response_id == "resp_first"

    @pytest.mark.parametrize(
        "messages,match",
        [
            ([{"role": "user", "content": "你好"}], "exactly one text system"),
            (
                [
                    {"role": "system", "content": "系统一"},
                    {"role": "system", "content": "系统二"},
                    {"role": "user", "content": "你好"},
                ],
                "exactly one text system",
            ),
            (
                [
                    {"role": "system", "content": "系统"},
                    {"role": "assistant", "content": "回答"},
                ],
                "end with a text user",
            ),
            (
                [
                    {"role": "system", "content": "系统"},
                    {"role": "user", "content": []},
                ],
                "user message must contain text",
            ),
        ],
    )
    async def test_context_role_boundary_is_strict(
        self, messages: list[dict[str, Any]], match: str
    ) -> None:
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")

        with pytest.raises(ValueError, match=match):
            await svc.get_chat_completions(LLMContext(messages=messages))  # type: ignore[arg-type]

    async def test_sse_handles_done_and_malformed_lines(self) -> None:
        """验证 SSE 遇到 [DONE] 正常退出、遇到非 JSON 或空行不崩溃。"""
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")
        lines = [
            ": ping",
            "data: not json",
            *stream_lines("测试", "resp_done"),
            "data: [DONE]",
            'data: {"type":"message.delta","content":"不会被消费"}',
        ]

        @asynccontextmanager
        async def fake_stream(*_: Any, **__: Any) -> Any:
            yield FakeSSEResponse(lines)

        svc._http.stream = fake_stream  # type: ignore[method-assign]
        chunks = [chunk async for chunk in await svc.get_chat_completions(make_context())]
        assert [c.choices[0].delta.content for c in chunks] == ["测试"]

    async def test_missing_chat_end_raises_without_committing_state(self) -> None:
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")

        @asynccontextmanager
        async def fake_stream(*_: Any, **__: Any) -> Any:
            yield FakeSSEResponse(['data: {"type":"message.delta","content":"测试"}'])

        svc._http.stream = fake_stream  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match=r"chat\.end"):
            _ = [chunk async for chunk in await svc.get_chat_completions(make_context())]
        assert svc._previous_response_id is None

    async def test_invalid_previous_response_id_rebuilds_chain_once(self) -> None:
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")
        payloads: list[dict[str, Any]] = []
        call_count = 0

        @asynccontextmanager
        async def fake_stream(
            _method: str, _url: str, json: dict[str, Any] | None = None, **_: Any
        ) -> Any:
            nonlocal call_count
            call_count += 1
            payloads.append(json or {})
            if call_count == 1:
                yield FakeSSEResponse(SSE_LINES)
            elif call_count == 2:
                yield FakeSSEResponse(
                    [],
                    status_code=400,
                    error_body={
                        "error": {
                            "type": "invalid_request",
                            "param": "previous_response_id",
                            "message": "previous_response_id was not found",
                        }
                    },
                )
            else:
                yield FakeSSEResponse(stream_lines("恢复", "resp_recovered"))

        svc._http.stream = fake_stream  # type: ignore[method-assign]
        _ = [chunk async for chunk in await svc.get_chat_completions(make_context())]
        chunks = [
            chunk
            async for chunk in await svc.get_chat_completions(make_second_turn_context())
        ]

        assert [chunk.choices[0].delta.content for chunk in chunks] == ["恢复"]
        assert payloads[1]["previous_response_id"] == "resp_first"
        assert "previous_response_id" not in payloads[2]
        assert payloads[2]["system_prompt"] == "你是一个中文语音助手"
        assert svc._previous_response_id == "resp_recovered"

    @pytest.mark.parametrize(
        "chat_end_line,error_type",
        [
            ('data: {"type":"chat.end","result":[]}', TypeError),
            (
                "data: "
                + json.dumps(
                    {
                        "type": "chat.end",
                        "result": native_result_json("测试", "bad"),
                    },
                    ensure_ascii=False,
                ),
                ValueError,
            ),
        ],
    )
    async def test_invalid_chat_end_never_commits_state(
        self, chat_end_line: str, error_type: type[Exception]
    ) -> None:
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")

        @asynccontextmanager
        async def fake_stream(*_: Any, **__: Any) -> Any:
            yield FakeSSEResponse(
                ['data: {"type":"message.delta","content":"测试"}', chat_end_line]
            )

        svc._http.stream = fake_stream  # type: ignore[method-assign]
        with pytest.raises(error_type):
            _ = [chunk async for chunk in await svc.get_chat_completions(make_context())]
        assert svc._previous_response_id is None

    async def test_reset_during_inflight_stream_discards_late_response_id(self) -> None:
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")

        @asynccontextmanager
        async def fake_stream(*_: Any, **__: Any) -> Any:
            yield FakeSSEResponse(SSE_LINES)

        svc._http.stream = fake_stream  # type: ignore[method-assign]
        stream = await svc.get_chat_completions(make_context())
        svc.reset_conversation()
        chunks = [chunk async for chunk in stream]

        assert [chunk.choices[0].delta.content for chunk in chunks] == ["你", "好"]
        assert svc._previous_response_id is None

    async def test_invalid_previous_response_id_is_retried_only_once(self) -> None:
        svc = LmStudioNativeLLMService(model="m", base_url="http://localhost:1234")
        call_count = 0

        @asynccontextmanager
        async def fake_stream(*_: Any, **__: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield FakeSSEResponse(SSE_LINES)
                return
            yield FakeSSEResponse(
                [],
                status_code=400,
                error_body={
                    "error": {
                        "param": "previous_response_id",
                        "message": "previous_response_id was not found",
                    }
                },
            )

        svc._http.stream = fake_stream  # type: ignore[method-assign]
        _ = [chunk async for chunk in await svc.get_chat_completions(make_context())]
        with pytest.raises(httpx.HTTPStatusError):
            _ = [
                chunk
                async for chunk in await svc.get_chat_completions(
                    make_second_turn_context()
                )
            ]

        assert call_count == 3
        assert svc._previous_response_id is None

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

    def test_memory_protocol_is_present_once_before_persona(self) -> None:
        persona = "你是一位耐心顾问。"
        prompt = build_system_prompt(persona=persona)

        assert prompt.count("conversation_memory_data") == 1
        assert "最新原生 user turn" in prompt
        assert prompt.endswith(persona)
