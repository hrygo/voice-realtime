#!/usr/bin/env python3
"""显式人工物理输出 capture 冒烟；自动测试不得执行确认参数。"""

from __future__ import annotations

import argparse
import asyncio
import math
import struct
import sys
import tempfile
import time
from array import array
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from voice_realtime.audio.output_source import (
    AudioCaptureClient,
    AudioCaptureError,
    HelperSupervisor,
)
from voice_realtime.config import AudioCaptureSettings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HELPER = (
    PROJECT_ROOT
    / "build/vr-audio-capture/vr-audio-capture.app/Contents/MacOS/vr-audio-capture-helper"
)


@dataclass(frozen=True, slots=True)
class CaptureStats:
    frames: int
    nonzero_frames: int
    sequence_gaps: int
    peak_rms: float
    first_sequence: int | None
    last_sequence: int | None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "人工验证默认物理输出设备 capture；会触发 macOS 系统音频录制权限。"
        )
    )
    parser.add_argument(
        "--i-understand-this-will-request-system-audio-permission",
        action="store_true",
        required=True,
        help="确认主动执行真实 capture 并允许出现系统权限弹窗",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=30.0,
        help="采集秒数，范围 5~120，默认 30",
    )
    parser.add_argument(
        "--helper",
        type=Path,
        default=DEFAULT_HELPER,
        help="已构建 .app 内的 Helper 可执行文件",
    )
    args = parser.parse_args()
    if not 5 <= args.duration <= 120:
        parser.error("--duration must be between 5 and 120 seconds")
    return args


def _play_test_tone(duration: float) -> None:
    import pyaudio  # type: ignore[import-untyped]

    sample_rate = 48_000
    chunk_frames = 480
    amplitude = 0.18 * 32_767
    frequency = 440.0
    audio = pyaudio.PyAudio()
    stream = audio.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=sample_rate,
        output=True,
        frames_per_buffer=chunk_frames,
    )
    try:
        deadline = time.monotonic() + min(duration, 3.0)
        frame_index = 0
        while time.monotonic() < deadline:
            samples = array(
                "h",
                (
                    int(
                        amplitude
                        * math.sin(2 * math.pi * frequency * frame_index / sample_rate)
                    )
                    for frame_index in range(frame_index, frame_index + chunk_frames)
                ),
            )
            frame_index += chunk_frames
            if sys.byteorder != "little":
                samples.byteswap()
            stream.write(samples.tobytes(), exception_on_underflow=False)
    finally:
        stream.stop_stream()
        stream.close()
        audio.terminate()


def _frame_rms(pcm: bytes) -> float:
    samples = struct.unpack("<512h", pcm)
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32_768


def _runtime_artifacts(runtime_dir: Path) -> list[Path]:
    return [
        path
        for path in runtime_dir.rglob("*")
        if path.suffix.lower() in {".pcm", ".wav", ".sock", ".socket"}
    ]


async def _collect_frames(
    client: AudioCaptureClient,
    duration: float,
) -> CaptureStats:
    iterator = client.pcm_messages().__aiter__()
    deadline = asyncio.get_running_loop().time() + duration
    frames = 0
    nonzero_frames = 0
    sequence_gaps = 0
    peak_rms = 0.0
    first_sequence: int | None = None
    previous_sequence: int | None = None
    last_frame_at = asyncio.get_running_loop().time()

    while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
        try:
            frame = await asyncio.wait_for(anext(iterator), timeout=min(5.0, remaining))
        except TimeoutError as exc:
            now = asyncio.get_running_loop().time()
            if now >= deadline and now - last_frame_at < 5.0:
                break
            raise RuntimeError("5 秒内未收到物理输出 PCM") from exc
        last_frame_at = asyncio.get_running_loop().time()
        frames += 1
        first_sequence = frame.sequence if first_sequence is None else first_sequence
        if previous_sequence is not None and frame.sequence != previous_sequence + 1:
            sequence_gaps += 1
        previous_sequence = frame.sequence
        rms = _frame_rms(frame.pcm)
        peak_rms = max(peak_rms, rms)
        if rms >= 0.005:
            nonzero_frames += 1

    return CaptureStats(
        frames=frames,
        nonzero_frames=nonzero_frames,
        sequence_gaps=sequence_gaps,
        peak_rms=peak_rms,
        first_sequence=first_sequence,
        last_sequence=previous_sequence,
    )


async def _run(args: argparse.Namespace) -> CaptureStats:
    helper = args.helper.expanduser().resolve(strict=True)
    capture_id = uuid4()
    with tempfile.TemporaryDirectory(prefix="vrac-manual-", dir="/tmp") as directory:
        runtime_dir = Path(directory)
        settings = AudioCaptureSettings(
            _env_file=None,
            enabled=True,
            helper_executable=helper,
            runtime_dir=runtime_dir,
            startup_timeout_secs=10,
            command_timeout_secs=30,
            queue_size=128,
            restart_attempts=0,
        )
        supervisor = HelperSupervisor(settings)
        client: AudioCaptureClient | None = None
        committed = False
        tone_task: asyncio.Task[None] | None = None
        try:
            client = await supervisor.start_client()
            devices = await client.list_devices()
            default_devices = [device for device in devices if device.get("is_default") is True]
            if len(default_devices) != 1:
                raise RuntimeError("默认物理输出设备不可用或不唯一")
            selected = default_devices[0]
            print(
                {
                    "selected_label": selected.get("label"),
                    "transport": selected.get("transport"),
                }
            )
            await client.prepare_capture(capture_id, follow_default_output=True)
            await client.commit_capture(capture_id)
            committed = True
            tone_task = asyncio.create_task(asyncio.to_thread(_play_test_tone, args.duration))
            stats = await _collect_frames(client, args.duration)
            await tone_task
            tone_task = None
            if stats.nonzero_frames == 0:
                raise RuntimeError("未检测到测试音，请确认默认输出设备及系统音量")
            if stats.sequence_gaps or client.dropped_frames:
                raise RuntimeError("检测到 PCM 序列间隙或客户端丢帧")
            return stats
        finally:
            if tone_task is not None:
                tone_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await tone_task
            if client is not None:
                with suppress(AudioCaptureError):
                    if committed:
                        await client.stop_capture(capture_id)
                    else:
                        await client.abort_capture(capture_id)
            await supervisor.stop()
            leftovers = await asyncio.to_thread(_runtime_artifacts, runtime_dir)
            if leftovers:
                raise RuntimeError("采集结束后仍有音频或 Socket 运行产物")


def main() -> int:
    args = _parse_args()
    try:
        stats = asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except (AudioCaptureError, OSError, RuntimeError, ValueError) as exc:
        print(f"capture smoke failed: {exc}", file=sys.stderr)
        return 1
    print(
        {
            "frames": stats.frames,
            "nonzero_frames": stats.nonzero_frames,
            "sequence_gaps": stats.sequence_gaps,
            "peak_rms": round(stats.peak_rms, 4),
            "first_sequence": stats.first_sequence,
            "last_sequence": stats.last_sequence,
            "residual_audio_or_socket_files": 0,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
