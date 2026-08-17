"""build_pipeline 装配测试：验证处理器链与配置透传。

用 mock transport 注入，避免单元测试触碰真实 PyAudio/麦克风。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from voice_realtime.config import InteractionSettings
from voice_realtime.interaction.pipeline import (
    DEFAULT_SENSEVOICE_REPO,
    _resolve_stt_model,
    build_pipeline,
)


class TestResolveSttModel:
    def test_local_path_passthrough(self, tmp_path) -> None:
        model_dir = tmp_path / "sensevoice"
        model_dir.mkdir()
        with patch("voice_realtime.interaction.pipeline.snapshot_download") as mock_dl:
            assert _resolve_stt_model(str(model_dir)) == str(model_dir)
        mock_dl.assert_not_called()

    def test_empty_uses_default_repo(self) -> None:
        with patch(
            "voice_realtime.interaction.pipeline.snapshot_download",
            return_value="/mnt/snapshot",
        ) as mock_dl:
            assert _resolve_stt_model("") == "/mnt/snapshot"
        mock_dl.assert_called_once_with(DEFAULT_SENSEVOICE_REPO)

    def test_custom_repo_resolved(self) -> None:
        with patch(
            "voice_realtime.interaction.pipeline.snapshot_download",
            return_value="/mnt/snapshot2",
        ) as mock_dl:
            assert _resolve_stt_model("some/other-stt") == "/mnt/snapshot2"
        mock_dl.assert_called_once_with("some/other-stt")


@pytest.fixture
def settings() -> InteractionSettings:
    return InteractionSettings(
        llm_base_url="http://localhost:1234/v1",
        llm_model="qwen/qwen3.6-35b-a3b",
        tts_bridge_url="http://127.0.0.1:8765/v1",
        tts_voice="default",
        silence_secs=0.8,
        sample_rate=16000,
    )


@pytest.fixture
def mock_transport() -> MagicMock:
    transport = MagicMock()
    transport.input.return_value = MagicMock()
    transport.output.return_value = MagicMock()
    return transport


@pytest.fixture
def mock_services() -> list[MagicMock]:
    """Mock 重型服务类：FunASRSTTService 构造会立即下载模型（网络阻塞）。"""
    mocks = [MagicMock(), MagicMock(), MagicMock()]
    with (
        patch("voice_realtime.interaction.pipeline.snapshot_download", return_value="/mnt/stt"),
        patch("voice_realtime.interaction.pipeline.FunASRSTTService", mocks[0]),
        patch("voice_realtime.interaction.pipeline.LmStudioNativeLLMService", mocks[1]),
        patch("voice_realtime.interaction.pipeline.OpenAITTSService", mocks[2]),
    ):
        yield mocks


class TestBuildPipeline:
    def test_returns_pipeline_with_full_chain(
        self,
        settings: InteractionSettings,
        mock_transport: MagicMock,
        mock_services: list[MagicMock],
    ) -> None:
        with patch(
            "voice_realtime.interaction.pipeline.LocalAudioTransport", return_value=mock_transport
        ):
            pipeline = build_pipeline(settings, transport=mock_transport)
        assert pipeline is not None
        processors = list(pipeline.processors)
        assert len(processors) == 10  # source + 8 处理器 + sink

    def test_llm_uses_lm_studio_endpoint(
        self,
        settings: InteractionSettings,
        mock_transport: MagicMock,
        mock_services: list[MagicMock],
    ) -> None:
        with patch(
            "voice_realtime.interaction.pipeline.LocalAudioTransport", return_value=mock_transport
        ):
            build_pipeline(settings, transport=mock_transport)
        llm_mock = mock_services[1]
        llm_mock.assert_called_once_with(
            model="qwen/qwen3.6-35b-a3b",
            base_url="http://localhost:1234/v1",
            temperature=0.7,
            reasoning="off",
        )

    def test_tts_points_at_bridge(
        self,
        settings: InteractionSettings,
        mock_transport: MagicMock,
        mock_services: list[MagicMock],
    ) -> None:
        with patch(
            "voice_realtime.interaction.pipeline.LocalAudioTransport", return_value=mock_transport
        ):
            build_pipeline(settings, transport=mock_transport)
        tts_mock = mock_services[2]
        tts_mock.assert_called_once()
        assert tts_mock.call_args.kwargs["base_url"] == "http://127.0.0.1:8765/v1"
        assert tts_mock.call_args.kwargs["voice"] == "default"
        assert tts_mock.call_args.kwargs["sample_rate"] == 24000

    def test_stt_language_is_chinese(
        self,
        settings: InteractionSettings,
        mock_transport: MagicMock,
        mock_services: list[MagicMock],
    ) -> None:
        from voice_realtime.interaction.pipeline import build_pipeline as bp

        with patch(
            "voice_realtime.interaction.pipeline.LocalAudioTransport", return_value=mock_transport
        ):
            bp(settings, transport=mock_transport)
        stt_mock = mock_services[0]
        stt_mock.assert_called_once()
        assert stt_mock.call_args.kwargs["device"] == "cpu"
        stt_settings = stt_mock.call_args.kwargs["settings"]
        assert stt_settings.language == "zh"
        assert stt_settings.use_itn is True

    def test_vad_silence_matches_settings(
        self,
        settings: InteractionSettings,
        mock_transport: MagicMock,
        mock_services: list[MagicMock],
    ) -> None:
        with patch(
            "voice_realtime.interaction.pipeline.LocalAudioTransport", return_value=mock_transport
        ):
            pipeline = build_pipeline(settings, transport=mock_transport)
        vad = pipeline.processors[2]
        analyzer_params = vad._vad_controller._vad_analyzer.params  # type: ignore[attr-defined]
        assert analyzer_params.stop_secs == 0.8  # 0.8s 静音判定说话结束

    def test_context_contains_system_prompt(
        self,
        settings: InteractionSettings,
        mock_transport: MagicMock,
        mock_services: list[MagicMock],
    ) -> None:
        with patch(
            "voice_realtime.interaction.pipeline.LocalAudioTransport", return_value=mock_transport
        ):
            pipeline = build_pipeline(settings, transport=mock_transport)
        # 处理器 3 = user aggregator，其 context 含 system 消息
        user_agg = pipeline.processors[4]
        context = user_agg._context  # type: ignore[attr-defined]
        assert context._messages  # type: ignore[attr-defined]
        assert context._messages[0]["role"] == "system"  # type: ignore[attr-defined]
