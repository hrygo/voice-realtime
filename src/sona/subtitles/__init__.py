"""Sona 实时字幕与流式转录核心领域包。"""

from __future__ import annotations

from sona.subtitles.archive import SrtArchive
from sona.subtitles.clients import ClientSender, SubtitleClientHub
from sona.subtitles.proxy import (
    AudioListener,
    CaptureListener,
    GapListener,
    SubtitleProxy,
    SubtitleProxyDiagnostics,
    SubtitleProxyState,
    TranscriberFactory,
)
from sona.subtitles.sessions import (
    CapturePreparation,
    FinalizationTimeout,
    FinalizationTimeoutError,
    MeetingCaptureSession,
    StandardSubtitleSession,
    SubtitlePreparation,
    SubtitleSessionState,
    TranscriptionGap,
)

__all__ = [
    "AudioListener",
    "CaptureListener",
    "CapturePreparation",
    "ClientSender",
    "FinalizationTimeout",
    "FinalizationTimeoutError",
    "GapListener",
    "MeetingCaptureSession",
    "SrtArchive",
    "StandardSubtitleSession",
    "SubtitleClientHub",
    "SubtitlePreparation",
    "SubtitleProxy",
    "SubtitleProxyDiagnostics",
    "SubtitleProxyState",
    "SubtitleSessionState",
    "TranscriberFactory",
    "TranscriptionGap",
]
