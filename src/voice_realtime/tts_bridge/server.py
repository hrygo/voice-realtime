"""qwen3-tts-openai 桥的 FastAPI 服务器。

对外暴露 OpenAI 兼容的 `POST /v1/audio/speech` 流式端点，
供 Pipecat 的 OpenAITTSService 等客户端直接消费。
"""

from __future__ import annotations

import logging
import struct
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from voice_realtime.config import BridgeSettings, get_settings
from voice_realtime.tts_bridge.engine import TTSEngine
from voice_realtime.tts_bridge.schema import HealthResponse, SpeechRequest

logger = logging.getLogger(__name__)


def build_wav_header(sample_rate: int, channels: int = 1, bits: int = 16) -> bytes:
    """构造标准 44 字节 PCM WAV 头（RIFF/WAVE/fmt/data）。

    流式输出时块大小未知，RIFF 与 data 尺寸字段置 0（播放器兼容）。
    """
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        0,  # RIFF chunk size（未知，置 0）
        b"WAVE",
        b"fmt ",
        16,  # fmt chunk size
        1,  # PCM
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
        b"data",
        0,  # data chunk size（未知，置 0）
    )


def create_app(
    bridge_settings: BridgeSettings | None = None,
    engine: TTSEngine | None = None,
) -> FastAPI:
    """构建桥应用。engine 可注入（测试用），否则按 bridge_settings 创建。"""
    bridge_settings = bridge_settings or get_settings().bridge
    engine = engine or TTSEngine(bridge_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info("TTSEngine 加载中: %s", bridge_settings.model)
        engine.load()
        logger.info("TTSEngine 就绪 (sample_rate=%d)", engine.sample_rate)
        yield
        engine.close()

    app = FastAPI(title="qwen3-tts-openai bridge", version="0.1.0", lifespan=lifespan)
    app.state.settings = bridge_settings
    app.state.engine = engine

    @app.exception_handler(HTTPException)
    async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            return JSONResponse(status_code=exc.status_code, content=detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "message": str(detail),
                    "type": "invalid_request_error",
                    "code": None,
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"message": "internal error", "type": "server_error", "code": None}},
        )

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok" if engine.loaded else "warming_up",
            model=bridge_settings.model,
            voice=bridge_settings.voice,
            sample_rate=engine.sample_rate if engine.loaded else bridge_settings.sample_rate,
        )

    @app.post("/v1/audio/speech")
    async def speech(req: SpeechRequest) -> StreamingResponse:
        if not engine.loaded:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": {"message": "engine not loaded", "type": "server_error", "code": None}
                },
            )
        if not req.input:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": "input must not be blank",
                        "type": "invalid_request_error",
                        "code": None,
                    }
                },
            )

        async def _stream() -> AsyncIterator[bytes]:
            if req.response_format == "wav":
                yield build_wav_header(engine.sample_rate)
            try:
                async for chunk in engine.stream_speech(
                    req.input,
                    voice=req.voice,
                    speed=req.speed,
                    lang="auto",
                ):
                    yield chunk
            except Exception:
                logger.exception("流式合成中断")
                return

        media_type = "audio/wav" if req.response_format == "wav" else "audio/x-pcm"
        return StreamingResponse(_stream(), media_type=media_type)

    return app


def main() -> None:
    """`vr-bridge` 控制台入口。"""
    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    bridge_settings = settings.bridge
    app = create_app(bridge_settings)
    uvicorn.run(app, host=bridge_settings.host, port=bridge_settings.port, log_level="info")


if __name__ == "__main__":
    main()
