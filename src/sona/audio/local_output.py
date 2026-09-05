"""Sona 稳定本机音频输出适配器。

以当前输出设备的原生采样率和显式 40ms 缓冲打开 PyAudio 输出流，
降低长播报期间 CoreAudio overload 与爆音风险。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pyaudio  # type: ignore[import-untyped]
from pipecat.frames.frames import StartFrame
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.local.audio import (
    LocalAudioOutputTransport,
    LocalAudioTransport,
    LocalAudioTransportParams,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OutputDeviceProfile:
    device_index: int
    device_name: str
    sample_rate: int
    frames_per_buffer: int
    buffer_ms: int


def resolve_output_device_profile(
    py_audio: Any,
    *,
    output_device_index: int | None,
    buffer_ms: int,
) -> OutputDeviceProfile:
    try:
        info = (
            py_audio.get_default_output_device_info()
            if output_device_index is None
            else py_audio.get_device_info_by_index(output_device_index)
        )
        device_index = int(info["index"])
        sample_rate = round(float(info["defaultSampleRate"]))
        channels = int(info["maxOutputChannels"])
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise RuntimeError("audio_output_device_invalid") from exc
    if channels < 1 or not 8_000 <= sample_rate <= 192_000:
        raise RuntimeError("audio_output_device_invalid")
    try:
        py_audio.is_format_supported(
            sample_rate,
            output_device=device_index,
            output_channels=1,
            output_format=pyaudio.paInt16,
        )
    except ValueError as exc:
        raise RuntimeError("audio_output_format_unsupported") from exc
    return OutputDeviceProfile(
        device_index=device_index,
        device_name=str(info.get("name", "unknown")),
        sample_rate=sample_rate,
        frames_per_buffer=round(sample_rate * buffer_ms / 1_000),
        buffer_ms=buffer_ms,
    )


class StableLocalAudioOutputTransport(LocalAudioOutputTransport):
    def __init__(
        self,
        py_audio: Any,
        params: LocalAudioTransportParams,
        *,
        profile: OutputDeviceProfile,
    ) -> None:
        super().__init__(py_audio, params)
        self._profile = profile

    async def start(self, frame: StartFrame) -> None:
        await BaseOutputTransport.start(self, frame)
        if self._out_stream:
            return
        stream = None
        try:
            stream = self._py_audio.open(
                format=self._py_audio.get_format_from_width(2),
                channels=self._params.audio_out_channels,
                rate=self._profile.sample_rate,
                frames_per_buffer=self._profile.frames_per_buffer,
                output=True,
                output_device_index=self._profile.device_index,
                start=False,
            )
            stream.start_stream()
        except Exception:
            if stream is not None:
                stream.close()
            raise
        self._out_stream = stream
        logger.info(
            "audio-output: device=%r index=%d rate=%d buffer=%d frames (%dms)",
            self._profile.device_name,
            self._profile.device_index,
            self._profile.sample_rate,
            self._profile.frames_per_buffer,
            self._profile.buffer_ms,
        )
        await self.set_transport_ready(frame)


class StableLocalAudioTransport(LocalAudioTransport):
    def __init__(self, params: LocalAudioTransportParams, *, buffer_ms: int) -> None:
        super().__init__(params)
        try:
            self._profile = resolve_output_device_profile(
                self._pyaudio,
                output_device_index=params.output_device_index,
                buffer_ms=buffer_ms,
            )
        except Exception:
            self._pyaudio.terminate()
            raise
        self._params.audio_out_sample_rate = self._profile.sample_rate

    def output(self) -> StableLocalAudioOutputTransport:
        if not isinstance(self._output, StableLocalAudioOutputTransport):
            self._output = StableLocalAudioOutputTransport(
                self._pyaudio,
                self._params,
                profile=self._profile,
            )
        return self._output


__all__ = [
    "OutputDeviceProfile",
    "StableLocalAudioOutputTransport",
    "StableLocalAudioTransport",
    "resolve_output_device_profile",
]
