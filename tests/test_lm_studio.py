"""LM Studio 端点与认证 helper 测试。"""

from __future__ import annotations

import pytest

from voice_realtime.lm_studio import (
    DEFAULT_LM_STUDIO_API_KEY,
    LMStudioClient,
    LMStudioOutputLimitError,
    LMStudioProtocolError,
    LMStudioResponseError,
    NativeChatRequest,
    lm_studio_auth_headers,
    lm_studio_openai_models_url,
    lm_studio_root_url,
)


def test_native_chat_request_uses_safe_defaults_and_optional_parameters() -> None:
    request = NativeChatRequest(model="m", input="你好")
    assert request.to_payload() == {
        "model": "m", "input": "你好", "stream": True, "store": False,
        "reasoning": "off",
    }
    payload = NativeChatRequest(
        model="m", input="hi", system_prompt="sys", temperature=0.2,
        max_output_tokens=32, previous_response_id="resp_x",
    ).to_payload()
    assert payload["system_prompt"] == "sys"
    assert payload["previous_response_id"] == "resp_x"
    assert NativeChatRequest.from_payload(payload).to_payload() == payload


async def test_native_client_parses_sse_without_leaking_non_message_events() -> None:
    import httpx

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/chat"
        body = await request.aread()
        assert b'"model":"m"' in body
        return httpx.Response(
            200,
            content=(
                b'data: {"type":"chat.start"}\n\n'
                b'data: {"type":"reasoning.delta","content":"hidden"}\n\n'
                b'data: {"type":"message.delta","content":"hello"}\n\n'
                b'data: {"type":"chat.end","result":{"output":[],"stats":{}}}\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )

    client = LMStudioClient(
        base_url="http://localhost:1234",
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    events = [event async for event in client.stream_chat(NativeChatRequest(model="m", input="hi"))]
    await client.aclose()
    assert [event.type for event in events] == [
        "chat.start", "reasoning.delta", "message.delta", "chat.end"
    ]
    assert events[2].content == "hello"


async def test_native_client_completes_text_and_normalizes_terminal_result() -> None:
    import httpx

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            content=(
                b'data: {"type":"reasoning.delta","content":"hidden"}\n\n'
                b'data: {"type":"message.delta","content":"hel"}\n\n'
                b'data: {"type":"message.delta","content":"lo"}\n\n'
                b'data: {"type":"chat.end","result":{"response_id":"resp_1",'
                b'"stats":{"input_tokens":7,"total_output_tokens":2}}}\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )

    client = LMStudioClient(
        base_url="http://localhost:1234",
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await client.complete_chat(NativeChatRequest(model="m", input="hi"))

    assert result.text == "hello"
    assert result.response_id == "resp_1"
    assert result.stats == {"input_tokens": 7, "total_output_tokens": 2}


async def test_native_client_uses_chat_end_output_when_no_delta_arrives() -> None:
    import httpx

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            content=(
                b'data: {"type":"chat.end","result":{"output":['
                b'{"type":"message","content":"fallback"}]}}\n\n'
            ),
        )

    client = LMStudioClient(
        base_url="http://localhost:1234",
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await client.complete_chat(NativeChatRequest(model="m", input="hi"))

    assert result.text == "fallback"


async def test_native_client_rejects_missing_terminal_event() -> None:
    import httpx

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            content=b'data: {"type":"message.delta","content":"partial"}\n\n',
        )

    client = LMStudioClient(
        base_url="http://localhost:1234",
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(LMStudioProtocolError, match=r"chat\.end"):
        await client.complete_chat(NativeChatRequest(model="m", input="hi"))


async def test_native_client_maps_error_event_without_exposing_raw_payload() -> None:
    import httpx

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            content=(
                b'data: {"type":"chat.error","error":'
                b'{"code":"model_unloaded","message":"load model first"}}\n\n'
            ),
        )

    client = LMStudioClient(
        base_url="http://localhost:1234",
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(LMStudioResponseError) as exc_info:
        await client.complete_chat(NativeChatRequest(model="m", input="hi"))

    assert exc_info.value.code == "model_unloaded"
    assert str(exc_info.value) == "load model first"


async def test_native_client_enforces_output_character_limit() -> None:
    import httpx

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            content=(
                b'data: {"type":"message.delta","content":"123"}\n\n'
                b'data: {"type":"message.delta","content":"456"}\n\n'
                b'data: {"type":"chat.end","result":{}}\n\n'
            ),
        )

    client = LMStudioClient(
        base_url="http://localhost:1234",
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(LMStudioOutputLimitError):
        await client.complete_chat(
            NativeChatRequest(model="m", input="hi"), max_output_chars=5
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
