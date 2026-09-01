from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from voice_realtime.config import InteractionSettings
from voice_realtime.interaction.echo import EchoState, EchoTextBuffer
from voice_realtime.interaction.pipeline import (
    BotTextRecorder,
    EchoSuppressionProcessor,
    SelfEchoFilter,
    TTSStateObserver,
    build_pipeline,
)


@pytest.fixture
def settings() -> InteractionSettings:
    return InteractionSettings(sample_rate=16_000, silence_secs=0.8)


@pytest.fixture
def services() -> list[MagicMock]:
    factories = [MagicMock(), MagicMock(), MagicMock()]
    factories[0].return_value.create_processor.return_value = MagicMock(name="stt")
    with (
        patch("voice_realtime.interaction.pipeline.SpeechRailConversationSTTFactory", factories[0]),
        patch("voice_realtime.interaction.pipeline.LmStudioNativeLLMService", factories[1]),
        patch("voice_realtime.interaction.pipeline.SpeechRailTTSService", factories[2]),
    ):
        yield factories


def _transport() -> MagicMock:
    transport = MagicMock(name="transport")
    transport.input.return_value = MagicMock(name="input")
    transport.output.return_value = MagicMock(name="output")
    return transport


def test_pipeline_keeps_l1_l2_order_and_shared_resources(
    settings: InteractionSettings, services: list[MagicMock]
) -> None:
    transport = _transport()
    echo_state = EchoState()
    echo_buffer = EchoTextBuffer()

    pipeline = build_pipeline(
        settings,
        transport=transport,
        audio_queue=asyncio.Queue(),
        echo_state=echo_state,
        echo_buffer=echo_buffer,
    )

    processors = list(pipeline.processors)
    assert len(processors) == 13
    assert processors[2].__class__ is EchoSuppressionProcessor
    assert processors[4].__class__ is SelfEchoFilter
    assert processors[7].__class__ is BotTextRecorder
    assert processors[9].__class__ is TTSStateObserver
    l1 = processors[2]
    l2 = processors[4]
    recorder = processors[7]
    assert l1.echo_state is echo_state
    assert l2._echo_state is echo_state
    assert l2._buffer is recorder._buffer is echo_buffer
    assert l1._energy_gate is not l2._policy


def test_explicit_transport_stt_and_audio_queue_remain_construction_seams(
    settings: InteractionSettings, services: list[MagicMock]
) -> None:
    transport = _transport()
    stt = MagicMock(name="custom-stt")
    stt_factory = MagicMock(name="custom-stt-factory")
    stt_factory.create_processor.return_value = stt
    queue: asyncio.Queue[bytes] = asyncio.Queue()

    pipeline = build_pipeline(
        settings,
        transport=transport,
        stt_factory=stt_factory,
        audio_queue=queue,
    )

    processors = list(pipeline.processors)
    assert processors[1]._queue is queue
    assert processors[3] is stt
    transport.input.assert_not_called()
    stt_factory.create_processor.assert_called_once_with(sample_rate=16_000, language="zh")
