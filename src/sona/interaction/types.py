"""交互领域共享值对象，不依赖 UI 或传输层。"""

from __future__ import annotations

from enum import StrEnum


class DuplexMode(StrEnum):
    """单机音频交互模式。"""

    SPEAKER_FOCUS = "speaker_focus"
    HEADPHONE_DUPLEX = "headphone_duplex"
