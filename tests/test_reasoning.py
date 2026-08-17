"""LmStudioLLMService（LM Studio reasoning 注入）+ 系统提示词测试。

验证 build_chat_completion_params 覆写点（pipecat 官方 provider 定制钩子）
能注入 LM Studio 的 reasoning 开关。
"""

from __future__ import annotations

from voice_realtime.interaction.pipeline import build_system_prompt
from voice_realtime.interaction.reasoning import LmStudioLLMService

FAKE_PARAMS = {"messages": [], "tools": None, "tool_choice": None}


class TestLmStudioLLMService:
    def test_injects_reasoning_off_by_default(self) -> None:
        svc = LmStudioLLMService(model="test-model", base_url="http://localhost:1234/v1")
        params = svc.build_chat_completion_params(FAKE_PARAMS)  # type: ignore[arg-type]
        assert params["extra_body"] == {"reasoning": "off"}
        assert params["model"] == "test-model"

    def test_reasoning_configurable(self) -> None:
        svc = LmStudioLLMService(
            model="test-model",
            base_url="http://localhost:1234/v1",
            reasoning="on",
        )
        params = svc.build_chat_completion_params(FAKE_PARAMS)  # type: ignore[arg-type]
        assert params["extra_body"] == {"reasoning": "on"}

    def test_preserves_existing_params(self) -> None:
        svc = LmStudioLLMService(model="test-model", base_url="http://localhost:1234/v1")
        params = svc.build_chat_completion_params(
            {"messages": [{"role": "user", "content": "hi"}], "tools": None, "tool_choice": None}  # type: ignore[arg-type]
        )
        assert params["messages"] == [{"role": "user", "content": "hi"}]
        assert params["extra_body"] == {"reasoning": "off"}

    def test_base_url_passed_to_client(self) -> None:
        svc = LmStudioLLMService(model="test-model", base_url="http://localhost:1234/v1")
        assert str(svc._client.base_url).startswith("http://localhost:1234")  # type: ignore[attr-defined]


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
