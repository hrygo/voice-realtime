"""Pipecat SenseVoice STT 的默认交互适配器。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.funasr.stt import FunASRSTTService, FunASRSTTSettings
from pipecat.transcriptions.language import Language

from voice_realtime.asr.contracts import ASRCapabilities
from voice_realtime.model_cache import resolve_model_snapshot

DEFAULT_SENSEVOICE_REPO = "FunAudioLLM/SenseVoiceSmall"
_PIPECAT_LANGUAGES = {
    "zh": Language.ZH,
    "en": Language.EN,
    "yue": Language.YUE,
    "ja": Language.JA,
    "ko": Language.KO,
}
logger = logging.getLogger(__name__)


def to_pipecat_language(lang_code: str) -> Language:
    normalized = lang_code.strip().lower()
    language = _PIPECAT_LANGUAGES.get(normalized)
    if language is not None:
        return language
    logger.warning("未知 STT 语言代码 %r，回退到中文", lang_code)
    return Language.ZH


def resolve_stt_model(model: str, *, allow_downloads: bool = False) -> str:
    """只向 Pipecat/FunASR 交付本地 SenseVoice snapshot。"""
    return resolve_model_snapshot(
        model,
        default_repo=DEFAULT_SENSEVOICE_REPO,
        allow_downloads=allow_downloads,
    )


@dataclass(frozen=True)
class PipecatSenseVoiceFactory:
    """保持当前交互 STT 参数不变的 processor 工厂。"""

    model: str = ""
    allow_model_downloads: bool = False
    device: str = "cpu"
    ttfs_p99_latency: float = 0.5
    backend_id: str = field(default="pipecat-sensevoice", init=False)
    capabilities: ASRCapabilities = field(
        default=ASRCapabilities(
            languages=frozenset(_PIPECAT_LANGUAGES),
            supports_partial=False,
            supports_segment_timestamps=False,
            supports_word_timestamps=False,
            supports_hotwords=False,
            supports_speaker_labels=False,
            supports_native_diarization=False,
            supports_eof_flush=False,
        ),
        init=False,
    )

    def create_processor(self, *, sample_rate: int, language: str) -> FrameProcessor:
        if sample_rate != 16000:
            raise ValueError("SenseVoice 交互 STT 仅支持 16000Hz PCM")
        return FunASRSTTService(
            device=self.device,
            settings=FunASRSTTSettings(
                model=resolve_stt_model(
                    self.model,
                    allow_downloads=self.allow_model_downloads,
                ),
                language=to_pipecat_language(language),
                use_itn=True,
            ),
            ttfs_p99_latency=self.ttfs_p99_latency,
        )
