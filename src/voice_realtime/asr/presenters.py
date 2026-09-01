"""ASR 领域对象到既有外部展示协议的纯转换。"""

from __future__ import annotations

from typing import Any

from voice_realtime.asr.models import ASRWindow


def _raw_speaker(speaker_key: str) -> int | str:
    raw = speaker_key.rpartition(":")[2]
    try:
        return int(raw)
    except ValueError:
        return raw


def _legacy_timestamp(timestamp_ms: int) -> str:
    hours, remainder = divmod(timestamp_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def legacy_ready_payload() -> dict[str, Any]:
    """生成浏览器当前依赖的 PCM/full 模式握手。"""
    return {"type": "config", "useAudioWorklet": False, "mode": "full"}


def legacy_subtitle_payload(window: ASRWindow) -> dict[str, Any]:
    """生成前端当前消费的完整字幕快照。"""
    return {
        "type": "full_update",
        "buffer_transcription": window.partial,
        "lines": [
            {
                "speaker": _raw_speaker(segment.speaker_key),
                "text": segment.text,
                "start": _legacy_timestamp(segment.start_ms),
                "end": _legacy_timestamp(segment.end_ms),
                "translation": segment.translation,
                "detected_language": segment.detected_language,
            }
            for segment in window.segments
        ],
    }
