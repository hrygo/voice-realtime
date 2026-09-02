"""ASR 后端无关的领域契约与适配器。"""

from sona.asr.contracts import (
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
