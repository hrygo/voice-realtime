"""TTSEngine 单元测试：加载/预热/流式合成 → PCM 分块。

用 mock 替换 mlx_audio 的 load 与 model.generate，验证引擎的
模型类型路由、音频格式转换与生命周期管理。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from unittest.mock import MagicMock, patch

import mlx.core as mx
import numpy as np
import pytest

from voice_realtime.config import BridgeSettings
from voice_realtime.tts_bridge.engine import VOICE_PROFILES, TTSEngine

TEST_MODEL = "test-org/test-tts-model"


def _mock_generation_result(
    samples: int = 4800,
    sample_rate: int = 24000,
    final: bool = True,
) -> MagicMock:
    """构造 GenerationResult mock，带真实 mx.array 音频。"""
    audio = mx.array(np.random.randn(samples).astype(np.float32))
    result = MagicMock()
    result.audio = audio
    result.samples = samples
    result.sample_rate = sample_rate
    result.is_streaming_chunk = not final
    result.is_final_chunk = final
    return result


def _mock_model(model_type: str = "voice_design") -> MagicMock:
    model = MagicMock()
    model.config.tts_model_type = model_type
    model.generate.return_value = iter([_mock_generation_result()])
    return model


@pytest.fixture
def settings() -> BridgeSettings:
    return BridgeSettings(model=TEST_MODEL, warmup_on_start=False)


@pytest.fixture
def engine(settings: BridgeSettings) -> TTSEngine:
    return TTSEngine(settings)


class TestLoad:
    def test_load_uses_mlx_audio_utils(self, engine: TTSEngine) -> None:
        with patch("mlx_audio.tts.utils.load", return_value=_mock_model()) as mock_load:
            engine.load()
        mock_load.assert_called_once_with(TEST_MODEL)
        assert engine.loaded

    def test_load_warmup_drains_generator(self) -> None:
        settings = BridgeSettings(model=TEST_MODEL, warmup_on_start=True)
        engine = TTSEngine(settings)
        model = _mock_model()
        with patch("mlx_audio.tts.utils.load", return_value=model):
            engine.load()
        model.generate.assert_called_once()
        assert model.generate.return_value.__next__  # 生成器被消费

    def test_load_warmup_routes_custom_voice(self) -> None:
        settings = BridgeSettings(model=TEST_MODEL, warmup_on_start=True, voice="Chelsie")
        engine = TTSEngine(settings)
        model = _mock_model("custom_voice")
        with patch("mlx_audio.tts.utils.load", return_value=model):
            engine.load()
        call_kwargs = model.generate.call_args.kwargs
        assert call_kwargs["voice"] == "Chelsie"
        assert call_kwargs["instruct"] is None

    def test_load_twice_is_idempotent(self, engine: TTSEngine) -> None:
        with patch("mlx_audio.tts.utils.load", return_value=_mock_model()) as mock_load:
            engine.load()
            engine.load()
        mock_load.assert_called_once()


class TestStreamSpeech:
    @pytest.mark.asyncio
    async def test_voice_design_uses_instruct(
        self, engine: TTSEngine, settings: BridgeSettings
    ) -> None:
        model = _mock_model("voice_design")
        with patch("mlx_audio.tts.utils.load", return_value=model):
            engine.load()
        chunks = [
            c
            async for c in engine.stream_speech("你好", voice="default", speed=1.0, lang="chinese")
        ]
        assert chunks
        call_kwargs = model.generate.call_args.kwargs
        assert call_kwargs["voice"] is None
        assert call_kwargs["instruct"] == VOICE_PROFILES["default"]
        assert call_kwargs["lang_code"] == "chinese"
        assert call_kwargs["stream"] is True
        assert call_kwargs["streaming_interval"] == settings.chunk_ms / 1000

    @pytest.mark.asyncio
    async def test_custom_voice_model_uses_speaker_name(self, engine: TTSEngine) -> None:
        model = _mock_model("custom_voice")
        with patch("mlx_audio.tts.utils.load", return_value=model):
            engine.load()
        await anext(engine.stream_speech("你好", voice="Chelsie"))
        call_kwargs = model.generate.call_args.kwargs
        assert call_kwargs["voice"] == "Chelsie"
        assert call_kwargs["instruct"] is None

    @pytest.mark.asyncio
    async def test_unknown_voice_passthrough_as_instruct(self, engine: TTSEngine) -> None:
        model = _mock_model("voice_design")
        with patch("mlx_audio.tts.utils.load", return_value=model):
            engine.load()
        await anext(engine.stream_speech("你好", voice="自定义音色描述"))
        assert model.generate.call_args.kwargs["instruct"] == "自定义音色描述"

    @pytest.mark.asyncio
    async def test_yields_int16_pcm_bytes(self, engine: TTSEngine) -> None:
        model = _mock_model("voice_design")
        with patch("mlx_audio.tts.utils.load", return_value=model):
            engine.load()
        chunks = [c async for c in engine.stream_speech("你好")]
        joined = b"".join(chunks)
        assert joined
        assert len(joined) % 2 == 0
        pcm = np.frombuffer(joined, dtype=np.int16)
        assert pcm.shape[0] == 4800  # 与 mock 音频样本数一致
        assert pcm.dtype == np.int16

    @pytest.mark.asyncio
    async def test_multi_chunk_streaming(self, engine: TTSEngine) -> None:
        model = _mock_model("voice_design")
        model.generate.return_value = iter(
            [
                _mock_generation_result(samples=2400, final=False),
                _mock_generation_result(samples=2400, final=True),
            ]
        )
        with patch("mlx_audio.tts.utils.load", return_value=model):
            engine.load()
        chunks = [c async for c in engine.stream_speech("你好")]
        assert len(chunks) == 2

    @pytest.mark.asyncio
    async def test_not_loaded_raises_runtime_error(self, engine: TTSEngine) -> None:
        with pytest.raises(RuntimeError, match="not loaded"):
            await anext(engine.stream_speech("你好"))

    @pytest.mark.asyncio
    async def test_model_generation_is_serialized(self, engine: TTSEngine) -> None:
        model = _mock_model("voice_design")
        first_started = threading.Event()
        release_first = threading.Event()
        calls = 0
        calls_lock = threading.Lock()

        def _generate(**_: object):
            nonlocal calls
            with calls_lock:
                calls += 1
                call_number = calls
            if call_number == 1:
                first_started.set()
                assert release_first.wait(timeout=2)
            yield _mock_generation_result(samples=16)

        model.generate.side_effect = _generate
        with patch("mlx_audio.tts.utils.load", return_value=model):
            engine.load()

        first = asyncio.create_task(_collect(engine.stream_speech("第一条")))
        assert await asyncio.to_thread(first_started.wait, 1)
        second = asyncio.create_task(_collect(engine.stream_speech("第二条")))
        await asyncio.sleep(0.05)

        assert calls == 1
        release_first.set()
        await asyncio.gather(first, second)
        assert calls == 2

    @pytest.mark.asyncio
    async def test_closing_stream_stops_producer(self, engine: TTSEngine) -> None:
        model = _mock_model("voice_design")
        producer_stopped = threading.Event()
        produced = 0

        def _generate(**_: object):
            nonlocal produced
            try:
                while True:
                    produced += 1
                    yield _mock_generation_result(samples=16)
                    time.sleep(0.005)
            finally:
                producer_stopped.set()

        model.generate.side_effect = _generate
        with patch("mlx_audio.tts.utils.load", return_value=model):
            engine.load()

        stream = engine.stream_speech("会被取消")
        assert await anext(stream)
        await asyncio.sleep(0.1)
        assert produced <= 10  # 1 块已消费 + 8 块队列 + 1 块等待入队
        await stream.aclose()

        assert await asyncio.to_thread(producer_stopped.wait, 1)

    @pytest.mark.asyncio
    async def test_generation_error_reaches_consumer(self, engine: TTSEngine) -> None:
        model = _mock_model("voice_design")

        def _generate(**_: object):
            raise RuntimeError("model failed")
            yield  # pragma: no cover - 保持生成器形状

        model.generate.side_effect = _generate
        with patch("mlx_audio.tts.utils.load", return_value=model):
            engine.load()

        with pytest.raises(RuntimeError, match="model failed"):
            await anext(engine.stream_speech("失败"))

    @pytest.mark.asyncio
    async def test_rejects_non_native_model_sample_rate(self, engine: TTSEngine) -> None:
        model = _mock_model("voice_design")
        model.generate.return_value = iter([_mock_generation_result(sample_rate=16000)])
        with patch("mlx_audio.tts.utils.load", return_value=model):
            engine.load()

        with pytest.raises(RuntimeError, match="unexpected sample rate"):
            await anext(engine.stream_speech("错误采样率"))


class TestClose:
    def test_close_resets_state(self, engine: TTSEngine) -> None:
        with patch("mlx_audio.tts.utils.load", return_value=_mock_model()):
            engine.load()
        engine.close()
        assert not engine.loaded

    def test_sample_rate_from_model(self, engine: TTSEngine) -> None:
        with patch("mlx_audio.tts.utils.load", return_value=_mock_model()):
            engine.load()
        assert engine.sample_rate == 24000


async def _collect(stream: AsyncIterator[bytes]) -> list[bytes]:
    return [chunk async for chunk in stream]
