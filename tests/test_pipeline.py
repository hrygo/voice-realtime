"""build_pipeline 装配测试：验证处理器链与配置透传。

用 mock transport 注入，避免单元测试触碰真实 PyAudio/麦克风。
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from voice_realtime.audio.audio_injector import AudioInjector
from voice_realtime.config import TTS_OUTPUT_SAMPLE_RATE, InteractionSettings
from voice_realtime.interaction.pipeline import (
    DEFAULT_SENSEVOICE_REPO,
    EchoSuppressionProcessor,
    _resolve_stt_model,
    _to_pipecat_language,
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
        # source + 8 处理器 + sink（1.7 组装：input→echo→stt→user→llm→tts→output→assistant）
        assert len(processors) == 10

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
        # voice 在白名单内占位；桥固定用配置音色（VR_BRIDGE_VOICE），忽略该值
        assert tts_mock.call_args.kwargs["voice"] == "alloy"
        assert tts_mock.call_args.kwargs["sample_rate"] == TTS_OUTPUT_SAMPLE_RATE

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

    def test_stt_language_uses_configured_language(
        self,
        mock_transport: MagicMock,
        mock_services: list[MagicMock],
    ) -> None:
        settings = InteractionSettings(stt_language="EN", sample_rate=16000)
        with patch(
            "voice_realtime.interaction.pipeline.LocalAudioTransport", return_value=mock_transport
        ):
            build_pipeline(settings, transport=mock_transport)
        stt_settings = mock_services[0].call_args.kwargs["settings"]
        assert stt_settings.language == "en"

    def test_stt_language_normalizes_case(self) -> None:
        assert InteractionSettings(stt_language="YUE").stt_language == "yue"

    def test_stt_language_rejects_unknown_code(self) -> None:
        with pytest.raises(ValueError, match="en"):
            InteractionSettings(stt_language="fr")

    def test_stt_ttfs_p99_latency(
        self,
        settings: InteractionSettings,
        mock_transport: MagicMock,
        mock_services: list[MagicMock],
    ) -> None:
        with patch(
            "voice_realtime.interaction.pipeline.LocalAudioTransport", return_value=mock_transport
        ):
            build_pipeline(settings, transport=mock_transport)
        # ttfs_p99 实测 STT 交付 ~0.5s，且需 > silence_secs 以保留转写等待窗口
        assert mock_services[0].call_args.kwargs["ttfs_p99_latency"] == 0.5

    def test_echo_suppression_processor_installed(
        self,
        settings: InteractionSettings,
        mock_transport: MagicMock,
        mock_services: list[MagicMock],
    ) -> None:
        from voice_realtime.interaction.pipeline import EchoSuppressionProcessor

        settings = InteractionSettings(sample_rate=16000, interrupt_echo_suppression_ms=800)
        with patch(
            "voice_realtime.interaction.pipeline.LocalAudioTransport", return_value=mock_transport
        ):
            pipeline = build_pipeline(settings, transport=mock_transport)
        echo = pipeline.processors[2]
        assert isinstance(echo, EchoSuppressionProcessor)
        assert echo._window_seconds == 0.8  # type: ignore[attr-defined]

    def test_echo_suppression_disabled_when_zero(
        self,
        settings: InteractionSettings,
        mock_transport: MagicMock,
        mock_services: list[MagicMock],
    ) -> None:
        settings = InteractionSettings(sample_rate=16000, interrupt_echo_suppression_ms=0)
        with patch(
            "voice_realtime.interaction.pipeline.LocalAudioTransport", return_value=mock_transport
        ):
            pipeline = build_pipeline(settings, transport=mock_transport)
        # 0=关闭：窗口为 0，BotStarted 后立即放行所有音频
        echo = pipeline.processors[2]
        assert echo._window_seconds == 0.0  # type: ignore[attr-defined]

    @pytest.mark.parametrize("lang_code", ["zh", "en", "yue", "ja", "ko"])
    def test_language_mapping_accepts_supported_codes(self, lang_code: str) -> None:
        assert _to_pipecat_language(lang_code).value == lang_code

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
        # 1.7：VAD 集成进 LLMUserAggregatorParams，不在独立节点
        user_agg = pipeline.processors[4]  # pair.user()（input/echo/stt 之后）
        analyzer = user_agg._params.vad_analyzer  # type: ignore[attr-defined]
        assert analyzer.params.stop_secs == 0.8  # 跟随 settings.silence_secs（fixture=0.8）

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
        # 1.7：user aggregator 在 stt 之后（index 3），assistant aggregator 在管道末尾
        user_agg = pipeline.processors[4]
        context = user_agg._context  # type: ignore[attr-defined]
        assert context._messages  # type: ignore[attr-defined]
        assert context._messages[0]["role"] == "system"  # type: ignore[attr-defined]


class TestEchoSuppressionProcessor:
    """EchoSuppressionProcessor 帧级行为：窗口内丢弃音频、窗口外放行。"""

    @staticmethod
    async def _run(
        processor: EchoSuppressionProcessor,
        frame: Frame,
        direction: FrameDirection = FrameDirection.DOWNSTREAM,
    ) -> list[Frame]:
        emitted: list[Frame] = []

        async def _sink(local_frame: Frame, _: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
            emitted.append(local_frame)

        processor._next = MagicMock()
        with patch.object(processor._next, "queue_frame", side_effect=_sink):
            await processor.process_frame(frame, direction)
        return emitted

    @staticmethod
    def _make_processor(window_ms: int) -> EchoSuppressionProcessor:
        processor = EchoSuppressionProcessor(window_ms=window_ms)
        # 跳过 _check_started 门禁（单测粒度：帧级行为，不启动完整管道）
        processor._FrameProcessor__started = True
        return processor

    @staticmethod
    def _audio() -> InputAudioRawFrame:
        return InputAudioRawFrame(b"x" * 320, sample_rate=16000, num_channels=1)

    def test_drops_audio_inside_window(self) -> None:
        processor = self._make_processor(window_ms=100)
        emitted = asyncio.run(
            self._run(processor, BotStartedSpeakingFrame())
        ) + asyncio.run(self._run(processor, self._audio()))
        assert all(isinstance(f, BotStartedSpeakingFrame) for f in emitted)
        assert processor._dropped == 1  # type: ignore[attr-defined]
        assert processor._window_end is not None  # type: ignore[attr-defined]

    def test_forwards_audio_outside_window(self) -> None:
        processor = self._make_processor(window_ms=50)
        asyncio.run(self._run(processor, BotStartedSpeakingFrame()))
        time.sleep(0.1)
        emitted = asyncio.run(self._run(processor, self._audio()))
        assert [type(f).__name__ for f in emitted] == ["InputAudioRawFrame"]
        assert processor._dropped == 0  # type: ignore[attr-defined]

    def test_forwards_audio_when_bot_not_speaking(self) -> None:
        processor = self._make_processor(window_ms=500)
        emitted = asyncio.run(self._run(processor, self._audio()))
        assert [type(f).__name__ for f in emitted] == ["InputAudioRawFrame"]

    def test_bot_stopped_clears_window(self) -> None:
        processor = self._make_processor(window_ms=100)
        asyncio.run(self._run(processor, BotStartedSpeakingFrame()))
        asyncio.run(self._run(processor, BotStoppedSpeakingFrame()))
        emitted = asyncio.run(self._run(processor, self._audio()))
        assert [type(f).__name__ for f in emitted] == ["InputAudioRawFrame"]
        assert processor._window_end is None  # type: ignore[attr-defined]

    def test_window_opens_on_upstream_direction_too(self) -> None:
        # 运行期观察到 BotStarted UPSTREAM 未达 input->echo（pipecat 1.7 层间行为）；
        # 逻辑上需保证任一方向到达都能正确开窗
        processor = self._make_processor(window_ms=50)
        asyncio.run(self._run(processor, BotStartedSpeakingFrame(), FrameDirection.UPSTREAM))
        emitted = asyncio.run(self._run(processor, self._audio(), FrameDirection.UPSTREAM))
        assert emitted == []
        assert processor._dropped == 1  # type: ignore[attr-defined]


class TestInjectorMode:
    """AudioHub 单源扇出注入模式：audio_queue 非空 → 首节点 AudioInjector、transport 关麦。"""

    def test_first_node_is_audio_injector(
        self,
        settings: InteractionSettings,
        mock_transport: MagicMock,
        mock_services: list[MagicMock],
    ) -> None:
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        with patch(
            "voice_realtime.interaction.pipeline.LocalAudioTransport", return_value=mock_transport
        ):
            pipeline = build_pipeline(settings, transport=mock_transport, audio_queue=queue)

        processors = list(pipeline.processors)
        # 1.7 Pipeline 自动在链首插入 PipelineSource，真实输入源在索引 1
        assert len(processors) == 10
        first = processors[1]
        assert isinstance(first, AudioInjector)
        assert first._queue is queue  # type: ignore[attr-defined]
        mock_transport.input.assert_not_called()

    def test_default_mode_keeps_transport_input(
        self,
        settings: InteractionSettings,
        mock_transport: MagicMock,
        mock_services: list[MagicMock],
    ) -> None:
        with patch(
            "voice_realtime.interaction.pipeline.LocalAudioTransport", return_value=mock_transport
        ):
            pipeline = build_pipeline(settings, transport=mock_transport)

        processors = list(pipeline.processors)
        assert processors[1] is mock_transport.input.return_value
        mock_transport.input.assert_called_once()

    def test_transport_mic_disabled_when_injecting(
        self,
        settings: InteractionSettings,
        mock_services: list[MagicMock],
    ) -> None:
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        transport_mock = MagicMock()
        with patch(
            "voice_realtime.interaction.pipeline.LocalAudioTransport", return_value=transport_mock
        ) as mock_cls:
            build_pipeline(settings, audio_queue=queue)

        params = mock_cls.call_args.args[0]
        assert params.audio_in_enabled is False
