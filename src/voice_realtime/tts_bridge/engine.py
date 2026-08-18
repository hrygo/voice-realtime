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

from collections.abc import AsyncIterator
from typing import Any

import numpy as np

from voice_realtime.config import BridgeSettings

VOICE_PROFILES: dict[str, str] = {
    "default": "自然清晰的中文女声，语气平和亲切，语速适中，适合日常对话。",
    "warm": "温暖柔和的中文女声，语速略慢，语气舒缓，适合阅读与陪伴场景。",
    "bright": "明亮活泼的中文女声，音调偏高，语气轻快，适合播报与讲解。",
    "calm": "沉稳平静的中文男声，语速平稳，语气专业，适合资讯播报。",
}


class TTSEngine:
    """Qwen3-TTS 流式合成引擎（进程内单例，由 FastAPI lifespan 管理）。

    音色状态：
    - 构造时取 `settings.voice` 为当前音色；
    - `set_voice()` 运行时热切换（/v1/voice 端点）；
    - `stream_speech()` 默认使用当前音色。
    """

    def __init__(self, settings: BridgeSettings) -> None:
        self._settings = settings
        self._model: Any | None = None
        self._voice = settings.voice

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

        self._model = load(self._settings.model)
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
    ) -> AsyncIterator[bytes]:
        """流式合成，逐块产出 int16 PCM 字节。

        Args:
            text: 待合成文本。
            voice: 音色；None 使用当前音色（engine.voice）。VoiceDesign 模型下映射为
                音色描述（instruct）；未登记的 voice 值直接作为描述透传。
            speed: 语速倍率。
            lang: 语言代码（auto/chinese/english…）。
        """
        if self._model is None:
            raise RuntimeError("TTSEngine not loaded")
        _, instruct, speaker = self._synthesis_args(voice or self._voice)
        chunk_secs = self._settings.chunk_ms / 1000
        for result in self._model.generate(
            text=text,
            voice=speaker,
            instruct=instruct,
            speed=speed,
            lang_code=lang,
            stream=True,
            streaming_interval=chunk_secs,
        ):
            yield self._to_pcm(result)

    def _to_pcm(self, result: Any) -> bytes:
        """将 GenerationResult.audio (float32 mono) 转为 int16 PCM。"""
        audio: Any = result.audio
        samples = np.asarray(audio, dtype=np.float32)
        pcm = np.clip(samples * 32767.0, -32768.0, 32767.0).astype(np.int16)
        return pcm.tobytes()
