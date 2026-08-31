"""Outbound adapters for the SpeechRail public protocol."""

from voice_realtime.speechrail.transport import (
    ConnectionFactory,
    SpeechRailConnection,
    SpeechRailProtocolError,
    SpeechRailRealtimeClient,
    SpeechRailV2Transport,
)
from voice_realtime.speechrail.tts import SpeechRailTTSClient

__all__ = [
    "ConnectionFactory",
    "SpeechRailConnection",
    "SpeechRailProtocolError",
    "SpeechRailRealtimeClient",
    "SpeechRailTTSClient",
    "SpeechRailV2Transport",
]
