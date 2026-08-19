"""build_pipeline 装配测试：验证处理器链与配置透传。

用 mock transport 注入，避免单元测试触碰真实 PyAudio/麦克风。
"""

from __future__ import annotations

import asyncio
import struct
import time
from unittest.mock import MagicMock, patch

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
    LLMTextFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice_realtime.audio.audio_injector import AudioInjector
from voice_realtime.config import TTS_OUTPUT_SAMPLE_RATE, InteractionSettings
from voice_realtime.interaction.pipeline import (
    DEFAULT_SENSEVOICE_REPO,
    BotTextRecorder,
    EchoSuppressionProcessor,
    EchoTextBuffer,
    SelfEchoFilter,
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
        # source + 10 处理器 + sink（1.7 组装：input→echo→stt→self-echo→user
        # →llm→bot-text-recorder→tts→output→assistant）
        assert len(processors) == 12

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

        settings = InteractionSettings(
            sample_rate=16000, echo_barge_in_gain=3.0, echo_barge_in_frames=4
        )
        with patch(
            "voice_realtime.interaction.pipeline.LocalAudioTransport", return_value=mock_transport
        ):
            pipeline = build_pipeline(settings, transport=mock_transport)
        echo = pipeline.processors[2]
        assert isinstance(echo, EchoSuppressionProcessor)
        assert echo._barge_in_gain == 3.0  # type: ignore[attr-defined]
        assert echo._barge_in_frames == 4  # type: ignore[attr-defined]

    def test_self_echo_chain_installed(
        self,
        settings: InteractionSettings,
        mock_transport: MagicMock,
        mock_services: list[MagicMock],
    ) -> None:
        """L2 链：SelfEchoFilter 挂在 STT 与 user aggregator 之间，
        BotTextRecorder 挂在 LLM 与 TTS 之间，二者共享同一文本缓冲。"""
        with patch(
            "voice_realtime.interaction.pipeline.LocalAudioTransport", return_value=mock_transport
        ):
            pipeline = build_pipeline(
                settings, transport=mock_transport, audio_queue=asyncio.Queue()
            )
        processors = list(pipeline.processors)
        echo_filter = processors[4]
        recorder = processors[7]
        assert isinstance(echo_filter, SelfEchoFilter)
        assert isinstance(recorder, BotTextRecorder)
        assert echo_filter._buffer is recorder._buffer  # type: ignore[attr-defined]

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
        user_agg = pipeline.processors[5]  # pair.user()（input/echo/stt/self-echo 之后）
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
        # 1.7：user aggregator 在 stt 与 self-echo 过滤之后（index 5），
        # assistant aggregator 在管道末尾
        user_agg = pipeline.processors[5]
        context = user_agg._context  # type: ignore[attr-defined]
        assert context._messages  # type: ignore[attr-defined]
        assert context._messages[0]["role"] == "system"  # type: ignore[attr-defined]


async def _run_one(
    processor: FrameProcessor,
    frame: Frame,
    direction: FrameDirection = FrameDirection.DOWNSTREAM,
) -> list[Frame]:
    """单帧送入处理器，返回放行帧（测试用）。"""
    emitted: list[Frame] = []

    async def _sink(
        local_frame: Frame, _: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        emitted.append(local_frame)

    processor._next = MagicMock()  # type: ignore[attr-defined]
    with patch.object(processor._next, "queue_frame", side_effect=_sink):  # type: ignore[attr-defined]
        await processor.process_frame(frame, direction)
    return emitted


async def _run_seq(
    processor: FrameProcessor,
    frames: list[Frame],
    direction: FrameDirection = FrameDirection.DOWNSTREAM,
) -> list[Frame]:
    """按序把多帧送入处理器（依赖处理器状态机，须串行）。"""
    emitted: list[Frame] = []
    for frame in frames:
        emitted.extend(await _run_one(processor, frame, direction))
    return emitted


class TestEchoSuppressionProcessor:
    """EchoSuppressionProcessor 帧级行为：全播报期丢回声 + 能量门控插话。"""

    @staticmethod
    def _make_processor(
        gain: float = 2.5, frames: int = 3
    ) -> EchoSuppressionProcessor:
        processor = EchoSuppressionProcessor(barge_in_gain=gain, barge_in_frames=frames)
        processor._FrameProcessor__started = True  # type: ignore[attr-defined]
        return processor

    @staticmethod
    def _audio(amp: int = 1000) -> InputAudioRawFrame:
        """恒定振幅 int16 单声道帧：RMS ≈ amp。"""
        samples = struct.pack("<160h", *([amp] * 160))
        return InputAudioRawFrame(samples, sample_rate=16000, num_channels=1)

    def test_echo_frames_dropped_whole_utterance(self) -> None:
        """播报全程（BotStarted → BotStopped）低于插话门限的输入帧全部丢弃。"""
        processor = self._make_processor()
        emitted = asyncio.run(
            _run_seq(
                processor,
                [BotStartedSpeakingFrame()]
                + [self._audio(1000) for _ in range(6)],
            )
        )
        assert all(isinstance(f, BotStartedSpeakingFrame) for f in emitted)
        assert processor._dropped == 6  # type: ignore[attr-defined]
        assert processor._suppressing  # type: ignore[attr-defined]

    def test_barge_in_after_sustained_loud_input(self) -> None:
        """持续响语音（>基线×增益 连续 3 帧）→ 判定真人插话，恢复输入并放行。"""
        processor = self._make_processor(gain=2.5, frames=3)
        emitted_bot = asyncio.run(
            _run_seq(
                processor,
                [BotStartedSpeakingFrame()]
                + [self._audio(1000)] * 4  # warmup 基线
                + [self._audio(4000)] * 2  # 插话累积（streak=1, 2）
                + [self._audio(4000)],  # 第 3 帧超阈 → 触发，本帧放行
            )
        )
        emitted = asyncio.run(
            _run_seq(
                processor,
                [self._audio(1000), self._audio(1000)],  # 抑制已解除，直接放行
            )
        )
        # 插话帧放行；BotStarted 控制帧透传不计入音频断言
        audio_only_bot = [f for f in emitted_bot if isinstance(f, InputAudioRawFrame)]
        assert [type(f).__name__ for f in audio_only_bot] == ["InputAudioRawFrame"]
        assert [type(f).__name__ for f in emitted] == [
            "InputAudioRawFrame",
            "InputAudioRawFrame",
        ]
        assert not processor._suppressing  # type: ignore[attr-defined]

    def test_single_loud_frame_does_not_breakout(self) -> None:
        """单帧高能（<连续帧数要求）不视为插话，继续抑制。"""
        processor = self._make_processor(gain=2.5, frames=3)
        asyncio.run(
            _run_seq(
                processor,
                [BotStartedSpeakingFrame()]
                + [self._audio(1000)] * 4  # warmup 完成
                + [self._audio(4000)]  # 单次高能 → streak=1
                + [self._audio(1000)],  # 回落 → streak 清零
            )
        )
        assert processor._dropped == 6  # type: ignore[attr-defined]
        assert processor._suppressing  # type: ignore[attr-defined]

    def test_forwards_audio_when_bot_not_speaking(self) -> None:
        processor = self._make_processor()
        emitted = asyncio.run(_run_seq(processor, [self._audio(1000)]))
        assert [type(f).__name__ for f in emitted] == ["InputAudioRawFrame"]
        assert processor._dropped == 0  # type: ignore[attr-defined]

    def test_bot_stopped_lifts_suppression(self) -> None:
        processor = self._make_processor()
        asyncio.run(
            _run_seq(
                processor,
                [
                    BotStartedSpeakingFrame(),
                    self._audio(1000),
                    BotStoppedSpeakingFrame(),
                ],
            )
        )
        emitted = asyncio.run(_run_seq(processor, [self._audio(1000)]))
        assert [type(f).__name__ for f in emitted] == ["InputAudioRawFrame"]
        assert not processor._suppressing  # type: ignore[attr-defined]

    def test_opens_on_upstream_direction_too(self) -> None:
        # 运行期观察到 BotStarted UPSTREAM 未达 input->echo（pipecat 1.7 层间行为）；
        # 逻辑上需保证任一方向到达都能正确开抑制
        processor = self._make_processor()
        asyncio.run(
            _run_seq(
                processor,
                [BotStartedSpeakingFrame()],
                FrameDirection.UPSTREAM,
            )
        )
        asyncio.run(_run_seq(processor, [self._audio(1000)], FrameDirection.UPSTREAM))
        assert processor._dropped == 1  # type: ignore[attr-defined]


class TestEchoTextBuffer:
    def test_recent_entries_match_and_expire(self) -> None:
        buffer = EchoTextBuffer(window_secs=1.0)
        buffer.add("今天天气很好我们出去散步", now=100.0)
        assert buffer.matches("今天天气很好我们出去散步", 0.7, 4, now=100.5)
        assert not buffer.matches("今天天气很好我们出去散步", 0.7, 4, now=102.0)  # 过期

    def test_short_text_never_matches(self) -> None:
        buffer = EchoTextBuffer(window_secs=10.0)
        buffer.add("好的没问题", now=0.0)
        assert not buffer.matches("好的", 0.7, 4, now=1.0)

    def test_distinct_text_does_not_match(self) -> None:
        buffer = EchoTextBuffer(window_secs=10.0)
        buffer.add("介绍一下你自己吧", now=0.0)
        assert not buffer.matches("帮我订一张明天去北京的机票", 0.7, 4, now=1.0)

    def test_fragment_of_bot_text_matches_by_containment(self) -> None:
        """用户转写是机器人文本的子串（STT 只捕获回声尾部）→ 判定自回声。"""
        buffer = EchoTextBuffer(window_secs=10.0)
        buffer.add("今天我们一起去公园散步吧然后回家吃饭", now=0.0)
        assert buffer.matches("一起去公园散步", 0.7, 4, now=1.0)


class TestSelfEchoFilter:
    @staticmethod
    def _make_filter(buffer: EchoTextBuffer | None = None) -> SelfEchoFilter:
        processor = SelfEchoFilter(buffer or EchoTextBuffer(), min_ratio=0.7)
        processor._FrameProcessor__started = True  # type: ignore[attr-defined]
        return processor

    def test_drops_transcription_matching_recent_bot_text(self) -> None:
        buffer = EchoTextBuffer(window_secs=10.0)
        buffer.add("今天天气很好我们出去散步", now=time.monotonic())
        processor = self._make_filter(buffer)
        emitted = asyncio.run(
            _run_seq(
                processor,
                [TranscriptionFrame("今天天气很好我们出去散步", "user-1", "2026-08-19T00:00:00")],
            )
        )
        assert emitted == []
        assert processor._dropped == 1  # type: ignore[attr-defined]

    def test_passes_distinct_user_transcription(self) -> None:
        buffer = EchoTextBuffer(window_secs=10.0)
        buffer.add("介绍一下你自己吧", now=time.monotonic())
        processor = self._make_filter(buffer)
        emitted = asyncio.run(
            _run_seq(
                processor,
                [TranscriptionFrame("帮我订明天去北京的机票", "user-1", "2026-08-19T00:00:00")],
            )
        )
        assert [type(f).__name__ for f in emitted] == ["TranscriptionFrame"]

    def test_passes_non_text_frames(self) -> None:
        processor = self._make_filter()
        emitted = asyncio.run(
            _run_seq(processor, [BotStartedSpeakingFrame(), BotStoppedSpeakingFrame()])
        )
        assert {type(f).__name__ for f in emitted} == {
            "BotStartedSpeakingFrame",
            "BotStoppedSpeakingFrame",
        }


class TestBotTextRecorder:
    def test_records_llm_text_and_passes_through(self) -> None:
        buffer = EchoTextBuffer()
        recorder = BotTextRecorder(buffer)
        recorder._FrameProcessor__started = True  # type: ignore[attr-defined]
        emitted = asyncio.run(_run_seq(recorder, [LLMTextFrame("今天天气很好")]))
        assert [type(f).__name__ for f in emitted] == ["LLMTextFrame"]
        assert buffer.matches("今天天气很好", 0.99, 4, now=time.monotonic())

    def test_passes_control_frames_without_recording(self) -> None:
        buffer = EchoTextBuffer()
        recorder = BotTextRecorder(buffer)
        recorder._FrameProcessor__started = True  # type: ignore[attr-defined]
        asyncio.run(_run_seq(recorder, [BotStoppedSpeakingFrame()]))
        assert not buffer.matches("x", 0.5, 1, now=0.0)


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
        # 链：src, input, echo, stt, self-echo, user, llm, recorder, tts, output, assistant = 12
        assert len(processors) == 12
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
