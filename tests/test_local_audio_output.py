from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pipecat.frames.frames import StartFrame
from pipecat.transports.local.audio import LocalAudioTransportParams

from sona.audio.local_output import (
    StableLocalAudioOutputTransport,
    StableLocalAudioTransport,
    resolve_output_device_profile,
)


class FakePyAudio:
    def __init__(self, *, sample_rate: float = 48_000.0, channels: int = 2) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.open_kwargs: dict[str, Any] | None = None
        self.stream = MagicMock()

    def get_default_output_device_info(self) -> dict[str, object]:
        return {
            "index": 2,
            "name": "Test Speaker",
            "defaultSampleRate": self.sample_rate,
            "maxOutputChannels": self.channels,
        }

    def get_device_info_by_index(self, index: int) -> dict[str, object]:
        return {**self.get_default_output_device_info(), "index": index}

    def get_format_from_width(self, width: int) -> str:
        assert width == 2
        return "pcm16"

    def is_format_supported(self, rate: int, **kwargs: object) -> bool:
        assert rate == round(self.sample_rate)
        assert kwargs["output_channels"] == 1
        return True

    def open(self, **kwargs: Any) -> MagicMock:
        self.open_kwargs = kwargs
        return self.stream


class MissingOutputDevicePyAudio(FakePyAudio):
    def get_default_output_device_info(self) -> dict[str, object]:
        raise OSError("No Default Output Device Available")

    def get_device_info_by_index(self, index: int) -> dict[str, object]:
        del index
        raise OSError("Invalid output device")


@pytest.mark.parametrize(
    ("sample_rate", "expected_frames"),
    [(48_000.0, 1_920), (44_100.0, 1_764)],
)
def test_output_profile_uses_native_rate_and_40ms_buffer(
    sample_rate: float,
    expected_frames: int,
) -> None:
    profile = resolve_output_device_profile(
        FakePyAudio(sample_rate=sample_rate),
        output_device_index=None,
        buffer_ms=40,
    )
    assert profile.sample_rate == round(sample_rate)
    assert profile.frames_per_buffer == expected_frames
    assert profile.buffer_ms == 40


@pytest.mark.parametrize(
    "fake",
    [FakePyAudio(sample_rate=1.0), FakePyAudio(sample_rate=48_000.0, channels=0)],
)
def test_output_profile_rejects_invalid_device(fake: FakePyAudio) -> None:
    with pytest.raises(RuntimeError, match="audio_output_device_invalid"):
        resolve_output_device_profile(fake, output_device_index=None, buffer_ms=40)


@pytest.mark.parametrize("output_device_index", [None, 7])
def test_output_profile_maps_missing_device_to_stable_error(
    output_device_index: int | None,
) -> None:
    with pytest.raises(RuntimeError, match="audio_output_device_invalid"):
        resolve_output_device_profile(
            MissingOutputDevicePyAudio(),
            output_device_index=output_device_index,
            buffer_ms=40,
        )


class FakeTaskManager:
    def create_task(self, coro: Any, name: str = "", context: Any = None) -> MagicMock:
        if hasattr(coro, "close"):
            coro.close()
        return MagicMock()


@pytest.mark.asyncio
async def test_stable_output_opens_once_with_explicit_native_buffer() -> None:
    fake = FakePyAudio()
    params = LocalAudioTransportParams(
        audio_out_enabled=True,
        audio_out_sample_rate=48_000,
        audio_out_channels=1,
        output_device_index=2,
    )
    profile = resolve_output_device_profile(fake, output_device_index=2, buffer_ms=40)
    output = StableLocalAudioOutputTransport(fake, params, profile=profile)
    output._task_manager = FakeTaskManager()  # type: ignore[assignment]

    await output.start(StartFrame())
    await output.start(StartFrame())

    assert fake.open_kwargs == {
        "format": "pcm16",
        "channels": 1,
        "rate": 48_000,
        "frames_per_buffer": 1_920,
        "output": True,
        "output_device_index": 2,
        "start": False,
    }
    fake.stream.start_stream.assert_called_once_with()


@pytest.mark.asyncio
async def test_stable_output_closes_stream_when_start_fails() -> None:
    fake = FakePyAudio()
    fake.stream.start_stream.side_effect = OSError("start failed")
    params = LocalAudioTransportParams(
        audio_out_enabled=True,
        audio_out_sample_rate=48_000,
        audio_out_channels=1,
        output_device_index=2,
    )
    profile = resolve_output_device_profile(fake, output_device_index=2, buffer_ms=40)
    output = StableLocalAudioOutputTransport(fake, params, profile=profile)

    with pytest.raises(OSError, match="start failed"):
        await output.start(StartFrame())

    fake.stream.close.assert_called_once_with()
    assert output._out_stream is None


def test_stable_transport_initialization_updates_sample_rate_and_returns_stable_output() -> None:
    fake = FakePyAudio(sample_rate=48_000.0)
    params = LocalAudioTransportParams(
        audio_out_enabled=True,
        audio_out_sample_rate=24_000,
        audio_out_channels=1,
        output_device_index=2,
    )
    with patch("pyaudio.PyAudio", return_value=fake):
        transport = StableLocalAudioTransport(params, buffer_ms=40)
        assert transport._params.audio_out_sample_rate == 48_000
        output = transport.output()
        assert isinstance(output, StableLocalAudioOutputTransport)
        assert transport.output() is output


def test_stable_transport_terminates_pyaudio_when_device_resolution_fails() -> None:
    fake = MissingOutputDevicePyAudio()
    fake.terminate = MagicMock()
    params = LocalAudioTransportParams(
        audio_out_enabled=True,
        audio_out_sample_rate=24_000,
        audio_out_channels=1,
    )

    with (
        patch("pyaudio.PyAudio", return_value=fake),
        pytest.raises(RuntimeError, match="audio_output_device_invalid"),
    ):
        StableLocalAudioTransport(params, buffer_ms=40)

    fake.terminate.assert_called_once_with()
