"""LM Studio 适配：OpenAI 兼容 LLM + reasoning 开关注入。

pipecat 的 OpenAILLMService 通过 base_url 直连 LM Studio；
本模块在官方预留的 build_chat_completion_params 覆写点注入
LM Studio 的 `reasoning` 参数（对话场景关、复杂推理开）。
"""

from __future__ import annotations

from typing import Any

from pipecat.services.openai.llm import OpenAILLMService

DEFAULT_SYSTEM_PROMPT = (
    "你是一个中文语音助手，通过语音与用户对话。"
    "要求：1) 用中文回答；2) 回答简短精炼（口头对话风格，通常不超过 3 句话）；"
    "3) 不要重复用户的话，不要复述问题；4) 不要使用 markdown 格式、列表或表情符号；"
    "5) 不确定时就坦诚说明。"
)


class LmStudioLLMService(OpenAILLMService):
    """面向 LM Studio 的 OpenAILLMService 子类，注入 reasoning 开关。"""

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

    def build_chat_completion_params(self, params_from_context: Any) -> dict[str, Any]:
        """在官方 provider 定制钩子注入 LM Studio reasoning 参数。"""
        params = super().build_chat_completion_params(params_from_context)
        params["extra_body"] = {"reasoning": self._reasoning}
        return params
