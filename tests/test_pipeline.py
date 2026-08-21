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
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice_realtime.audio.audio_injector import AudioInjector
from voice_realtime.config import TTS_OUTPUT_SAMPLE_RATE, InteractionSettings
from voice_realtime.interaction.pipeline import (
    DEFAULT_SENSEVOICE_REPO,
    BotTextRecorder,
    EchoState,
    EchoSuppressionProcessor,
    EchoTextBuffer,
    HangoverUserMuteStrategy,
    SelfEchoFilter,
    TTSStateObserver,
    _resolve_stt_model,
    _to_pipecat_language,
    build_pipeline,
)


class TestResolveSttModel:
    def test_local_path_passthrough(self, tmp_path) -> None:
        model_dir = tmp_path / "sensevoice"
        model_dir.mkdir()
        with patch("voice_realtime.model_cache.snapshot_download") as mock_dl:
            assert _resolve_stt_model(str(model_dir)) == str(model_dir)
        mock_dl.assert_not_called()

    def test_empty_uses_default_repo(self) -> None:
        with patch(
            "voice_realtime.model_cache.snapshot_download",
            return_value="/mnt/snapshot",
        ) as mock_dl:
            assert _resolve_stt_model("") == "/mnt/snapshot"
        mock_dl.assert_called_once_with(DEFAULT_SENSEVOICE_REPO, local_files_only=True)

    def test_custom_repo_resolved(self) -> None:
        with patch(
            "voice_realtime.model_cache.snapshot_download",
            return_value="/mnt/snapshot2",
        ) as mock_dl:
            assert _resolve_stt_model("some/other-stt") == "/mnt/snapshot2"
        mock_dl.assert_called_once_with("some/other-stt", local_files_only=True)

    def test_explicit_download_mode_allows_network_fallback(self) -> None:
        with patch(
            "voice_realtime.model_cache.snapshot_download",
            return_value="/mnt/snapshot",
        ) as mock_dl:
            assert _resolve_stt_model("repo/model", allow_downloads=True) == "/mnt/snapshot"
        mock_dl.assert_called_once_with("repo/model", local_files_only=False)


@pytest.fixture
def settings() -> InteractionSettings:
    return InteractionSettings(
        llm_base_url="http://localhost:1234/v1",
        llm_model="qwen/qwen3.6-35b-a3b",
        tts_bridge_url="http://127.0.0.1:8765/v1",
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
        patch("voice_realtime.model_cache.snapshot_download", return_value="/mnt/stt"),
        patch("voice_realtime.interaction.pipeline.FunASRSTTService", mocks[0]),
        patch("voice_realtime.interaction.pipeline.LmStudioNativeLLMService", mocks[1]),
        patch("voice_realtime.interaction.pipeline.LocalBridgeTTSService", mocks[2]),
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
        # source + 11 处理器 + sink（1.7 组装：input→echo→stt→self-echo→user
        # →llm→bot-text-recorder→tts→tts-state-observer→output→assistant）
        assert len(processors) == 13

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
        # 内部哨兵要求桥使用当前权威音色，避免 OpenAI 的 alloy 占位覆盖热切换。
        from pipecat.services.openai.tts import VALID_VOICES

        from voice_realtime.config import TTS_ENGINE_DEFAULT_VOICE

        tts_mock.Settings.assert_called_with(voice=TTS_ENGINE_DEFAULT_VOICE)
        assert TTS_ENGINE_DEFAULT_VOICE in VALID_VOICES
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
        if isinstance(processor, EchoSuppressionProcessor):
            state = processor._echo_state  # type: ignore[attr-defined]
            if isinstance(frame, BotStartedSpeakingFrame):
                state.on_bot_speaking_started()
            elif isinstance(frame, BotStoppedSpeakingFrame):
                state.on_bot_speaking_stopped()
        emitted.extend(await _run_one(processor, frame, direction))
    return emitted


class TestEchoSuppressionProcessor:
    """EchoSuppressionProcessor 帧级行为：全播报期丢回声 + 能量门控插话。"""

    @staticmethod
    def _make_processor(
        gain: float = 2.5, frames: int = 3, allow_barge_in: bool = True
    ) -> EchoSuppressionProcessor:
        processor = EchoSuppressionProcessor(
            barge_in_gain=gain,
            barge_in_frames=frames,
            allow_barge_in=allow_barge_in,
            enable_direct_mode=True,
        )
        processor._FrameProcessor__started = True  # type: ignore[attr-defined]
        processor._task_manager = MagicMock()  # type: ignore[attr-defined]
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

    def test_hard_mute_mode_discards_all_audio_during_speaking(self) -> None:
        """默认物理闭麦模式（allow_barge_in=False）：播报全程丢弃所有麦克风输入（无论多大音量）。"""
        processor = self._make_processor(allow_barge_in=False)
        emitted = asyncio.run(
            _run_seq(
                processor,
                [BotStartedSpeakingFrame()]
                + [self._audio(8000) for _ in range(10)],
            )
        )
        audio_frames = [f for f in emitted if isinstance(f, InputAudioRawFrame)]
        assert audio_frames == []
        assert processor._dropped == 10  # type: ignore[attr-defined]
        assert processor._suppressing  # type: ignore[attr-defined]

    def test_barge_in_after_sustained_loud_input(self) -> None:
        """持续响语音（>基线×增益 连续 3 帧）→ 判定真人插话，恢复输入并放行。"""
        processor = self._make_processor(gain=2.5, frames=3)
        emitted_bot = asyncio.run(
            _run_seq(
                processor,
                [BotStartedSpeakingFrame()]
                + [self._audio(1000)] * 8  # warmup 基线 (8 帧)
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

    def test_headphone_mode_fast_barge_in(self) -> None:
        """耳机模式（gain=1.15, frames=2）：近场自然轻声（500 RMS）极速 2 帧触发插话放行。"""
        processor = self._make_processor(gain=1.15, frames=2, allow_barge_in=True)
        emitted = asyncio.run(
            _run_seq(
                processor,
                [BotStartedSpeakingFrame()]
                + [self._audio(200)] * 2  # 耳机模式 2 帧极速建峰
                + [self._audio(500)] * 2,  # 自然人声 2 帧即刻触发插话
            )
        )
        audio_frames = [f for f in emitted if isinstance(f, InputAudioRawFrame)]
        assert len(audio_frames) == 1
        assert processor._barge_in_active  # type: ignore[attr-defined]
        assert not processor._suppressing  # type: ignore[attr-defined]

    def test_headphone_voice_stays_unlocked_past_relock_window(self) -> None:
        processor = self._make_processor(gain=1.15, frames=2, allow_barge_in=True)
        asyncio.run(
            _run_seq(
                processor,
                [BotStartedSpeakingFrame()]
                + [self._audio(200)] * 2
                + [self._audio(500)] * 30,
            )
        )
        assert processor._barge_in_active  # type: ignore[attr-defined]

    def test_new_tts_generation_resets_energy_baseline_without_control_frame(self) -> None:
        state = EchoState()
        processor = EchoSuppressionProcessor(
            echo_state=state,
            allow_barge_in=True,
            enable_direct_mode=True,
        )
        processor._FrameProcessor__started = True  # type: ignore[attr-defined]
        state.on_tts_started()
        state.on_bot_speaking_started()
        asyncio.run(_run_seq(processor, [self._audio(1000)] * 8))
        state.on_tts_stopped()
        state.on_bot_speaking_stopped()
        previous_generation = state.generation
        state.on_tts_started()
        asyncio.run(_run_seq(processor, [self._audio(4000)]))
        assert state.generation > previous_generation
        assert len(processor._echo_rms) == 1  # type: ignore[attr-defined]
        assert processor._hot_streak == 0  # type: ignore[attr-defined]

    def test_echo_state_reset(self) -> None:
        """EchoState.reset 清空所有状态标志与时间戳。"""
        state = EchoState()
        state.on_tts_started()
        state.on_bot_speaking_started()
        assert state.bot_speaking
        assert state._tts_active
        assert state._speaker_active
        state.reset()
        assert not state.bot_speaking
        assert not state._tts_active
        assert not state._speaker_active
        assert state.last_speaking_stop_time == 0.0

    def test_single_loud_frame_does_not_breakout(self) -> None:
        """单帧高能（<连续帧数要求）不视为插话，继续抑制。"""
        processor = self._make_processor(gain=2.5, frames=3)
        asyncio.run(
            _run_seq(
                processor,
                [BotStartedSpeakingFrame()]
                + [self._audio(1000)] * 8  # warmup 完成
                + [self._audio(4000)]  # 单次高能 → streak=1
                + [self._audio(1000)],  # 回落 → streak 清零
            )
        )
        assert processor._dropped == 10  # type: ignore[attr-defined]
        assert processor._suppressing  # type: ignore[attr-defined]

    def test_interruption_blocked_during_speaking_without_barge_in(self) -> None:
        """播报期间若未确认真人插话，拦截 InterruptionFrame 防止自打断腰斩 TTS。"""
        processor = self._make_processor()
        emitted = asyncio.run(
            _run_seq(
                processor,
                [
                    BotStartedSpeakingFrame(),
                    InterruptionFrame(),
                ],
            )
        )
        # InterruptionFrame 被丢弃，仅放行 BotStartedSpeakingFrame
        assert len(emitted) == 1
        assert isinstance(emitted[0], BotStartedSpeakingFrame)

    def test_interruption_allowed_when_barge_in_active(self) -> None:
        """真人强力插话生效后，放行 InterruptionFrame。"""
        processor = self._make_processor(gain=2.5, frames=3)
        emitted = asyncio.run(
            _run_seq(
                processor,
                [BotStartedSpeakingFrame()]
                + [self._audio(1000)] * 8
                + [self._audio(4000)] * 3
                + [InterruptionFrame()],
            )
        )
        # 放行了 BotStarted, 1 帧音频, 以及 InterruptionFrame
        assert any(isinstance(f, InterruptionFrame) for f in emitted)

    def test_forwards_audio_when_bot_not_speaking(self) -> None:
        processor = self._make_processor()
        emitted = asyncio.run(_run_seq(processor, [self._audio(1000)]))
        assert [type(f).__name__ for f in emitted] == ["InputAudioRawFrame"]
        assert processor._dropped == 0  # type: ignore[attr-defined]

    def test_bot_stopped_lifts_suppression_after_hangover(self) -> None:
        # tail_hangover_secs=0.0 时立即解除抑制
        processor = EchoSuppressionProcessor(tail_hangover_secs=0.0)
        processor._FrameProcessor__started = True  # type: ignore[attr-defined]
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

    def test_tail_hangover_suppresses_residual_echo(self) -> None:
        # tail_hangover_secs=0.5 时，BotStoppedSpeaking 后的音频帧在窗口内仍被丢弃
        processor = EchoSuppressionProcessor(tail_hangover_secs=0.5)
        processor._FrameProcessor__started = True  # type: ignore[attr-defined]
        asyncio.run(
            _run_seq(
                processor,
                [
                    BotStartedSpeakingFrame(),
                    BotStoppedSpeakingFrame(),
                    self._audio(1000),
                ],
            )
        )
        assert processor._dropped == 1  # type: ignore[attr-defined]
        # 模拟时间流逝超过 hangover
        processor._echo_state.last_speaking_stop_time = (  # type: ignore[attr-defined]
            time.monotonic() - 0.6
        )
        emitted = asyncio.run(_run_seq(processor, [self._audio(1000)]))
        assert [type(f).__name__ for f in emitted] == ["InputAudioRawFrame"]

    def test_barge_in_auto_relocks_when_volume_drops(self) -> None:
        """插话放行后，若音量平稳回落到基线以下连续 25 帧且 Bot 仍在播报，自动重锁抑制。"""
        processor = self._make_processor(gain=2.5, frames=3)
        asyncio.run(
            _run_seq(
                processor,
                [BotStartedSpeakingFrame()]
                + [self._audio(1000)] * 8  # warmup
                + [self._audio(4000)] * 3,  # 触发插话
            )
        )
        assert not processor._suppressing  # type: ignore[attr-defined]
        assert processor._bot_speaking  # type: ignore[attr-defined]

        # 连续 25 帧低于基线峰值 80% 的平稳低音量
        asyncio.run(_run_seq(processor, [self._audio(500)] * 25))
        assert processor._suppressing  # type: ignore[attr-defined]

    def test_opens_on_upstream_direction_too(self) -> None:
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
        assert buffer.matches("今天天气很好我们出去散步", 0.7, 2, now=100.5)
        assert not buffer.matches("今天天气很好我们出去散步", 0.7, 2, now=102.0)  # 过期

    def test_short_text_substring_matches_bot_text(self) -> None:
        buffer = EchoTextBuffer(window_secs=10.0)
        buffer.add("今天天气很好我们出去散步吧", now=0.0)
        # 短词如果是机器人的子串，判定为回声
        assert buffer.matches("散步吧", 0.7, 2, now=1.0)
        assert buffer.matches("好的", 0.7, 2, now=1.0) is False

    def test_fuzzy_homophone_phrase_matches(self) -> None:
        """同音错字容错：STT 转写短语有 1 个同音字误差时，依然判定为自回声。"""
        buffer = EchoTextBuffer(window_secs=10.0)
        buffer.add("好的，请问还有什么可以帮您的吗？", now=0.0)
        # 帮您的嘛 (4字错1字)
        assert buffer.matches("帮您的嘛", 0.7, 2, now=1.0)
        # 明天下 与原句完全无关
        assert not buffer.matches("明天下", 0.7, 2, now=1.0)

    def test_punctuation_and_space_normalization(self) -> None:
        buffer = EchoTextBuffer(window_secs=10.0)
        buffer.add("你好，今天天气真不错！我们一起去公园？", now=0.0)
        # STT 识别结果可能无标点或空格
        assert buffer.matches("你好今天天气真不错我们一起去公园", 0.7, 2, now=1.0)
        assert buffer.matches("今天天气真不错", 0.7, 2, now=1.0)

    def test_distinct_text_does_not_match(self) -> None:
        buffer = EchoTextBuffer(window_secs=10.0)
        buffer.add("介绍一下你自己吧", now=0.0)
        assert not buffer.matches("帮我订一张明天去北京的机票", 0.7, 2, now=1.0)

    def test_fragment_of_bot_text_matches_by_containment(self) -> None:
        """用户转写是机器人文本的子串（STT 只捕获回声尾部）→ 判定自回声。"""
        buffer = EchoTextBuffer(window_secs=10.0)
        buffer.add("今天我们一起去公园散步吧然后回家吃饭", now=0.0)
        assert buffer.matches("一起去公园散步", 0.7, 2, now=1.0)

    def test_pinyin_fuzzy_homophone_echo_matching(self) -> None:
        """ASR 谐音错字（如'白昼时长'误识为'不市场'）经拼音模糊比对判定为回声。"""
        buffer = EchoTextBuffer(window_secs=10.0)
        buffer.add("你想估算哪个日期或季节的白昼时长呢比如夏至冬至还是今天", now=0.0)
        assert buffer.matches("不市场", 0.7, 2, now=1.0)
        assert buffer.matches("今天", 0.7, 2, now=1.0)
        assert not buffer.matches("今天北京天气怎么样", 0.7, 2, now=1.0)
        assert not buffer.matches("详细每个纬度的具体时长演算", 0.7, 2, now=1.0)

    def test_single_character_and_acknowledgement_are_not_killed(self) -> None:
        buffer = EchoTextBuffer(window_secs=10.0)
        buffer.add("夏天北极是极昼，好的，南极是极夜", now=0.0)
        assert not buffer.matches("夏", 0.7, 2, now=1.0)
        assert not buffer.matches("好", 0.7, 2, now=1.0)
        assert not buffer.matches("好的", 0.7, 2, now=1.0)


class TestHangoverUserMuteStrategy:
    def test_mutes_during_speech_and_hangover(self) -> None:
        state = EchoState()
        strategy = HangoverUserMuteStrategy(tail_hangover_secs=0.5, echo_state=state)
        # 初始未静音
        assert not asyncio.run(strategy.process_frame(InputAudioRawFrame(b"", 16000, 1)))

        # Bot 开始发声 -> 静音
        state.on_bot_speaking_started()
        assert asyncio.run(strategy.process_frame(BotStartedSpeakingFrame()))

        # Bot 停止发声 -> 依然在 hangover 窗口内静音
        state.on_bot_speaking_stopped()
        assert asyncio.run(strategy.process_frame(BotStoppedSpeakingFrame()))

        # 模拟时间超过 hangover
        state.last_speaking_stop_time = time.monotonic() - 0.6
        assert not asyncio.run(strategy.process_frame(InputAudioRawFrame(b"", 16000, 1)))


class TestSelfEchoFilter:
    @staticmethod
    def _make_filter(
        buffer: EchoTextBuffer | None = None, *, active: bool = True
    ) -> SelfEchoFilter:
        state = EchoState()
        if active:
            state.on_tts_started()
        processor = SelfEchoFilter(buffer or EchoTextBuffer(), min_ratio=0.7, echo_state=state)
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

    def test_matching_text_passes_outside_echo_window(self) -> None:
        buffer = EchoTextBuffer(window_secs=10.0)
        buffer.add("今天天气很好我们出去散步", now=time.monotonic())
        processor = self._make_filter(buffer, active=False)
        emitted = asyncio.run(
            _run_seq(
                processor,
                [TranscriptionFrame("今天天气很好我们出去散步", "user-1", "t")],
            )
        )
        assert len(emitted) == 1

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
    def test_llm_text_does_not_mark_tts_active(self) -> None:
        state = EchoState()
        recorder = BotTextRecorder(EchoTextBuffer())
        recorder._FrameProcessor__started = True  # type: ignore[attr-defined]
        asyncio.run(_run_seq(recorder, [LLMTextFrame("尚未合成")]))
        assert not state.bot_speaking
        assert not state._tts_active

    def test_tts_state_observer_is_state_writer(self) -> None:
        state = EchoState()
        observer = TTSStateObserver(state)
        observer._FrameProcessor__started = True  # type: ignore[attr-defined]
        from pipecat.frames.frames import TTSStartedFrame, TTSStoppedFrame

        asyncio.run(_run_seq(observer, [TTSStartedFrame()]))
        assert state.bot_speaking
        asyncio.run(_run_seq(observer, [TTSStoppedFrame()]))
        assert not state.bot_speaking

    def test_aggregates_streaming_tokens_and_flushes_on_punctuation(self) -> None:
        buffer = EchoTextBuffer()
        recorder = BotTextRecorder(buffer)
        recorder._FrameProcessor__started = True  # type: ignore[attr-defined]
        tokens = [LLMTextFrame("今天"), LLMTextFrame("天气"), LLMTextFrame("很好。")]
        emitted = asyncio.run(_run_seq(recorder, tokens))
        assert len(emitted) == 3
        assert buffer.matches("今天天气很好", 0.99, 2, now=time.monotonic())

    def test_flushes_on_response_end(self) -> None:
        buffer = EchoTextBuffer()
        recorder = BotTextRecorder(buffer)
        recorder._FrameProcessor__started = True  # type: ignore[attr-defined]
        asyncio.run(
            _run_seq(
                recorder,
                [LLMTextFrame("好的"), LLMTextFrame("没问题"), LLMFullResponseEndFrame()],
            )
        )
        assert buffer.matches("好的没问题", 0.99, 2, now=time.monotonic())

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
        # 链：src, input, echo, stt, self-echo, user, llm, recorder,
        #     tts, tts-observer, output, assistant, sink = 13
        assert len(processors) == 13
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
