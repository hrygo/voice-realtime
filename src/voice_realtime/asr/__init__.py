"""ASR 后端无关的领域契约与适配器。"""

from voice_realtime.asr.contracts import (
    ASRCapabilities,
    ASREvent,
    ASREventKind,
    ASRSessionContext,
    ConversationSTTFactory,
    StreamingTranscriber,
)

__all__ = [
    "ASRCapabilities",
    "ASREvent",
    "ASREventKind",
    "ASRSessionContext",
    "ConversationSTTFactory",
    "StreamingTranscriber",
]
