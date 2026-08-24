"""固定 s16le PCM 切块与离线/1× 实时回放。"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import resource
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from voice_realtime.asr.contracts import ASREvent, ASRSessionContext, StreamingTranscriber
from voice_realtime.benchmarks.asr.manifest import (
    ASRRunManifest,
    BenchmarkSample,
    BlindHypothesisRecord,
    CorpusInputManifest,
    CorpusReferenceManifest,
    HypothesisRecord,
    resolve_relative_file,
    sha256_file,
    write_run_manifest,
)
from voice_realtime.benchmarks.asr.metrics import (
    PRIMARY_NORMALIZATION_VERSION,
    MetricStatus,
    character_error_rate,
    normalize_primary_text,
    percentile,
    realtime_factor,
    stratified_cluster_bootstrap_difference,
)
from voice_realtime.meeting.models import TranscriptWindow


class ReplayMode(StrEnum):
    """同一 chunk schedule 的两种发送时序。"""

    OFFLINE = "offline"
    REALTIME_1X = "realtime-1x"


@dataclass(frozen=True)
class PCMFormat:
    """benchmark 唯一允许的 PCM 格式。"""

    sample_rate: int = 16_000
    channels: int = 1
    sample_width_bytes: int = 2

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError("sample_rate 必须为正数")
        if self.channels <= 0:
            raise ValueError("channels 必须为正数")
        if self.sample_width_bytes <= 0:
            raise ValueError("sample_width_bytes 必须为正数")

    @property
    def bytes_per_frame(self) -> int:
        """返回一个采样时刻的 byte 数。"""
        return self.channels * self.sample_width_bytes


@dataclass(frozen=True)
class PCMChunk:
    """带确定性音频游标的一个 PCM chunk。"""

    index: int
    audio_start_ms: int
    audio_end_ms: int
    payload: bytes


@dataclass(frozen=True)
class ReplayResult:
    """一次 chunk schedule 发送的可观测结果。"""

    sent_chunks: int
    audio_duration_ms: int
    wall_time_ms: float
    deadline_misses: int


@dataclass(frozen=True)
class BenchmarkRunResult:
    """一次实验臂执行完成后的样本计数。"""

    completed_samples: int
    failed_samples: int


class BenchmarkSampleError(RuntimeError):
    """保留稳定错误码的单样本失败。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BenchmarkTranscriberFactory(Protocol):
    """按样本和应用时间轴创建独立 transcriber。"""

    def __call__(
        self,
        sample: BenchmarkSample,
        context: ASRSessionContext,
        vendor_event_sink: Callable[[Mapping[str, object]], None],
    ) -> StreamingTranscriber: ...


MonotonicClock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]
SendAudio = Callable[[bytes], Awaitable[None]]
ProcessTreeRSSSampler = Callable[[tuple[int, ...]], int]
_RESOURCE_RSS_SAMPLE_INTERVAL_SECS = 5.0


def build_chunk_schedule(
    pcm: bytes,
    *,
    pcm_format: PCMFormat,
    chunk_ms: int = 20,
) -> tuple[PCMChunk, ...]:
    """把原始 PCM bytes 切成可在不同后端间复用的不可变 schedule。"""
    if chunk_ms <= 0:
        raise ValueError("chunk_ms 必须为正数")
    if len(pcm) % pcm_format.bytes_per_frame != 0:
        raise ValueError("PCM byte length must align with sample width and channels")
    frames_per_chunk_numerator = pcm_format.sample_rate * chunk_ms
    if frames_per_chunk_numerator % 1000 != 0:
        raise ValueError("chunk_ms 不能在给定 sample_rate 下形成整数采样数")
    frames_per_chunk = frames_per_chunk_numerator // 1000
    bytes_per_chunk = frames_per_chunk * pcm_format.bytes_per_frame
    chunks: list[PCMChunk] = []
    for index, offset in enumerate(range(0, len(pcm), bytes_per_chunk)):
        payload = pcm[offset : offset + bytes_per_chunk]
        start_frame = offset // pcm_format.bytes_per_frame
        end_frame = start_frame + len(payload) // pcm_format.bytes_per_frame
        chunks.append(
            PCMChunk(
                index=index,
                audio_start_ms=round(start_frame * 1000 / pcm_format.sample_rate),
                audio_end_ms=round(end_frame * 1000 / pcm_format.sample_rate),
                payload=payload,
            )
        )
    return tuple(chunks)


async def replay_schedule(
    schedule: Sequence[PCMChunk],
    send_audio: SendAudio,
    *,
    mode: ReplayMode,
    monotonic: MonotonicClock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
) -> ReplayResult:
    """按离线或 wall-clock 1× 时序发送既定 schedule。"""
    started_at = monotonic()
    deadline_misses = 0
    for chunk in schedule:
        if mode is ReplayMode.REALTIME_1X:
            deadline = started_at + chunk.audio_end_ms / 1000
            remaining = deadline - monotonic()
            if remaining > 0:
                await sleep(remaining)
            elif remaining < -1e-6:
                deadline_misses += 1
        await send_audio(chunk.payload)
    ended_at = monotonic()
    duration_ms = schedule[-1].audio_end_ms if schedule else 0
    return ReplayResult(
        sent_chunks=len(schedule),
        audio_duration_ms=duration_ms,
        wall_time_ms=max(0.0, (ended_at - started_at) * 1000),
        deadline_misses=deadline_misses,
    )


def _window_text(window: TranscriptWindow) -> str:
    confirmed = " ".join(segment.text for segment in window.segments).strip()
    return confirmed or window.partial


def _event_row(
    *,
    sample_id: str,
    backend_id: str,
    event: ASREvent,
    audio_cursor_ms: int,
    arrival_monotonic_ms: float,
) -> dict[str, object]:
    window = event.window
    return {
        "sample_id": sample_id,
        "audio_cursor_ms": audio_cursor_ms,
        "arrival_monotonic_ms": arrival_monotonic_ms,
        "event_kind": event.kind,
        "text": _window_text(window) if window is not None else "",
        "is_final": event.kind == "final",
        "source_epoch": window.source_epoch if window is not None else None,
        "segments": (
            [segment.model_dump(mode="json") for segment in window.segments]
            if window is not None
            else []
        ),
        "backend_id": backend_id,
        "error_code": event.error_code,
        "error_message": event.error_message,
    }


async def _collect_events(
    *,
    transcriber: StreamingTranscriber,
    sample_id: str,
    audio_cursor: list[int],
    run_started_at: float,
    monotonic: MonotonicClock,
    event_sink: Callable[[Mapping[str, object]], None],
) -> None:
    async for event in transcriber.events():
        event_sink(
            _event_row(
                sample_id=sample_id,
                backend_id=transcriber.backend_id,
                event=event,
                audio_cursor_ms=audio_cursor[0],
                arrival_monotonic_ms=max(0.0, (monotonic() - run_started_at) * 1000),
            )
        )


class _JsonlSink:
    """复用单个缓冲文件句柄，避免事件热路径反复 open/close。"""

    def __init__(self, path: Path) -> None:
        self._stream = path.open("a", encoding="utf-8", buffering=1024 * 1024)

    def write(self, row: Mapping[str, object]) -> None:
        self._stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def flush(self) -> None:
        self._stream.flush()

    def close(self) -> None:
        self._stream.close()


def _max_rss_bytes() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes; Linux reports KiB.
    return int(usage if __import__("sys").platform == "darwin" else usage * 1024)


def _resource_process_ids(transcriber: StreamingTranscriber) -> tuple[int, ...]:
    candidate = getattr(transcriber, "resource_process_ids", ())
    if not isinstance(candidate, tuple) or any(
        not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
        for pid in candidate
    ):
        raise ValueError("resource_process_ids must be a tuple of positive process IDs")
    return tuple(dict.fromkeys((os.getpid(), *candidate)))


def _process_tree_rss_bytes(process_ids: tuple[int, ...]) -> int:
    """读取当前进程集合 RSS；单进程保持既有无子进程开销路径。"""
    if process_ids == (os.getpid(),):
        return _max_rss_bytes()
    completed = subprocess.run(
        ["/bin/ps", "-o", "rss=", "-p", ",".join(str(pid) for pid in process_ids)],
        check=True,
        capture_output=True,
        text=True,
        timeout=2.0,
    )
    rss_kib = [int(value) for value in completed.stdout.split()]
    if len(rss_kib) != len(process_ids):
        raise RuntimeError("resource process exited before RSS sampling completed")
    return sum(rss_kib) * 1024


async def _run_sample(
    *,
    sample: BenchmarkSample,
    source_epoch: int,
    corpus_root: Path,
    transcriber_factory: BenchmarkTranscriberFactory,
    mode: ReplayMode,
    chunk_ms: int,
    final_timeout_secs: float,
    run_started_at: float,
    monotonic: MonotonicClock,
    expected_backend_id: str,
    event_sink: Callable[[Mapping[str, object]], None],
    vendor_sink: Callable[[Mapping[str, object]], None],
    process_tree_rss_sampler: ProcessTreeRSSSampler,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    try:
        audio_path = resolve_relative_file(corpus_root, sample.audio_path)
    except (FileNotFoundError, ValueError) as exc:
        error_code = "AUDIO_PATH_ESCAPE" if isinstance(exc, ValueError) else "AUDIO_NOT_FOUND"
        raise BenchmarkSampleError(error_code, f"invalid audio path: {sample.sample_id}") from None
    expected_audio_bytes = sample.duration_ms * PCMFormat().sample_rate // 1000
    expected_audio_bytes *= PCMFormat().bytes_per_frame
    if audio_path.stat().st_size != expected_audio_bytes:
        raise BenchmarkSampleError(
            "AUDIO_SIZE_MISMATCH",
            f"audio byte length mismatch: {sample.sample_id}",
        )
    if sha256_file(audio_path) != sample.audio_sha256:
        raise BenchmarkSampleError(
            "AUDIO_HASH_MISMATCH",
            f"audio SHA-256 mismatch: {sample.sample_id}",
        )
    pcm = audio_path.read_bytes()
    schedule = build_chunk_schedule(pcm, pcm_format=PCMFormat(), chunk_ms=chunk_ms)
    actual_duration_ms = schedule[-1].audio_end_ms if schedule else 0
    if actual_duration_ms != sample.duration_ms:
        raise BenchmarkSampleError(
            "AUDIO_DURATION_MISMATCH",
            f"audio duration mismatch: {sample.sample_id}",
        )

    context = ASRSessionContext(
        source_epoch=source_epoch,
        offset_ms=0,
        purpose="subtitles",
    )
    def sample_vendor_sink(payload: Mapping[str, object]) -> None:
        vendor_sink(
            {
                "sample_id": sample.sample_id,
                "backend_id": expected_backend_id,
                "vendor_payload": payload,
            }
        )

    transcriber = transcriber_factory(sample, context, sample_vendor_sink)
    audio_cursor = [0]
    resource_rows: list[dict[str, object]] = []
    next_resource_cursor_ms = 0
    event_task: asyncio.Task[None] | None = None
    sample_started_at = monotonic()
    finalization_started_at = sample_started_at
    finalization_completed_at = sample_started_at
    final_window: TranscriptWindow | None = None
    replay_result: ReplayResult | None = None
    resource_process_ids: tuple[int, ...] = (os.getpid(),)
    process_tree_rss_bytes = _max_rss_bytes()
    process_tree_rss_peak_bytes = process_tree_rss_bytes
    process_tree_rss_sampled_at = sample_started_at
    final_process_tree_rss_bytes: int | None = None
    try:
        if transcriber.backend_id != expected_backend_id:
            raise BenchmarkSampleError(
                "BACKEND_IDENTITY_MISMATCH",
                f"backend identity mismatch: {sample.sample_id}",
            )
        await transcriber.connect()
        resource_process_ids = _resource_process_ids(transcriber)
        process_tree_rss_bytes = process_tree_rss_sampler(resource_process_ids)
        process_tree_rss_peak_bytes = process_tree_rss_bytes
        process_tree_rss_sampled_at = monotonic()
        event_task = asyncio.create_task(
            _collect_events(
                transcriber=transcriber,
                sample_id=sample.sample_id,
                audio_cursor=audio_cursor,
                run_started_at=run_started_at,
                monotonic=monotonic,
                event_sink=event_sink,
            )
        )

        async def send_audio(payload: bytes) -> None:
            nonlocal next_resource_cursor_ms, process_tree_rss_bytes
            nonlocal process_tree_rss_peak_bytes, process_tree_rss_sampled_at
            await transcriber.send_audio(payload)
            audio_cursor[0] += round(
                len(payload) * 1000 / (PCMFormat().bytes_per_frame * PCMFormat().sample_rate)
            )
            if audio_cursor[0] >= next_resource_cursor_ms:
                sampled_at = monotonic()
                if (
                    sampled_at - process_tree_rss_sampled_at
                    >= _RESOURCE_RSS_SAMPLE_INTERVAL_SECS
                ):
                    process_tree_rss_bytes = process_tree_rss_sampler(
                        resource_process_ids
                    )
                    process_tree_rss_peak_bytes = max(
                        process_tree_rss_peak_bytes,
                        process_tree_rss_bytes,
                    )
                    process_tree_rss_sampled_at = sampled_at
                resource_rows.append(
                    {
                        "sample_id": sample.sample_id,
                        "audio_cursor_ms": audio_cursor[0],
                        "arrival_monotonic_ms": max(
                            0.0,
                            (monotonic() - run_started_at) * 1000,
                        ),
                        "process_cpu_seconds": time.process_time(),
                        "max_rss_bytes": max(
                            _max_rss_bytes(), process_tree_rss_peak_bytes
                        ),
                        "process_tree_rss_bytes": process_tree_rss_bytes,
                        "resource_process_count": len(resource_process_ids),
                    }
                )
                next_resource_cursor_ms = audio_cursor[0] + 1000

        replay_result = await replay_schedule(
            schedule,
            send_audio,
            mode=mode,
            monotonic=monotonic,
        )
        finalization_started_at = monotonic()
        async with asyncio.timeout(final_timeout_secs):
            final_window = await transcriber.finish()
        finalization_completed_at = monotonic()
        final_process_tree_rss_bytes = process_tree_rss_sampler(resource_process_ids)
        process_tree_rss_peak_bytes = max(
            process_tree_rss_peak_bytes,
            final_process_tree_rss_bytes,
        )
    finally:
        await transcriber.close()
        if event_task is not None:
            try:
                async with asyncio.timeout(1.0):
                    await event_task
            except TimeoutError:
                event_task.cancel()
                await asyncio.gather(event_task, return_exceptions=True)

    if replay_result is None or final_window is None:
        raise BenchmarkSampleError("ASR_FINAL_MISSING", "ASR did not return a final window")
    wall_time_ms = max(0.0, (finalization_completed_at - sample_started_at) * 1000)
    finalization_latency_ms = max(
        0.0,
        (finalization_completed_at - finalization_started_at) * 1000,
    )
    hypothesis_raw = _window_text(final_window)
    hypothesis_normalized = normalize_primary_text(hypothesis_raw)
    rtf = realtime_factor(wall_time_ms=wall_time_ms, audio_duration_ms=sample.duration_ms)
    hypothesis: dict[str, object] = {
        "sample_id": sample.sample_id,
        "scenario": sample.scenario,
        "hypothesis_raw": hypothesis_raw,
        "hypothesis_normalized": hypothesis_normalized,
        "language": sample.language,
        "duration_ms": sample.duration_ms,
        "wall_time_ms": wall_time_ms,
        "rtf": rtf.value,
        "deadline_misses": replay_result.deadline_misses,
        "finalization_latency_ms": finalization_latency_ms,
        "error_status": None,
    }
    if not resource_rows or resource_rows[-1]["audio_cursor_ms"] != sample.duration_ms:
        process_tree_rss_bytes = (
            final_process_tree_rss_bytes
            if final_process_tree_rss_bytes is not None
            else process_tree_rss_sampler(resource_process_ids)
        )
        resource_rows.append(
            {
                "sample_id": sample.sample_id,
                "audio_cursor_ms": sample.duration_ms,
                "arrival_monotonic_ms": max(0.0, (monotonic() - run_started_at) * 1000),
                "process_cpu_seconds": time.process_time(),
                "max_rss_bytes": max(
                    _max_rss_bytes(), process_tree_rss_peak_bytes
                ),
                "process_tree_rss_bytes": process_tree_rss_bytes,
                "resource_process_count": len(resource_process_ids),
            }
        )
    elif final_process_tree_rss_bytes is not None:
        resource_rows[-1]["process_tree_rss_bytes"] = final_process_tree_rss_bytes
        resource_rows[-1]["max_rss_bytes"] = max(
            cast(int, resource_rows[-1]["max_rss_bytes"]),
            process_tree_rss_peak_bytes,
        )
    return hypothesis, resource_rows


def load_blind_hypotheses(path: Path) -> list[dict[str, object]]:
    """读取结构上不含 reference/CER 的盲推理输出。"""
    return _load_hypothesis_records(path, BlindHypothesisRecord)


def _load_hypothesis_records(
    path: Path,
    record_type: type[BlindHypothesisRecord] | type[HypothesisRecord],
) -> list[dict[str, object]]:
    if path.stat().st_size > 256 * 1024 * 1024:
        raise ValueError("hypotheses JSONL exceeds 256 MiB")
    rows: list[dict[str, object]] = []
    sample_ids: set[str] = set()
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"hypotheses line {line_number} must be an object")
            try:
                record = record_type.model_validate(value)
            except ValueError as exc:
                raise ValueError(f"invalid hypotheses line {line_number}: {exc}") from exc
            row = cast(dict[str, object], record.model_dump(mode="json", by_alias=True))
            if record.sample_id in sample_ids:
                raise ValueError(f"duplicate sample_id in hypotheses: {record.sample_id}")
            sample_ids.add(record.sample_id)
            rows.append(row)
    return rows


def load_hypotheses(path: Path) -> list[dict[str, object]]:
    """读取开盲后带 reference 与指标的评分输出。"""
    return _load_hypothesis_records(path, HypothesisRecord)


def score_blind_hypotheses(
    blind_rows: Sequence[Mapping[str, object]],
    references: CorpusReferenceManifest,
) -> list[dict[str, object]]:
    """开盲后按完整 sample_id 集合一对一生成独立评分记录。"""
    blind = {
        str(row["sample_id"]): BlindHypothesisRecord.model_validate(row)
        for row in blind_rows
    }
    reference_by_id = {reference.sample_id: reference for reference in references.samples}
    if set(blind) != set(reference_by_id):
        raise ValueError("blind hypotheses and references must have identical sample IDs")
    scored: list[dict[str, object]] = []
    for sample_id in sorted(blind):
        row = blind[sample_id]
        reference = reference_by_id[sample_id]
        if normalize_primary_text(reference.reference_raw) != reference.reference_normalized:
            raise ValueError(f"reference normalization mismatch: {sample_id}")
        values: dict[str, object] = {
            **row.model_dump(mode="json"),
            "reference_raw": reference.reference_raw,
            "reference_normalized": reference.reference_normalized,
        }
        if row.error_status is not None:
            values.update(
                {
                    "S": None,
                    "D": None,
                    "I": None,
                    "N": None,
                    "cer_status": MetricStatus.MISSING.value,
                    "cer": None,
                }
            )
        else:
            cer = character_error_rate(
                reference.reference_normalized,
                row.hypothesis_normalized,
            )
            values.update(
                {
                    "S": cer.substitutions,
                    "D": cer.deletions,
                    "I": cer.insertions,
                    "N": cer.reference_tokens,
                    "cer_status": cer.status.value,
                    "cer": cer.value,
                }
            )
        record = HypothesisRecord.model_validate(values)
        scored.append(cast(dict[str, object], record.model_dump(mode="json", by_alias=True)))
    return scored


def write_scored_hypotheses(
    path: Path,
    rows: Sequence[Mapping[str, object]],
) -> None:
    """另写开盲评分产物，拒绝覆盖原始盲 hypotheses。"""
    if path.exists():
        raise FileExistsError(f"scored hypotheses already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            for row in rows:
                record = HypothesisRecord.model_validate(row)
                stream.write(record.model_dump_json(by_alias=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if __import__("math").isfinite(number) else None


def score_hypotheses(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """从逐样本记录生成 macro/micro、分层、失败率与性能摘要。"""
    cer_values: list[float] = []
    rtf_values: list[float] = []
    scenarios: dict[str, list[float]] = {}
    total_errors = 0
    total_reference = 0
    failures = 0
    for row in rows:
        if row.get("error_status") is not None:
            failures += 1
        cer = _finite_number(row.get("cer"))
        if row.get("cer_status") == MetricStatus.SUPPORTED.value and cer is not None:
            cer_values.append(cer)
            scenario = str(row.get("scenario") or "unknown")
            scenarios.setdefault(scenario, []).append(cer)
        counts = [row.get(name) for name in ("S", "D", "I", "N")]
        if all(isinstance(value, int) and not isinstance(value, bool) for value in counts):
            substitutions, deletions, insertions, reference_tokens = cast(list[int], counts)
            if reference_tokens > 0:
                total_errors += substitutions + deletions + insertions
                total_reference += reference_tokens
        rtf = _finite_number(row.get("rtf"))
        if rtf is not None:
            rtf_values.append(rtf)
    sample_count = len(rows)
    scenario_macro = {
        scenario: sum(values) / len(values) for scenario, values in sorted(scenarios.items())
    }
    return {
        "schema_version": "1.0",
        "samples": sample_count,
        "failures": failures,
        "failure_rate": failures / sample_count if sample_count else None,
        "macro_cer": (
            sum(scenario_macro.values()) / len(scenario_macro) if scenario_macro else None
        ),
        "sample_macro_cer": sum(cer_values) / len(cer_values) if cer_values else None,
        "micro_cer": total_errors / total_reference if total_reference else None,
        "scenario_macro_cer": scenario_macro,
        "rtf_p50": percentile(rtf_values, 50).value,
        "rtf_p95": percentile(rtf_values, 95).value,
    }


def compare_hypotheses(
    baseline_rows: Sequence[Mapping[str, object]],
    candidate_rows: Sequence[Mapping[str, object]],
    *,
    iterations: int = 10_000,
    seed: int = 0,
) -> dict[str, object]:
    """按 sample_id 对齐并比较候选减基线 CER。"""
    def supported_rows(
        rows: Sequence[Mapping[str, object]],
    ) -> dict[str, tuple[str, float]]:
        supported: dict[str, tuple[str, float]] = {}
        for row in rows:
            value = _finite_number(row.get("cer"))
            sample_id = row.get("sample_id")
            scenario = row.get("scenario")
            if (
                row.get("cer_status") != MetricStatus.SUPPORTED.value
                or value is None
                or not isinstance(sample_id, str)
                or not isinstance(scenario, str)
            ):
                continue
            if sample_id in supported:
                raise ValueError(f"duplicate sample_id: {sample_id}")
            supported[sample_id] = (scenario, value)
        return supported

    baseline = supported_rows(baseline_rows)
    candidate = supported_rows(candidate_rows)
    if set(baseline) != set(candidate):
        raise ValueError("baseline and candidate must have identical supported sample IDs")
    paired_ids = sorted(baseline)
    differences_by_scenario: dict[str, list[float]] = {}
    sample_differences: list[float] = []
    for sample_id in paired_ids:
        baseline_scenario, baseline_value = baseline[sample_id]
        candidate_scenario, candidate_value = candidate[sample_id]
        if baseline_scenario != candidate_scenario:
            raise ValueError(f"scenario mismatch for paired sample: {sample_id}")
        difference = candidate_value - baseline_value
        differences_by_scenario.setdefault(baseline_scenario, []).append(difference)
        sample_differences.append(difference)
    comparison = stratified_cluster_bootstrap_difference(
        differences_by_scenario,
        iterations=iterations,
        seed=seed,
    )
    return {
        "schema_version": "1.0",
        "paired_samples": comparison.paired_samples,
        "mean_cer_difference": round(comparison.mean_difference, 12),
        "sample_mean_cer_difference": round(
            sum(sample_differences) / len(sample_differences),
            12,
        ),
        "ci_low": comparison.ci_low,
        "ci_high": comparison.ci_high,
        "bootstrap_iterations": iterations,
        "seed": seed,
    }


def write_json(path: Path, payload: Mapping[str, object]) -> None:
    """写入稳定排序且带换行的 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _prepare_output_dir(output_dir: Path, artifact_names: Sequence[str]) -> None:
    """创建空产物集；已有运行不会被覆盖。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = [name for name in artifact_names if (output_dir / name).exists()]
    if existing:
        raise FileExistsError(f"benchmark output already exists: {', '.join(existing)}")
    output_dir.chmod(0o700)
    for name in ("events.jsonl", "failures.jsonl", "hypotheses.jsonl", "vendor-events.jsonl"):
        (output_dir / name).write_text("", encoding="utf-8")
    with (output_dir / "resources.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=(
                "sample_id",
                "audio_cursor_ms",
                "arrival_monotonic_ms",
                "process_cpu_seconds",
                "max_rss_bytes",
                "process_tree_rss_bytes",
                "resource_process_count",
            ),
        )
        writer.writeheader()
    for name in artifact_names:
        if name != "manifest.json":
            (output_dir / name).touch(exist_ok=True)
            (output_dir / name).chmod(0o600)


def _failure_hypothesis(sample: BenchmarkSample, error_code: str) -> dict[str, object]:
    return {
        "sample_id": sample.sample_id,
        "scenario": sample.scenario,
        "hypothesis_raw": "",
        "hypothesis_normalized": "",
        "language": sample.language,
        "duration_ms": sample.duration_ms,
        "wall_time_ms": None,
        "rtf": None,
        "deadline_misses": None,
        "finalization_latency_ms": None,
        "error_status": error_code,
    }


async def run_benchmark(
    *,
    manifest: ASRRunManifest,
    corpus: CorpusInputManifest,
    corpus_root: Path,
    output_dir: Path,
    transcriber_factory: BenchmarkTranscriberFactory,
    mode: ReplayMode,
    chunk_ms: int = 20,
    final_timeout_secs: float = 8.0,
    monotonic: MonotonicClock = time.monotonic,
    process_tree_rss_sampler: ProcessTreeRSSSampler = _process_tree_rss_bytes,
) -> BenchmarkRunResult:
    """顺序执行一个实验臂，并产生可审计且不含音频副本的文件集。"""
    if manifest.status != "planned":
        raise ValueError("run manifest status must be planned")
    if final_timeout_secs <= 0:
        raise ValueError("final_timeout_secs 必须为正数")
    if corpus.normalization_version != PRIMARY_NORMALIZATION_VERSION:
        raise ValueError(
            f"unsupported normalization_version: {corpus.normalization_version}"
        )
    artifact_names = (
        "events.jsonl",
        "failures.jsonl",
        "hypotheses.jsonl",
        "manifest.json",
        "resources.csv",
        "summary.json",
        "vendor-events.jsonl",
    )
    _prepare_output_dir(output_dir, artifact_names)
    write_run_manifest(
        output_dir / "manifest.json",
        manifest.model_copy(update={"status": "running"}),
    )

    completed = 0
    failed = 0
    run_started_at = monotonic()
    events_log = _JsonlSink(output_dir / "events.jsonl")
    vendor_events_log = _JsonlSink(output_dir / "vendor-events.jsonl")
    failures_log = _JsonlSink(output_dir / "failures.jsonl")
    hypotheses_log = _JsonlSink(output_dir / "hypotheses.jsonl")

    def event_sink(row: Mapping[str, object]) -> None:
        events_log.write(row)

    def vendor_sink(row: Mapping[str, object]) -> None:
        vendor_events_log.write(row)

    try:
        for source_epoch, sample in enumerate(corpus.samples):
            try:
                hypothesis, resource_rows = await _run_sample(
                    sample=sample,
                    source_epoch=source_epoch,
                    corpus_root=corpus_root,
                    transcriber_factory=transcriber_factory,
                    mode=mode,
                    chunk_ms=chunk_ms,
                    final_timeout_secs=final_timeout_secs,
                    run_started_at=run_started_at,
                    monotonic=monotonic,
                    expected_backend_id=manifest.backend_id,
                    event_sink=event_sink,
                    vendor_sink=vendor_sink,
                    process_tree_rss_sampler=process_tree_rss_sampler,
                )
            except BenchmarkSampleError as exc:
                failed += 1
                failures_log.write(
                    {
                        "sample_id": sample.sample_id,
                        "error_code": exc.code,
                        "error_message": str(exc)[:2000],
                    }
                )
                hypotheses_log.write(_failure_hypothesis(sample, exc.code))
            except Exception as exc:
                failed += 1
                error_code = "ASR_RUN_FAILED"
                failures_log.write(
                    {
                        "sample_id": sample.sample_id,
                        "error_code": error_code,
                        "error_message": f"unexpected ASR failure ({type(exc).__name__})",
                    }
                )
                hypotheses_log.write(_failure_hypothesis(sample, error_code))
            else:
                completed += 1
                hypotheses_log.write(hypothesis)
                with (output_dir / "resources.csv").open(
                    "a", encoding="utf-8", newline=""
                ) as stream:
                    writer = csv.DictWriter(stream, fieldnames=tuple(resource_rows[0].keys()))
                    writer.writerows(resource_rows)
            for log in (events_log, vendor_events_log, failures_log, hypotheses_log):
                log.flush()
    finally:
        for log in (events_log, vendor_events_log, failures_log, hypotheses_log):
            log.close()

    hypothesis_rows = load_blind_hypotheses(output_dir / "hypotheses.jsonl")
    rtf_values = [
        value
        for row in hypothesis_rows
        if (value := _finite_number(row.get("rtf"))) is not None
    ]
    summary: dict[str, object] = {
        "schema_version": "1.0",
        "samples": len(hypothesis_rows),
        "completed_samples": completed,
        "failed_samples": failed,
        "failure_rate": failed / len(hypothesis_rows) if hypothesis_rows else None,
        "rtf_p50": percentile(rtf_values, 50).value,
        "rtf_p95": percentile(rtf_values, 95).value,
        "scoring_status": "withheld",
    }
    write_json(output_dir / "summary.json", summary)
    final_status = "completed" if failed == 0 else "failed"
    write_run_manifest(
        output_dir / "manifest.json",
        manifest.model_copy(update={"status": final_status}),
    )
    for name in artifact_names:
        (output_dir / name).chmod(0o600)
    return BenchmarkRunResult(completed_samples=completed, failed_samples=failed)
