"""LM Studio 适配：原生 /api/v1/chat 端点 LLM 服务（reasoning 开关）。

QA 验证结论（2026-08-17，本机实测）：
- LM Studio 的 OpenAI 兼容端点 **忽略** `reasoning` 参数：模型始终思考
  （`reasoning_content` 占满 token、`content` 为空、TTFT 被思考拉长）。
- reasoning 开关只在 **原生 `/api/v1/chat`** 端点生效：
  `reasoning:"off"` 时 `reasoning_output_tokens=0`、TTFT 0.261s（实测）。
- 原生端点输入是**无 role 的 text items 序列**（按顺序隐式推断角色），
  支持 `stream:true` 的 SSE（`message.delta` 事件逐字输出）；
  不接受 `max_tokens`/`role` 字段。

本服务子类化 `OpenAILLMService`（复用适配器、上下文、指标、中断机制），
仅覆写 `get_chat_completions` 的传输层走原生端点。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import httpx
from openai import AsyncStream
from openai.types.chat import ChatCompletionChunk, ChatCompletionMessageParam
from pipecat.frames.frames import CancelFrame, EndFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.openai.llm import OpenAILLMService

DEFAULT_SYSTEM_PROMPT = (
    "# 角色\n"
    "你是一个中文语音助手，通过语音与用户自然对话，像面对面聊天一样口语化，"
    "语句短、用词简单。\n"
    "\n"
    "# 输出规则\n"
    "- 回答简短精炼：通常一到两句，一般不超过三句；只有用户要求展开时才多说。\n"
    "- 用口语表达，避免书面连接词（如“此外”“综上所述”）和冗长从句。\n"
    "- 数字、日期、单位写成读出来的样子（把“95%”读作“百分之九十五”），方便语音合成。\n"
    "\n"
    "# 禁止\n"
    "- 不要使用 markdown、列表符号、括号或表情符号，它们会被逐字朗读出来。\n"
    "- 不要重复用户的话，不要复述问题。\n"
    "- 不要机械客套收尾（如“还有什么需要帮忙的吗”），不要过度道歉。\n"
    "- 一次只问一个问题。\n"
    "\n"
    "# 容错\n"
    "- 用户的话可能被语音识别转错：意思不通时先推测真实意图，不要抓住字面追问。\n"
    "- 确实无法确定时，用一句话澄清并给出两个选项，不要反复追问。\n"
    "- 不知道或不方便回答时坦诚说明，不要编造。\n"
)

_NATIVE_CHAT_PATH = "/api/v1/chat"


def _text_content(message: ChatCompletionMessageParam) -> str | None:
    """提取文本消息内容；非文本（多模态）或空内容返回 None。"""
    content = message.get("content")
    if isinstance(content, str) and content:
        return content
    return None


class LmStudioNativeLLMService(OpenAILLMService):
    """调用 LM Studio 原生 /api/v1/chat 端点，注入有效的 reasoning 开关。

    OpenAI 兼容端点忽略 reasoning 参数会导致模型始终思考（content 为空、
    token 耗尽），因此必须走原生端点。实测 `reasoning:"off"` 时
    reasoning_output_tokens=0，TTFT≈260ms，满足对话实时性预算。
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        reasoning: str = "off",
        temperature: float = 0.7,
        api_key: str = "lm-studio",
        **kwargs: Any,
    ) -> None:
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            settings=self.Settings(
                model=model,
                temperature=temperature,
            ),
            **kwargs,
        )
        self._reasoning = reasoning
        self._model = model
        self._temperature = temperature
        # 原生端点挂在 LM Studio 根路径下；兼容配置里带 "/v1" 的写法。
        root_url = base_url.rstrip("/")
        if root_url.endswith("/v1"):
            root_url = root_url[: -len("/v1")]
        self._http = httpx.AsyncClient(
            base_url=root_url,
            timeout=httpx.Timeout(connect=5.0, read=None, write=10.0, pool=5.0),
        )
        self._native_client_closed = False

    async def _native_completions(
        self, payload: dict[str, Any]
    ) -> AsyncIterator[ChatCompletionChunk]:
        """SSE 消费原生端点，把 message.delta 转成 OpenAI 兼容 chunk。"""
        saw_content = False
        async with self._http.stream("POST", _NATIVE_CHAT_PATH, json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw_data = line[len("data: ") :].strip()
                if raw_data == "[DONE]":
                    break
                try:
                    event = json.loads(raw_data)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(event, dict):
                    raise TypeError("LM Studio SSE event must be an object")
                event_type = event.get("type")
                if event_type == "error" or (
                    isinstance(event_type, str) and event_type.endswith(".error")
                ):
                    raise RuntimeError("LM Studio stream reported an error")
                if event_type != "message.delta":
                    continue
                content = event.get("content")
                if not isinstance(content, str):
                    raise TypeError("LM Studio delta content must be a string")
                if not content:
                    continue
                saw_content = True
                chunk = SimpleNamespace(
                    usage=None,
                    model=None,
                    choices=[
                        SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=None))
                    ],
                )
                # SimpleNamespace 与 ChatCompletionChunk 无类型重叠，先经 Any 中转
                yield cast(ChatCompletionChunk, cast(Any, chunk))
        if not saw_content:
            raise RuntimeError("LM Studio returned no message content")

    async def close(self) -> None:
        """关闭原生 HTTP 客户端；允许 stop/cleanup 重复调用。"""
        if self._native_client_closed:
            return
        await self._http.aclose()
        self._native_client_closed = True

    async def stop(self, frame: EndFrame) -> None:
        """停止处理器并释放原生 HTTP 连接池。"""
        try:
            await super().stop(frame)
        finally:
            await self.close()

    async def cancel(self, frame: CancelFrame) -> None:
        """取消处理器并释放原生 HTTP 连接池。"""
        try:
            await super().cancel(frame)
        finally:
            await self.close()

    async def cleanup(self) -> None:
        """在管道清理兜底中释放原生 HTTP 连接池。"""
        try:
            await super().cleanup()  # type: ignore[no-untyped-call]
        finally:
            await self.close()

    async def get_chat_completions(self, context: LLMContext) -> AsyncStream[ChatCompletionChunk]:
        """覆写传输层：OpenAI 格式消息 → 原生端点 input items → SSE 流。"""
        adapter = self.get_llm_adapter()
        params_from_context = adapter.get_llm_invocation_params(
            context,
            system_instruction=None,
            convert_developer_to_user=False,
        )
        messages = params_from_context["messages"]
        input_items = [
            {"type": "text", "content": text}
            for text in (_text_content(m) for m in messages)
            if text
        ]
        payload: dict[str, Any] = {
            "model": self._model,
            "input": input_items,
            "reasoning": self._reasoning,
            "temperature": self._temperature,
            "stream": True,
        }
        return cast(AsyncStream[ChatCompletionChunk], self._native_completions(payload))
