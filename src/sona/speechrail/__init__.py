"""SpeechRail 公共协议基础设施客户端与适配器。"""

from __future__ import annotations

from sona.speechrail.stt_processor import (
    ClientFactory,
    SpeechRailConversationSTTFactory,
    SpeechRailConversationSTTProcessor,
)
from sona.speechrail.transcriber import (
    SpeechRailStreamingTranscriber,
)
from sona.speechrail.transcription_events import (
    Noop,
    SpeechRailTranscriptionError,
    TranscriptionCompleted,
    TranscriptionDelta,
    TranscriptionSegment,
    decode_transcription_event,
)
from sona.speechrail.transport import (
    ConnectionFactory,
    SpeechRailConnection,
    SpeechRailOpenAITransport,
    SpeechRailProtocolError,
    SpeechRailRealtimeClient,
)
from sona.speechrail.tts import SpeechRailTTSClient

__all__ = [
    "ClientFactory",
    "ConnectionFactory",
    "Noop",
    "SpeechRailConnection",
    "SpeechRailConversationSTTFactory",
    "SpeechRailConversationSTTProcessor",
    "SpeechRailOpenAITransport",
    "SpeechRailProtocolError",
    "SpeechRailRealtimeClient",
    "SpeechRailStreamingTranscriber",
    "SpeechRailTTSClient",
    "SpeechRailTranscriptionError",
    "TranscriptionCompleted",
    "TranscriptionDelta",
    "TranscriptionSegment",
    "decode_transcription_event",
]
