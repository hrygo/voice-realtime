"""qwen3-tts-openai 桥的引擎层：封装 mlx-audio Qwen3-TTS 流式合成。

职责：
- 一次性加载模型并预热（FastAPI lifespan 内调用）
- 按模型类型路由参数（VoiceDesign→instruct 音色描述；CustomVoice→voice 说话人）
- 将 GenerationResult 的 mx.array float32 音频转换为 int16 PCM 字节流
- 按 chunk_ms 配置输出分块（streaming_interval）

所有 mlx-audio 依赖集中在 load() 与 stream_speech() 内部，
便于用 mock 做单元测试，真实模型仅手动 QA 时加载。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncGenerator
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

import numpy as np

from voice_realtime.config import BridgeSettings
from voice_realtime.model_cache import resolve_model_snapshot

VOICE_PROFILES: dict[str, str] = {
    "default": "自然清晰的中文女声，语气平和亲切，语速适中，适合日常对话。",
    "warm": "温暖柔和的中文女声，语速略慢，语气舒缓，适合阅读与陪伴场景。",
    "bright": "明亮活泼的中文女声，音调偏高，语气轻快，适合播报与讲解。",
    "calm": "沉稳平静的中文男声，语速平稳，语气专业，适合资讯播报。",
}

_STREAM_QUEUE_SIZE = 8
_QUEUE_PUT_POLL_SECS = 0.05
_MIN_GENERATION_TOKENS = 96
_MAX_GENERATION_TOKENS = 1200
_TOKENS_PER_TEXT_CHAR = 8


def _generation_token_budget(text: str) -> int:
    """为音频 token 设置与文本长度匹配的硬上限，避免异常采样长期占住单引擎。"""
    estimated = 32 + len(text.strip()) * _TOKENS_PER_TEXT_CHAR
    return max(_MIN_GENERATION_TOKENS, min(_MAX_GENERATION_TOKENS, estimated))


class TTSEngine:
    """Qwen3-TTS 流式合成引擎（进程内单例，由 FastAPI lifespan 管理）。

    音色状态：
    - 构造时取 `settings.voice` 为当前音色；
    - `set_voice()` 运行时热切换（/v1/voice 端点）；
    - `stream_speech()` 默认使用当前音色，生成过程脱离主事件循环后台执行。
    """

    def __init__(self, settings: BridgeSettings) -> None:
        self._settings = settings
        self._model: Any | None = None
        self._voice = settings.voice
        self._generation_lock = asyncio.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def voice(self) -> str:
        """当前生效音色（profile 名或自定义描述）。"""
        return self._voice

    @property
    def available_voices(self) -> list[str]:
        """内置音色 profile 列表（原始描述见 VOICE_PROFILES）。"""
        return list(VOICE_PROFILES)

    def set_voice(self, voice: str) -> str:
        """热切换当前音色；未登记的值直接作为自定义描述使用。"""
        if not voice.strip():
            raise ValueError("voice must not be blank")
        self._voice = voice
        return self._voice

    @property
    def sample_rate(self) -> int:
        if self._model is None:
            raise RuntimeError("TTSEngine not loaded")
        return self._settings.sample_rate

    def load(self) -> None:
        """加载模型并预热。幂等：已加载时直接返回。"""
        if self._model is not None:
            return
        from mlx_audio.tts.utils import load  # type: ignore[import-untyped]

        model_path = resolve_model_snapshot(
            self._settings.model,
            allow_downloads=self._settings.allow_model_downloads,
        )
        self._model = load(model_path)
        if self._settings.warmup_on_start:
            self._warmup()

    def _synthesis_args(self, voice: str) -> tuple[bool, str | None, str | None]:
        assert self._model is not None
        is_voice_design = self._model.config.tts_model_type == "voice_design"
        if is_voice_design:
            return True, VOICE_PROFILES.get(voice, voice), None
        return False, None, voice

    def _warmup(self) -> None:
        assert self._model is not None
        _, instruct, speaker = self._synthesis_args(self._voice)
        for _ in self._model.generate(
            text="预热",
            voice=speaker,
            instruct=instruct,
            max_tokens=_MIN_GENERATION_TOKENS,
            stream=True,
            streaming_interval=self._settings.chunk_ms / 1000,
        ):
            pass

    def close(self) -> None:
        """释放模型引用（后续 GC 回收；不主动清理 MLX 缓存）。"""
        self._model = None

    async def stream_speech(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float = 1.0,
        lang: str = "auto",
    ) -> AsyncGenerator[bytes, None]:
        """流式合成，逐块产出 int16 PCM 字节（后台线程隔离执行，0 阻塞事件循环）。

        Args:
            text: 待合成文本。
            voice: 音色；None 使用当前音色（engine.voice）。VoiceDesign 模型下映射为
                音色描述（instruct）；未登记的 voice 值直接作为描述透传。
            speed: 语速倍率。
            lang: 语言代码（auto/chinese/english…）。
        """
        async with self._generation_lock:
            if self._model is None:
                raise RuntimeError("TTSEngine not loaded")
            _, instruct, speaker = self._synthesis_args(voice or self._voice)
            chunk_secs = self._settings.chunk_ms / 1000

            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[bytes | Exception | None] = asyncio.Queue(
                maxsize=_STREAM_QUEUE_SIZE
            )
            stop_requested = threading.Event()

            def _publish(item: bytes | Exception | None) -> bool:
                """从模型线程向有界异步队列提交，取消时及时解除背压等待。"""
                if stop_requested.is_set():
                    return False
                try:
                    pending = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
                except RuntimeError:
                    stop_requested.set()
                    return False
                while not stop_requested.is_set():
                    try:
                        pending.result(timeout=_QUEUE_PUT_POLL_SECS)
                    except FutureTimeoutError:
                        continue
                    except Exception:
                        stop_requested.set()
                        return False
                    return True
                pending.cancel()
                return False

            def _worker() -> None:
                try:
                    assert self._model is not None
                    for result in self._model.generate(
                        text=text,
                        voice=speaker,
                        instruct=instruct,
                        speed=speed,
                        lang_code=lang,
                        max_tokens=_generation_token_budget(text),
                        stream=True,
                        streaming_interval=chunk_secs,
                    ):
                        if stop_requested.is_set():
                            break
                        if not _publish(self._to_pcm(result)):
                            break
                except Exception as exc:
                    _publish(exc)
                finally:
                    _publish(None)

            worker_thread = threading.Thread(
                target=_worker,
                name="tts-engine-generate",
                daemon=True,
            )
            worker_thread.start()

            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    if isinstance(item, Exception):
                        raise item
                    yield item
            finally:
                stop_requested.set()
                await asyncio.to_thread(worker_thread.join)

    def _to_pcm(self, result: Any) -> bytes:
        """将 GenerationResult.audio (float32 mono) 转为 int16 PCM。"""
        result_sample_rate = int(result.sample_rate)
        if result_sample_rate != self._settings.sample_rate:
            raise RuntimeError(
                "TTS model returned unexpected sample rate: "
                f"{result_sample_rate}, expected {self._settings.sample_rate}"
            )
        audio: Any = result.audio
        samples = np.asarray(audio, dtype=np.float32)
        pcm = np.clip(samples * 32767.0, -32768.0, 32767.0).astype(np.int16)
        return pcm.tobytes()
