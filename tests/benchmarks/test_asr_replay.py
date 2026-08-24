"""固定 PCM 切块与 1× 回放测试。"""

from __future__ import annotations

import csv
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from voice_realtime.asr.contracts import ASRCapabilities, ASREvent, ASRSessionContext
from voice_realtime.benchmarks.asr.manifest import (
    ASRRunManifest,
    CorpusManifest,
    CorpusSample,
    EnvironmentIdentity,
    RuntimeIdentity,
)
from voice_realtime.benchmarks.asr.replay import (
    BenchmarkRunResult,
    PCMFormat,
    ReplayMode,
    build_chunk_schedule,
    replay_schedule,
    run_benchmark,
)
from voice_realtime.meeting.models import NormalizedSegment, TranscriptWindow


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        assert delay >= 0
        self.now += delay


def _sender(target: list[bytes]) -> Callable[[bytes], Awaitable[None]]:
    async def send(payload: bytes) -> None:
        target.append(payload)

    return send


def test_schedule_uses_exact_20ms_s16le_chunks_and_audio_cursor() -> None:
    pcm = bytes(range(256)) * 5  # 1280 bytes = 40ms at 16kHz mono s16le

    chunks = build_chunk_schedule(pcm, pcm_format=PCMFormat(), chunk_ms=20)

    assert [len(chunk.payload) for chunk in chunks] == [640, 640]
    assert [chunk.audio_start_ms for chunk in chunks] == [0, 20]
    assert [chunk.audio_end_ms for chunk in chunks] == [20, 40]
    assert b"".join(chunk.payload for chunk in chunks) == pcm


def test_schedule_rejects_misaligned_pcm() -> None:
    with pytest.raises(ValueError, match="sample width"):
        build_chunk_schedule(b"\x00", pcm_format=PCMFormat(), chunk_ms=20)


@pytest.mark.asyncio
async def test_same_schedule_replays_identical_bytes_for_every_backend() -> None:
    pcm = bytes(range(128)) * 10
    schedule = build_chunk_schedule(pcm, pcm_format=PCMFormat(), chunk_ms=20)
    first: list[bytes] = []
    second: list[bytes] = []

    await replay_schedule(schedule, _sender(first), mode=ReplayMode.OFFLINE)
    await replay_schedule(schedule, _sender(second), mode=ReplayMode.OFFLINE)

    assert first == second
    assert b"".join(first) == pcm


@pytest.mark.asyncio
async def test_realtime_replay_uses_one_x_schedule_without_deadline_misses() -> None:
    pcm = bytes(range(128)) * 10
    schedule = build_chunk_schedule(pcm, pcm_format=PCMFormat(), chunk_ms=20)
    clock = FakeClock()
    sent: list[bytes] = []

    result = await replay_schedule(
        schedule,
        _sender(sent),
        mode=ReplayMode.REALTIME_1X,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result.sent_chunks == 2
    assert result.deadline_misses == 0
    assert result.audio_duration_ms == 40
    assert clock.now == pytest.approx(100.04)


class FakeTranscriber:
    backend_id = "wlk-qwen3-streaming"
    capabilities = ASRCapabilities(
        languages=frozenset({"zh"}),
        supports_partial=True,
        supports_segment_timestamps=True,
        supports_word_timestamps=False,
        supports_hotwords=False,
        supports_speaker_labels=True,
        supports_native_diarization=False,
        supports_eof_flush=True,
    )

    def __init__(
        self,
        context: ASRSessionContext,
        vendor_event_sink: Callable[[dict[str, object]], None],
    ) -> None:
        self.context = context
        self.vendor_event_sink = vendor_event_sink
        self.audio: list[bytes] = []
        self.connected = False
        self.closed = False
        self._queue: list[ASREvent] = []
        self._finished = False

    @property
    def uri(self) -> str:
        return "fake://asr"

    async def connect(self) -> None:
        self.connected = True
        self.vendor_event_sink({"type": "started"})
        self._queue.append(ASREvent(kind="ready"))

    async def send_audio(self, chunk: bytes) -> None:
        self.audio.append(chunk)

    async def events(self) -> AsyncIterator[ASREvent]:
        yielded = 0
        while not self._finished or yielded < len(self._queue):
            while yielded < len(self._queue):
                event = self._queue[yielded]
                yielded += 1
                yield event
            await __import__("asyncio").sleep(0)

    async def finish(self) -> TranscriptWindow:
        window = TranscriptWindow(
            source_epoch=self.context.source_epoch,
            segments=(
                NormalizedSegment(
                    order=0,
                    source_epoch=self.context.source_epoch,
                    speaker_key=f"epoch:{self.context.source_epoch}:speaker:0",
                    start_ms=0,
                    end_ms=40,
                    text="你好",
                ),
            ),
        )
        self._queue.append(ASREvent(kind="final", window=window))
        self._finished = True
        return window

    async def close(self) -> None:
        self.closed = True


def _benchmark_inputs(tmp_path: Path) -> tuple[ASRRunManifest, CorpusManifest, Path]:
    corpus_root = tmp_path / "corpus"
    corpus_root.mkdir()
    audio_path = corpus_root / "sample.pcm"
    audio_path.write_bytes(bytes(range(128)) * 10)
    sample = CorpusSample(
        sample_id="sample-001",
        audio_path="sample.pcm",
        audio_sha256="f01233826840f8e3b0ebce8b1a0e42bc7735848ecb149aca8e929c19d3140a29",
        duration_ms=40,
        scenario="near-field",
        language="zh",
        reference_raw="你好",
        reference_normalized="你好",
        license_or_consent="public",
    )
    corpus = CorpusManifest(
        corpus_version="test-v1",
        normalization_version="nfkc-casefold-punct-space-v1",
        samples=(sample,),
    )
    manifest = ASRRunManifest(
        run_id="test-run",
        git_commit="a" * 40,
        corpus_manifest_sha256="c" * 64,
        backend_id="wlk-qwen3-streaming",
        model_id="test/model",
        model_revision="revision-1",
        model_files_sha256={"model.bin": "b" * 64},
        runtime=RuntimeIdentity(name="fake", revision="revision-1"),
        device="cpu",
        dtype="float32",
        parameters={},
        environment=EnvironmentIdentity(
            host="test",
            memory_bytes=1,
            macos="test",
            python="3.12",
            torch="test",
        ),
        started_at=datetime(2026, 8, 24, tzinfo=UTC),
        status="planned",
    )
    return manifest, corpus, corpus_root


@pytest.mark.asyncio
async def test_runner_writes_complete_auditable_artifact_set(tmp_path: Path) -> None:
    manifest, corpus, corpus_root = _benchmark_inputs(tmp_path)
    output_dir = tmp_path / "output"
    transcribers: list[FakeTranscriber] = []

    def factory(
        _sample: CorpusSample,
        context: ASRSessionContext,
        vendor_event_sink: Callable[[dict[str, object]], None],
    ) -> FakeTranscriber:
        transcriber = FakeTranscriber(context, vendor_event_sink)
        transcribers.append(transcriber)
        return transcriber

    result = await run_benchmark(
        manifest=manifest,
        corpus=corpus,
        corpus_root=corpus_root,
        output_dir=output_dir,
        transcriber_factory=factory,
        mode=ReplayMode.OFFLINE,
    )

    assert result == BenchmarkRunResult(completed_samples=1, failed_samples=0)
    assert transcribers[0].connected is True
    assert transcribers[0].closed is True
    assert b"".join(transcribers[0].audio) == (corpus_root / "sample.pcm").read_bytes()
    assert {path.name for path in output_dir.iterdir()} == {
        "events.jsonl",
        "failures.jsonl",
        "hypotheses.jsonl",
        "manifest.json",
        "resources.csv",
        "summary.json",
        "vendor-events.jsonl",
    }
    assert not any(path.suffix in {".pcm", ".wav"} for path in output_dir.iterdir())
    written_manifest = json.loads((output_dir / "manifest.json").read_text())
    hypothesis = json.loads((output_dir / "hypotheses.jsonl").read_text())
    assert written_manifest["status"] == "completed"
    assert hypothesis["hypothesis_normalized"] == "你好"
    assert hypothesis["S"] == hypothesis["D"] == hypothesis["I"] == 0
    with (output_dir / "resources.csv").open(newline="", encoding="utf-8") as stream:
        resource_rows = list(csv.DictReader(stream))
    assert [row["audio_cursor_ms"] for row in resource_rows] == ["20", "40"]
    vendor_event = json.loads((output_dir / "vendor-events.jsonl").read_text())
    assert vendor_event["vendor_payload"] == {"type": "started"}
    assert output_dir.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in output_dir.iterdir())


@pytest.mark.asyncio
async def test_runner_records_audio_hash_failure_without_sending_pcm(tmp_path: Path) -> None:
    manifest, corpus, corpus_root = _benchmark_inputs(tmp_path)
    invalid_sample = corpus.samples[0].model_copy(update={"audio_sha256": "f" * 64})
    invalid_corpus = corpus.model_copy(update={"samples": (invalid_sample,)})
    output_dir = tmp_path / "output"
    created: list[FakeTranscriber] = []

    def factory(
        _sample: CorpusSample,
        context: ASRSessionContext,
        vendor_event_sink: Callable[[dict[str, object]], None],
    ) -> FakeTranscriber:
        transcriber = FakeTranscriber(context, vendor_event_sink)
        created.append(transcriber)
        return transcriber

    result = await run_benchmark(
        manifest=manifest,
        corpus=invalid_corpus,
        corpus_root=corpus_root,
        output_dir=output_dir,
        transcriber_factory=factory,
        mode=ReplayMode.OFFLINE,
    )

    assert result.failed_samples == 1
    assert created == []
    failure = json.loads((output_dir / "failures.jsonl").read_text())
    hypothesis = json.loads((output_dir / "hypotheses.jsonl").read_text())
    summary = json.loads((output_dir / "summary.json").read_text())
    assert failure["error_code"] == "AUDIO_HASH_MISMATCH"
    assert hypothesis["error_status"] == "AUDIO_HASH_MISMATCH"
    assert summary["samples"] == 1
    assert summary["failure_rate"] == 1.0


@pytest.mark.asyncio
async def test_runner_rejects_symlink_that_escapes_corpus_root(tmp_path: Path) -> None:
    manifest, corpus, corpus_root = _benchmark_inputs(tmp_path)
    outside = tmp_path / "outside.pcm"
    outside.write_bytes((corpus_root / "sample.pcm").read_bytes())
    (corpus_root / "sample.pcm").unlink()
    (corpus_root / "sample.pcm").symlink_to(outside)
    output_dir = tmp_path / "output"

    result = await run_benchmark(
        manifest=manifest,
        corpus=corpus,
        corpus_root=corpus_root,
        output_dir=output_dir,
        transcriber_factory=lambda sample, context, vendor_event_sink: FakeTranscriber(
            context, vendor_event_sink
        ),
        mode=ReplayMode.OFFLINE,
    )

    assert result.failed_samples == 1
    failure = json.loads((output_dir / "failures.jsonl").read_text())
    assert failure["error_code"] == "AUDIO_PATH_ESCAPE"


@pytest.mark.asyncio
async def test_runner_rejects_backend_identity_mismatch(tmp_path: Path) -> None:
    manifest, corpus, corpus_root = _benchmark_inputs(tmp_path)
    output_dir = tmp_path / "output"

    def factory(
        sample: CorpusSample,
        context: ASRSessionContext,
        vendor_event_sink: Callable[[dict[str, object]], None],
    ) -> FakeTranscriber:
        del sample
        transcriber = FakeTranscriber(context, vendor_event_sink)
        transcriber.backend_id = "wrong-backend"
        return transcriber

    result = await run_benchmark(
        manifest=manifest,
        corpus=corpus,
        corpus_root=corpus_root,
        output_dir=output_dir,
        transcriber_factory=factory,
        mode=ReplayMode.OFFLINE,
    )

    assert result.failed_samples == 1
    failure = json.loads((output_dir / "failures.jsonl").read_text())
    assert failure["error_code"] == "BACKEND_IDENTITY_MISMATCH"


@pytest.mark.asyncio
async def test_runner_rejects_asymmetric_reference_normalization(tmp_path: Path) -> None:
    manifest, corpus, corpus_root = _benchmark_inputs(tmp_path)
    invalid_sample = corpus.samples[0].model_copy(update={"reference_normalized": "错误"})
    output_dir = tmp_path / "output"

    result = await run_benchmark(
        manifest=manifest,
        corpus=corpus.model_copy(update={"samples": (invalid_sample,)}),
        corpus_root=corpus_root,
        output_dir=output_dir,
        transcriber_factory=lambda sample, context, vendor_event_sink: FakeTranscriber(
            context, vendor_event_sink
        ),
        mode=ReplayMode.OFFLINE,
    )

    assert result.failed_samples == 1
    failure = json.loads((output_dir / "failures.jsonl").read_text())
    assert failure["error_code"] == "REFERENCE_NORMALIZATION_MISMATCH"
