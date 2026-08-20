"""桥 FastAPI 服务器测试：OpenAI 兼容端点 + 健康检查 + 错误路径。

使用 httpx ASGITransport + 注入 mock 引擎，验证流式响应、
WAV 头构造与 HTTP 语义。
"""

from __future__ import annotations

import struct
from typing import Protocol, cast
from unittest.mock import MagicMock

import httpx
import pytest

from voice_realtime.config import BridgeSettings
from voice_realtime.tts_bridge.engine import TTSEngine
from voice_realtime.tts_bridge.server import build_wav_header, create_app

TEST_MODEL = "test-org/test-tts-model"
SAMPLE_RATE = 24000


class _AppState(Protocol):
    engine: MagicMock


class _App(Protocol):
    state: _AppState


class _ASGITransport(Protocol):
    app: _App


def _mock_engine(loaded: bool = True, pcm_chunks: list[bytes] | None = None) -> MagicMock:
    engine = MagicMock(spec=TTSEngine)
    engine.loaded = loaded
    engine.sample_rate = SAMPLE_RATE
    engine.voice = "default"  # 实例属性不在 spec 内，需显式配置
    engine.available_voices = ["default", "warm", "bright", "calm"]
    chunks = pcm_chunks or [b"\x00\x00" * 4800]

    async def _gen(*args: object, **kwargs: object):
        for chunk in chunks:
            yield chunk

    engine.stream_speech = MagicMock(side_effect=_gen)
    return engine


@pytest.fixture
def client() -> httpx.AsyncClient:
    settings = BridgeSettings(model=TEST_MODEL, warmup_on_start=False)
    engine = _mock_engine()
    app = create_app(settings, engine=engine)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class TestHealth:
    @pytest.mark.asyncio
    async def test_health_ok(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["model"] == TEST_MODEL
        assert body["sample_rate"] == SAMPLE_RATE
        assert body["engine"] == "mlx-audio Qwen3-TTS"


class TestSpeech:
    @pytest.mark.asyncio
    async def test_wav_response_streams_with_riff_header(self, client: httpx.AsyncClient) -> None:
        resp = await client.post(
            "/v1/audio/speech",
            json={"model": TEST_MODEL, "input": "你好，世界", "response_format": "wav"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"
        body = resp.content
        assert body[:4] == b"RIFF"
        assert body[8:12] == b"WAVE"
        assert body[12:16] == b"fmt "
        sample_rate = struct.unpack("<I", body[24:28])[0]
        assert sample_rate == SAMPLE_RATE
        channels = struct.unpack("<H", body[22:24])[0]
        assert channels == 1
        assert body[36:40] == b"data"
        pcm_start = 44
        assert (len(body) - pcm_start) % 2 == 0
        # 缓冲模式：data 尺寸字段写入真实值（严格解析器兼容）
        data_size = struct.unpack("<I", body[40:44])[0]
        assert data_size == len(body) - 44

    @pytest.mark.asyncio
    async def test_pcm_response_is_raw_no_header(self, client: httpx.AsyncClient) -> None:
        resp = await client.post(
            "/v1/audio/speech",
            json={"model": TEST_MODEL, "input": "你好", "response_format": "pcm"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/x-pcm"
        assert not resp.content.startswith(b"RIFF")
        assert len(resp.content) == 9600  # 2 字节 * 4800 样本

    @pytest.mark.asyncio
    async def test_default_response_format_is_wav(self, client: httpx.AsyncClient) -> None:
        resp = await client.post("/v1/audio/speech", json={"model": TEST_MODEL, "input": "你好"})
        assert resp.headers["content-type"] == "audio/wav"
        assert resp.content.startswith(b"RIFF")

    @pytest.mark.asyncio
    async def test_config_voice_and_request_speed_forwarded_to_engine(
        self, client: httpx.AsyncClient
    ) -> None:
        engine = client._transport.app.state.engine  # type: ignore[attr-defined]
        await client.post(
            "/v1/audio/speech",
            json={"model": TEST_MODEL, "input": "你好", "voice": "warm", "speed": 1.2},
        )
        engine.stream_speech.assert_called_once_with(
            "你好", voice="warm", speed=1.2, lang="auto"
        )

    @pytest.mark.asyncio
    async def test_omitted_request_voice_uses_engine_default(
        self, client: httpx.AsyncClient
    ) -> None:
        engine = cast(_ASGITransport, cast(object, client._transport)).app.state.engine
        engine.voice = "calm"
        await client.post(
            "/v1/audio/speech",
            json={"model": TEST_MODEL, "input": "你好", "response_format": "pcm"},
        )
        engine.stream_speech.assert_called_once_with(
            "你好", voice="calm", speed=1.0, lang="auto"
        )

    @pytest.mark.asyncio
    async def test_lang_forwarded_to_engine(self, client: httpx.AsyncClient) -> None:
        engine = cast(_ASGITransport, cast(object, client._transport)).app.state.engine
        await client.post(
            "/v1/audio/speech",
            json={"model": TEST_MODEL, "input": "hello", "lang": "english"},
        )
        engine.stream_speech.assert_called_once_with(
            "hello", voice="default", speed=1.0, lang="english"
        )

    @pytest.mark.asyncio
    async def test_blank_lang_returns_422(self, client: httpx.AsyncClient) -> None:
        resp = await client.post(
            "/v1/audio/speech", json={"model": TEST_MODEL, "input": "你好", "lang": " "}
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_blank_request_voice_returns_422(self, client: httpx.AsyncClient) -> None:
        resp = await client.post(
            "/v1/audio/speech",
            json={"model": TEST_MODEL, "input": "你好", "voice": "  "},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_wav_synthesis_failure_returns_5xx(self) -> None:
        settings = BridgeSettings(model=TEST_MODEL, warmup_on_start=False)
        engine = _mock_engine()

        async def _failing_gen(*args: object, **kwargs: object):
            yield b"\x00\x00"
            raise RuntimeError("synthesis failed")

        engine.stream_speech = MagicMock(side_effect=_failing_gen)
        app = create_app(settings, engine=engine)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/audio/speech",
                json={"model": TEST_MODEL, "input": "你好", "response_format": "wav"},
            )
        assert 500 <= resp.status_code < 600
        assert "error" in resp.json()

    @pytest.mark.asyncio
    async def test_pcm_failure_before_first_chunk_returns_5xx(self) -> None:
        settings = BridgeSettings(model=TEST_MODEL, warmup_on_start=False)
        engine = _mock_engine()

        async def _failing_gen(*args: object, **kwargs: object):
            raise RuntimeError("synthesis failed")
            yield  # pragma: no cover - 保持异步生成器形状

        engine.stream_speech = MagicMock(side_effect=_failing_gen)
        app = create_app(settings, engine=engine)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/audio/speech",
                json={"model": TEST_MODEL, "input": "你好", "response_format": "pcm"},
            )
        assert 500 <= resp.status_code < 600
        assert "error" in resp.json()

    @pytest.mark.asyncio
    async def test_pcm_failure_after_first_chunk_ends_stream_without_fake_silence(
        self,
    ) -> None:
        settings = BridgeSettings(model=TEST_MODEL, warmup_on_start=False)
        engine = _mock_engine()

        async def _failing_gen(*args: object, **kwargs: object):
            yield b"\x01\x00"
            raise RuntimeError("synthesis failed")

        engine.stream_speech = MagicMock(side_effect=_failing_gen)
        app = create_app(settings, engine=engine)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/audio/speech",
                json={"model": TEST_MODEL, "input": "你好", "response_format": "pcm"},
            )
        assert resp.status_code == 200
        assert resp.content == b"\x01\x00"

    @pytest.mark.asyncio
    async def test_blank_input_returns_400(self, client: httpx.AsyncClient) -> None:
        resp = await client.post("/v1/audio/speech", json={"model": TEST_MODEL, "input": "   "})
        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body

    @pytest.mark.asyncio
    async def test_missing_input_returns_422(self, client: httpx.AsyncClient) -> None:
        resp = await client.post("/v1/audio/speech", json={"model": TEST_MODEL})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_response_format_returns_422(self, client: httpx.AsyncClient) -> None:
        resp = await client.post(
            "/v1/audio/speech",
            json={"model": TEST_MODEL, "input": "你好", "response_format": "mp3"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_engine_not_loaded_returns_503(self) -> None:
        settings = BridgeSettings(model=TEST_MODEL, warmup_on_start=False)
        engine = _mock_engine(loaded=False)
        app = create_app(settings, engine=engine)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/audio/speech", json={"model": TEST_MODEL, "input": "你好"}
            )
        assert resp.status_code == 503
        assert "error" in resp.json()

    @pytest.mark.asyncio
    async def test_lifespan_loads_engine_on_startup(self) -> None:
        from asgi_lifespan import LifespanManager

        settings = BridgeSettings(model=TEST_MODEL, warmup_on_start=True)
        engine = _mock_engine()
        app = create_app(settings, engine=engine)
        async with LifespanManager(app):
            pass
        engine.load.assert_called_once()


class TestWavHeader:
    def test_header_is_44_bytes_standard_pcm(self) -> None:
        header = build_wav_header(SAMPLE_RATE)
        assert len(header) == 44
        assert header[:4] == b"RIFF"
        assert header[8:12] == b"WAVE"
        assert header[36:40] == b"data"
        # fmt chunk: PCM=1, 1 声道, 16bit
        assert struct.unpack("<H", header[20:22])[0] == 1
        assert struct.unpack("<I", header[24:28])[0] == SAMPLE_RATE
        byte_rate = struct.unpack("<I", header[28:32])[0]
        assert byte_rate == SAMPLE_RATE * 2

    def test_header_with_data_size_writes_real_sizes(self) -> None:
        header = build_wav_header(SAMPLE_RATE, data_size=9600)
        assert struct.unpack("<I", header[4:8])[0] == 36 + 9600  # RIFF size
        assert struct.unpack("<I", header[40:44])[0] == 9600  # data size


class TestVoiceControl:
    """/v1/voices 查询 + /v1/voice 热切换。"""

    @pytest.mark.asyncio
    async def test_get_voices_lists_available(self, client: httpx.AsyncClient) -> None:
        engine = cast(_ASGITransport, cast(object, client._transport)).app.state.engine
        engine.voice = "default"
        engine.available_voices = ["default", "warm", "bright", "calm"]
        resp = await client.get("/v1/voices")
        assert resp.status_code == 200
        body = resp.json()
        assert body["voice"] == "default"
        assert body["available"] == ["default", "warm", "bright", "calm"]

    @pytest.mark.asyncio
    async def test_set_voice_switches_current(self, client: httpx.AsyncClient) -> None:
        engine = cast(_ASGITransport, cast(object, client._transport)).app.state.engine
        engine.voice = "default"
        engine.available_voices = ["default", "warm"]

        def _switch(v: str) -> None:
            engine.voice = v  # 模拟 TTSEngine.set_voice 的真实副作用

        engine.set_voice = MagicMock(side_effect=_switch)

        resp = await client.post("/v1/voice", json={"voice": "warm"})
        assert resp.status_code == 200
        engine.set_voice.assert_called_once_with("warm")
        assert resp.json()["voice"] == "warm"

    @pytest.mark.asyncio
    async def test_set_voice_blank_returns_422(self, client: httpx.AsyncClient) -> None:
        resp = await client.post("/v1/voice", json={"voice": "  "})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_set_voice_503_when_not_loaded(self) -> None:
        settings = BridgeSettings(model=TEST_MODEL, warmup_on_start=False)
        engine = _mock_engine(loaded=False)
        app = create_app(settings, engine=engine)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/v1/voice", json={"voice": "warm"})
        assert resp.status_code == 503
