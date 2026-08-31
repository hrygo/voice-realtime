"""`vr-asr-benchmark` 的 run、score、compare 命令。"""

from __future__ import annotations

import argparse
import asyncio
import json
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import fields
from ipaddress import ip_address
from pathlib import Path
from typing import Any, cast

from pydantic import TypeAdapter

from voice_realtime.asr.adapters.funasr_nano_pytorch import (
    FunASRNanoPyTorchInference,
)
from voice_realtime.asr.contracts import ASRSessionContext, StreamingTranscriber
from voice_realtime.asr.defaults import (
    build_funasr_nano_pytorch_registry,
    build_funasr_nano_ws_registry,
    build_wlk_registry,
)
from voice_realtime.asr.profiles import (
    ASRProfile,
    FunASRNanoPyTorchProfile,
    FunASRNanoWSProfile,
    SpeechRailRealtimeProfile,
)
from voice_realtime.asr.registry import ASRBackendRegistry
from voice_realtime.benchmarks.asr.analysis_plan import (
    freeze_formal_analysis_plan,
    load_analysis_plan,
    load_analysis_plan_design,
    sealed_sha256,
)
from voice_realtime.benchmarks.asr.backend_factory import (
    build_backend_runtime,
    require_compatible_mode,
    sample_profile,
    verify_profile_identity,
)
from voice_realtime.benchmarks.asr.corpus import load_preparation_spec, prepare_corpus
from voice_realtime.benchmarks.asr.manifest import (
    BenchmarkSample,
    CorpusReferenceManifest,
    load_corpus_input_manifest,
    load_reference_manifest,
    load_run_manifest,
    sha256_file,
    verify_file_hashes,
    verify_git_checkout,
)
from voice_realtime.benchmarks.asr.metrics import conditional_power_from_interim
from voice_realtime.benchmarks.asr.preflight import run_blind_preflight
from voice_realtime.benchmarks.asr.replay import (
    BenchmarkRunResult,
    ReplayMode,
    compare_hypotheses,
    load_blind_hypotheses,
    load_hypotheses,
    run_benchmark,
    score_blind_hypotheses,
    score_hypotheses,
    write_json,
    write_scored_hypotheses,
)
from voice_realtime.benchmarks.asr.report import (
    Look,
    evaluate_stage1_look,
    load_family_look_evidence,
    write_stage1_decision_report,
)
from voice_realtime.benchmarks.asr.stage_decision import (
    StageDecisionRequest,
    StageEvidenceError,
    verify_stage_decision,
    write_stage_decision_report,
)
from voice_realtime.benchmarks.asr.stage_evaluators import (
    DefaultStagePolicy,
    InteractionStagePolicy,
    MeetingStagePolicy,
    StagePolicy,
)
from voice_realtime.benchmarks.asr.stage_executors import (
    StageExecutorError,
    StageExecutorRegistry,
)
from voice_realtime.benchmarks.asr.stage_runner import (
    StageRunnerError,
    StageRunRequest,
    load_stage_run_request,
    run_stage,
)
from voice_realtime.benchmarks.asr.stage_validation import read_stable_file
from voice_realtime.benchmarks.resource_lock import (
    ResourceQuarantinedError,
    exclusive_resource_lock,
)

_PROFILE_ADAPTER: TypeAdapter[ASRProfile] = TypeAdapter(ASRProfile)
_STAGE_DECISION_REQUEST_ADAPTER: TypeAdapter[StageDecisionRequest] = TypeAdapter(
    StageDecisionRequest
)
_MAX_STAGE_REQUEST_BYTES = 1024 * 1024


def _loopback_service_url(host: str, port: int) -> str:
    """只允许本机 WLK 服务进入全本地 benchmark。"""
    normalized = host.strip().lower()
    if normalized == "localhost":
        return f"ws://localhost:{port}"
    try:
        address = ip_address(normalized)
    except ValueError as exc:
        raise ValueError("benchmark ASR service host must be loopback") from exc
    if not address.is_loopback:
        raise ValueError("benchmark ASR service host must be loopback")
    rendered_host = f"[{address}]" if address.version == 6 else str(address)
    return f"ws://{rendered_host}:{port}"


def _build_streaming_registry(
    profile: ASRProfile,
    service_url: str | None = None,
    raw_event_sink: Callable[[Mapping[str, object]], None] | None = None,
    *,
    pytorch_engine: FunASRNanoPyTorchInference | None = None,
) -> ASRBackendRegistry:
    """按判别 profile 选择对应协议 registry。"""
    if isinstance(profile, FunASRNanoPyTorchProfile):
        if pytorch_engine is None:
            raise ValueError("Fun-ASR PyTorch profile requires a shared inference engine")
        return build_funasr_nano_pytorch_registry(
            pytorch_engine,
            raw_event_sink=raw_event_sink,
        )
    if service_url is None:
        raise ValueError("streaming ASR profile requires a service URL")
    if isinstance(profile, FunASRNanoWSProfile):
        return build_funasr_nano_ws_registry(
            service_url,
            raw_event_sink=raw_event_sink,
        )
    return build_wlk_registry(service_url, raw_event_sink=raw_event_sink)


def _require_compatible_mode(profile: ASRProfile, mode: str) -> None:
    """阻止把原生离线推理的缓冲时间误报为实时流式指标。"""
    require_compatible_mode(profile, mode)


def _verify_pytorch_run_identity(
    profile: FunASRNanoPyTorchProfile,
    *,
    device: str,
    parameters: Mapping[str, object],
) -> None:
    """核对实际 profile 与盲测前冻结的 manifest 参数。"""
    if device != profile.device:
        raise ValueError("ASR profile device does not match run manifest")
    expected: dict[str, object] = {
        "language": profile.language,
        "language_source": profile.language_source,
        "hotwords": list(profile.hotwords),
        "itn": profile.itn,
        "ncpu": profile.ncpu,
    }
    for name, value in expected.items():
        if parameters.get(name) != value:
            raise ValueError(f"ASR profile {name} does not match run manifest")


def _sample_profile(profile: ASRProfile, sample: BenchmarkSample) -> ASRProfile:
    """仅对显式 corpus 策略应用冻结样本的语言提示。"""
    return sample_profile(profile, sample)


def _require_external_model_dir(model_dir: Path, repo_root: Path) -> Path:
    """拒绝把 benchmark 模型制品放在 Git 工作树内。"""
    resolved_model_dir = model_dir.resolve(strict=True)
    resolved_repo_root = repo_root.resolve(strict=True)
    if resolved_model_dir.is_relative_to(resolved_repo_root):
        raise ValueError("benchmark model_dir must be outside the repository")
    return resolved_model_dir


def _require_external_artifact_path(
    path: Path,
    repo_root: Path,
    *,
    label: str,
    must_exist: bool,
) -> Path:
    """拒绝把语料、逐字稿或 benchmark 结果留在 Git 工作树内。"""
    resolved = path.resolve(strict=must_exist)
    resolved_repo_root = repo_root.resolve(strict=True)
    if resolved.is_relative_to(resolved_repo_root):
        raise ValueError(f"{label} must be outside the repository")
    return resolved


def _json_object_no_duplicates(raw: bytes, *, label: str) -> Mapping[str, object]:
    def build_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} contains duplicate key: {key}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=build_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} JSON root must be an object")
    return cast(Mapping[str, object], payload)


def _load_external_request_object(
    request_path: Path,
    repository_root: Path,
    *,
    label: str,
) -> Mapping[str, object]:
    repository = repository_root.resolve(strict=True)
    _require_external_artifact_path(
        request_path,
        repository,
        label=label,
        must_exist=True,
    )
    stable = read_stable_file(
        request_path,
        label=label,
        max_bytes=_MAX_STAGE_REQUEST_BYTES,
    )
    if stat.S_IMODE(stable.identity[2]) != 0o600:
        raise ValueError(f"{label} must use mode 0600")
    _require_external_artifact_path(
        stable.path,
        repository,
        label=label,
        must_exist=True,
    )
    return _json_object_no_duplicates(stable.raw, label=label)


def _require_request_repository(
    declared_repository: Path,
    repository_root: Path,
    *,
    label: str,
) -> None:
    repository = repository_root.resolve(strict=True)
    try:
        declared = declared_repository.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label} repository boundary mismatch") from exc
    if declared != repository:
        raise ValueError(f"{label} repository boundary mismatch")


def load_stage_run_request_file(
    request_path: Path,
    repository_root: Path,
) -> StageRunRequest:
    """Load an exact, external Stage run request bound to the CLI repository."""

    payload = _load_external_request_object(
        request_path,
        repository_root,
        label="stage run request",
    )
    allowed = {field.name for field in fields(StageRunRequest)}
    extras = sorted(set(payload) - allowed)
    if extras:
        raise ValueError(f"stage run request contains extra field: {extras[0]}")
    request = load_stage_run_request(payload)
    _require_request_repository(
        request.repository_root,
        repository_root,
        label="stage run request",
    )
    return request


def load_stage_decision_request(
    request_path: Path,
    repository_root: Path,
) -> StageDecisionRequest:
    """Load an exact, external decision request bound to the CLI repository."""

    payload = _load_external_request_object(
        request_path,
        repository_root,
        label="stage decision request",
    )
    request = _STAGE_DECISION_REQUEST_ADAPTER.validate_python(payload)
    _require_request_repository(
        request.repository_root,
        repository_root,
        label="stage decision request",
    )
    return request


def build_stage_policy(request: StageRunRequest) -> StagePolicy:
    """Select the pure policy implied by immutable stage identity."""

    if request.stage == 5 and request.family_id == "meeting":
        if request.covered_stages != (3, 5):
            raise ValueError("meeting Stage 5 must use covered_stages (3, 5)")
        return MeetingStagePolicy()
    if request.family_id == "interaction":
        return InteractionStagePolicy()
    return DefaultStagePolicy()


def _build_stage_executor_registry() -> StageExecutorRegistry:
    """Return the explicit production registry; no synthetic fallback is registered."""

    return StageExecutorRegistry()


def run_stage_from_request(
    request_path: Path,
    repository_root: Path,
    registry: StageExecutorRegistry,
) -> int:
    """Run one frozen request; ``run_stage`` remains the only lock owner."""

    request = load_stage_run_request_file(request_path, repository_root)
    result = asyncio.run(
        run_stage(
            request,
            executor_factory=lambda: registry.create(request.executor_id),
            policy=build_stage_policy(request),
        )
    )
    return 0 if result.status in {"completed", "deferred"} else 1


def decide_stage_from_request(
    request_path: Path,
    repository_root: Path,
) -> int:
    """Derive and atomically publish a Stage decision from sealed evidence."""

    request = load_stage_decision_request(request_path, repository_root)
    report = verify_stage_decision(request)
    write_stage_decision_report(
        request.output_path,
        report,
        repository_root=repository_root,
    )
    return 0


def _verify_replay_identity(
    parameters: Mapping[str, object],
    *,
    chunk_ms: int,
    final_timeout_secs: float,
) -> None:
    """阻止 CLI 回放参数偏离冻结 run manifest。"""
    if parameters.get("chunk_ms") != chunk_ms:
        raise ValueError("ASR chunk_ms does not match run manifest")
    if parameters.get("final_timeout_secs") != final_timeout_secs:
        raise ValueError("ASR final_timeout_secs does not match run manifest")


def _open_reference_manifest(path: Path) -> tuple[str, CorpusReferenceManifest]:
    """首次从 000 开盲；同一 look 后续只接受 0600 已开封制品。"""
    mode = path.stat().st_mode & 0o777
    if mode == 0:
        artifact_hash = sealed_sha256(path)
        path.chmod(0o600)
    elif mode == 0o600:
        artifact_hash = sha256_file(path)
    else:
        raise ValueError("reference manifest must be sealed 000 or opened 0600")
    return artifact_hash, load_reference_manifest(path)


def build_parser() -> argparse.ArgumentParser:
    """构造稳定的 benchmark CLI 参数树。"""
    parser = argparse.ArgumentParser(description="运行和分析 ASR 科学对比测试")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser(
        "prepare-corpus", help="在项目外确定性制备并冻结语料"
    )
    prepare_parser.add_argument("--spec", required=True)
    prepare_parser.add_argument("--source-root", required=True)
    prepare_parser.add_argument("--output-root", required=True)
    prepare_parser.add_argument("--repo-root", default=".")

    preflight_parser = subparsers.add_parser(
        "preflight-corpus",
        help="不读取音频或逐字稿，预检项目外 blind metadata",
    )
    preflight_parser.add_argument("--metadata", required=True)
    preflight_parser.add_argument("--output-report", required=True)
    preflight_parser.add_argument("--repo-root", default=".")

    freeze_parser = subparsers.add_parser(
        "freeze-analysis", help="在 Core 输出可见前冻结序贯分析计划"
    )
    freeze_parser.add_argument("--core-manifest", required=True)
    freeze_parser.add_argument("--reserve-manifest", required=True)
    freeze_parser.add_argument("--core-references", required=True)
    freeze_parser.add_argument("--reserve-references", required=True)
    freeze_parser.add_argument("--design", required=True)
    freeze_parser.add_argument("--preflight-report", required=True)
    freeze_parser.add_argument(
        "--preflight-metadata",
        required=True,
        help="生成 preflight report 的原始 metadata 制品；freeze 会重新核验 SHA-256",
    )
    freeze_parser.add_argument(
        "--corpus-root",
        required=True,
        help="项目外已物化 PCM 根目录；freeze 会核验字节长度与 SHA-256",
    )
    freeze_parser.add_argument(
        "--power-simulation",
        required=True,
        help="blind 开封前基于 dev/pilot 生成的 10,000 次功效模拟制品",
    )
    freeze_parser.add_argument(
        "--profile",
        action="append",
        required=True,
        help="固定候选 profile，格式 candidate_id=/path/to/profile.json",
    )
    freeze_parser.add_argument("--output", required=True)
    freeze_parser.add_argument("--repo-root", default=".")

    run_parser = subparsers.add_parser("run", help="运行一个冻结实验臂")
    run_parser.add_argument("--manifest", required=True, help="预冻结 run manifest JSON")
    run_parser.add_argument("--corpus", required=True, help="冻结 corpus manifest JSON")
    run_parser.add_argument("--corpus-root", required=True, help="外部 PCM 语料根目录")
    run_parser.add_argument("--profile", required=True, help="ASR profile JSON")
    run_parser.add_argument(
        "--analysis-plan",
        help="正式 Core/Reserve 运行必须提供并匹配 run manifest 中冻结的 analysis plan",
    )
    run_parser.add_argument("--repo-root", default=".", help="用于核验 git commit 的仓库根目录")
    run_parser.add_argument(
        "--output-dir",
        required=True,
        help="项目外运行产物目录",
    )
    run_parser.add_argument(
        "--mode",
        choices=tuple(mode.value for mode in ReplayMode),
        default=ReplayMode.REALTIME_1X.value,
    )
    run_parser.add_argument("--chunk-ms", type=int, default=20)
    run_parser.add_argument("--final-timeout-secs", type=float, default=8.0)
    run_parser.add_argument(
        "--resource-lock",
        help="主机级实验排他锁路径；默认使用项目外的用户 cache",
    )
    run_parser.add_argument("--lock-timeout-secs", type=float, default=0.0)

    score_parser = subparsers.add_parser("score", help="显式开盲并生成独立评分产物")
    score_parser.add_argument("--run-dir", required=True)
    score_parser.add_argument("--references", required=True)
    score_parser.add_argument("--repo-root", default=".")

    compare_parser = subparsers.add_parser("compare", help="按 sample_id 配对比较两个实验臂")
    compare_parser.add_argument("--baseline", required=True)
    compare_parser.add_argument("--additional-baseline", action="append", default=[])
    compare_parser.add_argument("--candidate", required=True)
    compare_parser.add_argument("--additional-candidate", action="append", default=[])
    resampling_group = compare_parser.add_mutually_exclusive_group()
    resampling_group.add_argument(
        "--corpus",
        help="冻结输入 manifest；提供后按 content_group_id/session_id 做 cluster bootstrap",
    )
    compare_parser.add_argument("--additional-corpus", action="append", default=[])
    compare_parser.add_argument(
        "--analysis-plan",
        help="绑定 formal analysis plan；省略时 cluster 结果仅为 calibration",
    )
    resampling_group.add_argument(
        "--exploratory-sample-bootstrap",
        action="store_true",
        help="仅供探索性诊断；正式证据必须提供 --corpus",
    )
    compare_parser.add_argument("--output", required=True)
    compare_parser.add_argument("--bootstrap-iterations", type=int, default=10_000)
    compare_parser.add_argument("--seed", type=int)
    compare_parser.add_argument("--repo-root", default=".")

    decide_parser = subparsers.add_parser(
        "decide",
        help="按 formal analysis plan 生成 Stage 1 Core/Final 决策",
    )
    decide_parser.add_argument("--analysis-plan", required=True)
    decide_parser.add_argument("--look", choices=("core", "final"), required=True)
    decide_parser.add_argument("--evidence", required=True)
    decide_parser.add_argument(
        "--comparison",
        action="append",
        required=True,
        help="evidence 中每个 family/candidate 对应的不可变 formal comparison",
    )
    decide_parser.add_argument(
        "--gate-metrics",
        action="append",
        required=True,
        help="每个 family/candidate 的结构化非劣门禁指标制品",
    )
    decide_parser.add_argument(
        "--gate-source",
        action="append",
        required=True,
        help="门禁制品引用的源指标，格式 artifact_name=/path/to/artifact",
    )
    decide_parser.add_argument("--output", required=True)
    decide_parser.add_argument("--repo-root", default=".")

    run_stage_parser = subparsers.add_parser(
        "run-stage",
        help="执行 Stage 2-5 冻结运行",
    )
    run_stage_parser.add_argument("--request", required=True)
    run_stage_parser.add_argument("--repo-root", default=".")

    decide_stage_parser = subparsers.add_parser(
        "decide-stage",
        help="从封存制品生成 Stage 决策",
    )
    decide_stage_parser.add_argument("--request", required=True)
    decide_stage_parser.add_argument("--repo-root", default=".")
    return parser


def _run_command_locked(args: argparse.Namespace) -> int:
    manifest_path = Path(str(args.manifest))
    corpus_path = Path(str(args.corpus))
    profile_path = Path(str(args.profile))
    manifest = load_run_manifest(manifest_path)
    corpus = load_corpus_input_manifest(corpus_path)
    if sha256_file(corpus_path) != manifest.corpus_manifest_sha256:
        raise ValueError("corpus manifest SHA-256 does not match run manifest")
    repo_root = Path(str(args.repo_root))
    _require_external_artifact_path(
        corpus_path,
        repo_root,
        label="benchmark corpus manifest",
        must_exist=True,
    )
    corpus_root = _require_external_artifact_path(
        Path(str(args.corpus_root)),
        repo_root,
        label="benchmark corpus root",
        must_exist=True,
    )
    verify_git_checkout(repo_root, manifest.git_commit)
    profile = _PROFILE_ADAPTER.validate_json(profile_path.read_text(encoding="utf-8"))
    if sha256_file(profile_path) != manifest.profile_sha256:
        raise ValueError("ASR profile SHA-256 does not match run manifest")
    analysis_plan_value = args.analysis_plan
    if manifest.analysis_plan_sha256 is None:
        if analysis_plan_value is not None:
            raise ValueError("exploratory run manifest cannot attach an analysis plan")
    else:
        if analysis_plan_value is None:
            raise ValueError("formal run manifest requires --analysis-plan")
        analysis_plan_path = Path(str(analysis_plan_value))
        if sha256_file(analysis_plan_path) != manifest.analysis_plan_sha256:
            raise ValueError("analysis plan SHA-256 does not match run manifest")
        analysis_plan = load_analysis_plan(analysis_plan_path)
        expected_corpus_hash = (
            analysis_plan.core_manifest_sha256
            if manifest.analysis_split == "core"
            else analysis_plan.reserve_manifest_sha256
        )
        expected_split = (
            "blind-core" if manifest.analysis_split == "core" else "blind-reserve"
        )
        if (
            analysis_plan.evidence_tier != "formal"
            or manifest.candidate_id not in analysis_plan.candidate_ids
            or manifest.profile_sha256
            != analysis_plan.candidate_profile_sha256[manifest.candidate_id]
            or manifest.corpus_manifest_sha256 != expected_corpus_hash
            or corpus.split != expected_split
        ):
            raise ValueError("run manifest does not match formal analysis plan identity")
    if profile.kind != manifest.backend_id:
        raise ValueError("ASR profile backend_id does not match run manifest")
    if isinstance(profile, SpeechRailRealtimeProfile):
        raise ValueError(
            "speechrail-realtime-v2 is not benchmarkable: "
            "the benchmark manifest requires a frozen local model snapshot"
        )
    model_dir = _require_external_model_dir(profile.model_dir, repo_root)
    verify_file_hashes(model_dir, manifest.model_files_sha256)
    mode = ReplayMode(str(args.mode))
    _require_compatible_mode(profile, mode.value)
    _verify_replay_identity(
        manifest.parameters,
        chunk_ms=int(args.chunk_ms),
        final_timeout_secs=float(args.final_timeout_secs),
    )
    verify_profile_identity(
        profile,
        device=manifest.device,
        dtype=manifest.dtype,
        parameters=manifest.parameters,
    )
    runtime = build_backend_runtime(
        profile,
        repo_root=repo_root.resolve(strict=True),
        model_dir=model_dir,
    )

    def transcriber_factory(
        sample: BenchmarkSample,
        context: ASRSessionContext,
        vendor_event_sink: Callable[[Mapping[str, object]], None],
    ) -> StreamingTranscriber:
        return runtime.create_transcriber(sample, context, vendor_event_sink)

    output_dir = _require_external_artifact_path(
        Path(str(args.output_dir)),
        repo_root,
        label="benchmark output directory",
        must_exist=False,
    )

    async def run_with_cleanup() -> BenchmarkRunResult:
        try:
            return await run_benchmark(
                manifest=manifest,
                corpus=corpus,
                corpus_root=corpus_root,
                output_dir=output_dir,
                transcriber_factory=transcriber_factory,
                mode=mode,
                chunk_ms=int(args.chunk_ms),
                final_timeout_secs=float(args.final_timeout_secs),
            )
        finally:
            await runtime.close()

    result = asyncio.run(run_with_cleanup())
    return 0 if result.failed_samples == 0 else 1


def _run_command(args: argparse.Namespace) -> int:
    """在验证、加载模型和封存产物的完整生命周期内持有排他锁。"""
    lock_value = args.resource_lock
    lock_path = Path(str(lock_value)) if lock_value else None
    with exclusive_resource_lock(
        lock_path,
        timeout_secs=float(args.lock_timeout_secs),
        run_id=Path(str(args.manifest)).stem,
    ):
        return _run_command_locked(args)


def _score_command(args: argparse.Namespace) -> int:
    repo_root = Path(str(args.repo_root))
    run_dir = _require_external_artifact_path(
        Path(str(args.run_dir)),
        repo_root,
        label="benchmark run directory",
        must_exist=True,
    )
    manifest = load_run_manifest(run_dir / "manifest.json")
    reference_path = Path(str(args.references))
    _require_external_artifact_path(
        reference_path,
        repo_root,
        label="reference manifest",
        must_exist=True,
    )
    reference_hash, references = _open_reference_manifest(reference_path)
    if reference_hash != manifest.reference_manifest_sha256:
        raise ValueError("reference manifest SHA-256 does not match run manifest")
    if references.input_manifest_sha256 != manifest.corpus_manifest_sha256:
        raise ValueError("reference manifest is not bound to this corpus input")
    scored = score_blind_hypotheses(
        load_blind_hypotheses(run_dir / "hypotheses.jsonl"),
        references,
    )
    write_scored_hypotheses(run_dir / "scored-hypotheses.jsonl", scored)
    write_json(run_dir / "scored-summary.json", score_hypotheses(scored))
    return 0


def _compare_command(args: argparse.Namespace) -> int:
    output_path = Path(str(args.output))
    if output_path.exists():
        raise FileExistsError(f"comparison output already exists: {output_path}")
    baseline_paths = (
        Path(str(args.baseline)),
        *(Path(str(path)) for path in args.additional_baseline),
    )
    candidate_paths = (
        Path(str(args.candidate)),
        *(Path(str(path)) for path in args.additional_candidate),
    )
    if len(baseline_paths) != len(candidate_paths):
        raise ValueError("baseline and candidate run counts must match")
    corpus_paths = (
        ()
        if args.corpus is None
        else (
            Path(str(args.corpus)),
            *(Path(str(path)) for path in args.additional_corpus),
        )
    )
    if not corpus_paths and not bool(args.exploratory_sample_bootstrap):
        raise ValueError(
            "formal comparison requires --corpus for cluster bootstrap; "
            "sample bootstrap is exploratory only"
        )
    if corpus_paths and len(corpus_paths) != len(baseline_paths):
        raise ValueError("each run pair requires exactly one corpus manifest")
    if not corpus_paths and len(baseline_paths) != 1:
        raise ValueError("exploratory sample bootstrap accepts one run pair")
    analysis_plan_path = (
        Path(str(args.analysis_plan)) if args.analysis_plan is not None else None
    )
    if analysis_plan_path is not None and not corpus_paths:
        raise ValueError("--analysis-plan requires --corpus")
    repo_root = Path(str(args.repo_root))
    _require_external_artifact_path(
        output_path,
        repo_root,
        label="comparison output",
        must_exist=False,
    )
    for path in (*baseline_paths, *candidate_paths, *corpus_paths):
        _require_external_artifact_path(
            path,
            repo_root,
            label="benchmark comparison input",
            must_exist=True,
        )
    cluster_by_sample: dict[str, str] | None = None
    corpora = tuple(load_corpus_input_manifest(path) for path in corpus_paths)
    if corpora:
        cluster_by_sample = {}
        for corpus in corpora:
            for sample in corpus.samples:
                if sample.sample_id in cluster_by_sample:
                    raise ValueError("corpus manifests contain duplicate sample IDs")
                cluster_by_sample[sample.sample_id] = (
                    sample.analysis_cluster_id
                    or sample.content_group_id
                    or sample.session_id
                )

    confidence = 0.95
    seed = int(args.seed) if args.seed is not None else 0
    look: str | None = None
    plan = None
    if analysis_plan_path is not None:
        plan = load_analysis_plan(analysis_plan_path)
        if plan.evidence_tier != "formal":
            raise ValueError("comparison analysis plan must be formal")
        analysis_plan_hash = sha256_file(analysis_plan_path)
        by_split = {
            corpus.split: (path, corpus)
            for path, corpus in zip(corpus_paths, corpora, strict=True)
        }
        if len(by_split) != len(corpora):
            raise ValueError("formal comparison corpus splits must be unique")
        if set(by_split) == {"blind-core"}:
            look = "core"
            look_index = 0
        elif set(by_split) == {"blind-core", "blind-reserve"}:
            look = "final"
            look_index = 1
        else:
            raise ValueError("formal comparison requires Core or Core+Reserve union")
        expected_by_split = {
            "blind-core": (
                plan.core_manifest_sha256,
                plan.core_analysis_cluster_ids,
            ),
            "blind-reserve": (
                plan.reserve_manifest_sha256,
                plan.reserve_analysis_cluster_ids,
            ),
        }
        for split, (path, corpus) in by_split.items():
            expected_hash, expected_clusters = expected_by_split[split]
            observed_clusters = tuple(
                sorted({sample.analysis_cluster_id or "" for sample in corpus.samples})
            )
            if sha256_file(path) != expected_hash or observed_clusters != expected_clusters:
                raise ValueError("corpus does not match formal analysis plan identity")
        baseline_manifests = tuple(
            load_run_manifest(path / "manifest.json") for path in baseline_paths
        )
        candidate_manifests = tuple(
            load_run_manifest(path / "manifest.json") for path in candidate_paths
        )
        baseline_ids = {manifest.candidate_id for manifest in baseline_manifests}
        candidate_ids = {manifest.candidate_id for manifest in candidate_manifests}
        if len(baseline_ids) != 1 or len(candidate_ids) != 1:
            raise ValueError("formal run identity must remain stable across looks")
        stable_identity_fields = (
            "git_commit",
            "candidate_id",
            "profile_sha256",
            "backend_id",
            "model_id",
            "model_revision",
            "model_files_sha256",
            "runtime",
            "device",
            "dtype",
            "parameters",
            "environment",
        )
        for manifests in (baseline_manifests, candidate_manifests):
            first = manifests[0]
            if any(
                any(
                    getattr(manifest, field) != getattr(first, field)
                    for field in stable_identity_fields
                )
                for manifest in manifests[1:]
            ):
                raise ValueError("formal run identity drifted across Core/Reserve")
        baseline_id = next(iter(baseline_ids))
        candidate_id = next(iter(candidate_ids))
        matching_families = tuple(
            family
            for family in plan.decision_families
            if family.baseline_id == baseline_id
            and candidate_id in family.candidate_ids
        )
        if len(matching_families) != 1:
            raise ValueError("formal run pair does not match exactly one decision family")
        family = matching_families[0]
        expected_reference_by_split = {
            "blind-core": plan.core_reference_sha256,
            "blind-reserve": plan.reserve_reference_sha256,
        }
        for index, corpus_path in enumerate(corpus_paths):
            corpus = corpora[index]
            corpus_hash = sha256_file(corpus_path)
            expected_reference_hash = expected_reference_by_split[corpus.split]
            expected_analysis_split = (
                "core" if corpus.split == "blind-core" else "reserve"
            )
            for manifest, expected_candidate_id in (
                (baseline_manifests[index], baseline_id),
                (candidate_manifests[index], candidate_id),
            ):
                if manifest.status not in {"completed", "failed"}:
                    raise ValueError("formal comparison requires a finished run manifest")
                if (
                    manifest.corpus_manifest_sha256 != corpus_hash
                    or manifest.reference_manifest_sha256 != expected_reference_hash
                    or manifest.analysis_plan_sha256 != analysis_plan_hash
                    or manifest.analysis_split != expected_analysis_split
                ):
                    raise ValueError(
                        "formal run manifest is not bound to the frozen corpus/reference"
                    )
                if (
                    manifest.candidate_id != expected_candidate_id
                    or manifest.profile_sha256
                    != plan.candidate_profile_sha256[expected_candidate_id]
                ):
                    raise ValueError("formal run profile is not frozen in the analysis plan")
        if int(args.bootstrap_iterations) != plan.bootstrap_iterations:
            raise ValueError("formal comparison bootstrap iterations do not match analysis plan")
        if args.seed is not None and int(args.seed) != plan.bootstrap_seeds[look_index]:
            raise ValueError("formal comparison seed does not match analysis plan")
        confidence = plan.decision_confidence[look_index]
        seed = plan.bootstrap_seeds[look_index]

    baseline_rows = tuple(
        row
        for path in baseline_paths
        for row in load_hypotheses(path / "scored-hypotheses.jsonl")
    )
    candidate_rows = tuple(
        row
        for path in candidate_paths
        for row in load_hypotheses(path / "scored-hypotheses.jsonl")
    )
    comparison = compare_hypotheses(
        baseline_rows,
        candidate_rows,
        iterations=int(args.bootstrap_iterations),
        seed=seed,
        confidence=confidence,
        cluster_by_sample=cluster_by_sample,
    )
    comparison["baseline_run_ids"] = [path.name for path in baseline_paths]
    comparison["candidate_run_ids"] = [path.name for path in candidate_paths]
    if corpus_paths:
        corpus_hashes = [sha256_file(path) for path in corpus_paths]
        comparison["corpus_manifest_sha256"] = corpus_hashes[0]
        comparison["corpus_manifest_sha256s"] = corpus_hashes
        comparison["evidence_tier"] = "cluster_calibration"
        comparison["analysis_cluster_ids"] = (
            sorted(set(cluster_by_sample.values())) if cluster_by_sample else []
        )
        if analysis_plan_path is not None and plan is not None:
            comparison["evidence_tier"] = "formal"
            comparison["analysis_plan_sha256"] = sha256_file(analysis_plan_path)
            comparison["look"] = look
            comparison["family_id"] = family.family_id
            comparison["baseline_id"] = baseline_id
            comparison["candidate_id"] = candidate_id
            comparison["run_manifest_sha256s"] = {
                "baseline": [
                    sha256_file(path / "manifest.json") for path in baseline_paths
                ],
                "candidate": [
                    sha256_file(path / "manifest.json") for path in candidate_paths
                ],
            }
            comparison["scored_hypotheses_sha256s"] = {
                "baseline": [
                    sha256_file(path / "scored-hypotheses.jsonl")
                    for path in baseline_paths
                ],
                "candidate": [
                    sha256_file(path / "scored-hypotheses.jsonl")
                    for path in candidate_paths
                ],
            }
            if look == "core":
                standard_error_value = comparison["bootstrap_standard_error"]
                mean_difference_value = comparison["mean_cer_difference"]
                if not isinstance(standard_error_value, int | float) or not isinstance(
                    mean_difference_value, int | float
                ):
                    raise RuntimeError("comparison statistics must be numeric")
                core_duration_ms = plan.core_duration_ms
                reserve_duration_ms = plan.reserve_duration_ms
                if core_duration_ms is None or reserve_duration_ms is None:
                    raise RuntimeError("formal plan durations must be frozen")
                comparison["conditional_power"] = conditional_power_from_interim(
                    mean_difference=float(mean_difference_value),
                    standard_error=float(standard_error_value),
                    information_fraction=(
                        core_duration_ms
                        / (core_duration_ms + reserve_duration_ms)
                    ),
                    final_alpha=(
                        plan.look_alpha[1] / len(family.candidate_ids)
                    ),
                    minimum_detectable_effect=family.minimum_detectable_effect,
                )
                comparison["conditional_power_method"] = (
                    "normal-independent-increments-mde-holm-conservative-v1"
                )
                comparison["minimum_detectable_effect"] = (
                    family.minimum_detectable_effect
                )
            else:
                comparison["conditional_power"] = None
                comparison["conditional_power_method"] = None
                comparison["minimum_detectable_effect"] = (
                    family.minimum_detectable_effect
                )
    else:
        comparison["evidence_tier"] = "exploratory"
    write_json(output_path, comparison)
    return 0


def _parse_profile_paths(values: Sequence[str]) -> dict[str, Path]:
    profiles: dict[str, Path] = {}
    for value in values:
        candidate_id, separator, raw_path = value.partition("=")
        candidate_id = candidate_id.strip()
        raw_path = raw_path.strip()
        if not separator or not candidate_id or not raw_path:
            raise ValueError("--profile must use candidate_id=/path/to/profile.json")
        if candidate_id in profiles:
            raise ValueError(f"duplicate --profile candidate_id: {candidate_id}")
        profiles[candidate_id] = Path(raw_path)
    return profiles


def _parse_gate_source_paths(values: Sequence[str]) -> dict[str, Path]:
    sources: dict[str, Path] = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        name = name.strip()
        raw_path = raw_path.strip()
        if not separator or not name or not raw_path:
            raise ValueError("--gate-source must use artifact_name=/path/to/artifact")
        if name in sources:
            raise ValueError(f"duplicate --gate-source artifact_name: {name}")
        sources[name] = Path(raw_path)
    return sources


def main(argv: Sequence[str] | None = None) -> int:
    """解析命令并把边界错误转换为稳定非零退出码。"""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare-corpus":
            prepare_corpus(
                spec=load_preparation_spec(Path(str(args.spec))),
                source_root=Path(str(args.source_root)),
                output_root=Path(str(args.output_root)),
                repository_root=Path(str(args.repo_root)),
            )
            return 0
        if args.command == "preflight-corpus":
            run_blind_preflight(
                metadata_path=Path(str(args.metadata)),
                output_path=Path(str(args.output_report)),
                repository_root=Path(str(args.repo_root)),
            )
            return 0
        if args.command == "freeze-analysis":
            core_manifest = Path(str(args.core_manifest))
            reserve_manifest = Path(str(args.reserve_manifest))
            core_reference = Path(str(args.core_references))
            reserve_reference = Path(str(args.reserve_references))
            freeze_repo_root = Path(str(args.repo_root))
            preflight_report = Path(str(args.preflight_report))
            preflight_metadata = Path(str(args.preflight_metadata))
            corpus_root = Path(str(args.corpus_root))
            power_simulation = Path(str(args.power_simulation))
            output = Path(str(args.output))
            for path, label, must_exist in (
                (core_manifest, "Core manifest", True),
                (reserve_manifest, "Reserve manifest", True),
                (core_reference, "Core reference", True),
                (reserve_reference, "Reserve reference", True),
                (preflight_report, "preflight report", True),
                (preflight_metadata, "preflight metadata", True),
                (corpus_root, "corpus root", True),
                (power_simulation, "power simulation", True),
                (output, "analysis plan output", False),
            ):
                _require_external_artifact_path(
                    path,
                    freeze_repo_root,
                    label=label,
                    must_exist=must_exist,
                )
            design = load_analysis_plan_design(Path(str(args.design)))
            freeze_formal_analysis_plan(
                output,
                design,
                core_manifest=core_manifest,
                reserve_manifest=reserve_manifest,
                core_reference=core_reference,
                reserve_reference=reserve_reference,
                preflight_report=preflight_report,
                profile_paths=_parse_profile_paths(tuple(args.profile)),
                corpus_root=corpus_root,
                power_simulation=power_simulation,
                preflight_metadata=preflight_metadata,
            )
            return 0
        if args.command == "run":
            return _run_command(args)
        if args.command == "run-stage":
            return run_stage_from_request(
                Path(str(args.request)),
                Path(str(args.repo_root)),
                _build_stage_executor_registry(),
            )
        if args.command == "decide-stage":
            return decide_stage_from_request(
                Path(str(args.request)),
                Path(str(args.repo_root)),
            )
        if args.command == "score":
            return _score_command(args)
        if args.command == "compare":
            return _compare_command(args)
        if args.command == "decide":
            analysis_plan_path = Path(str(args.analysis_plan))
            evidence_path = Path(str(args.evidence))
            comparison_paths = tuple(Path(str(path)) for path in args.comparison)
            gate_metrics_paths = tuple(Path(str(path)) for path in args.gate_metrics)
            gate_source_paths = _parse_gate_source_paths(tuple(args.gate_source))
            repo_root = Path(str(args.repo_root))
            for path, label, must_exist in (
                (evidence_path, "Stage 1 evidence", True),
                *((path, "formal comparison", True) for path in comparison_paths),
                *((path, "gate metrics", True) for path in gate_metrics_paths),
                *((path, "gate source", True) for path in gate_source_paths.values()),
                (Path(str(args.output)), "Stage 1 decision output", False),
            ):
                _require_external_artifact_path(
                    path,
                    repo_root,
                    label=label,
                    must_exist=must_exist,
                )
            look = cast(Look, str(args.look))
            report = evaluate_stage1_look(
                load_analysis_plan(analysis_plan_path),
                look=look,
                evidence=load_family_look_evidence(
                    evidence_path,
                    expected_plan_sha256=sha256_file(analysis_plan_path),
                    expected_look=look,
                    comparison_paths=comparison_paths,
                    gate_metrics_paths=gate_metrics_paths,
                    gate_source_paths=gate_source_paths,
                ),
            )
            report = report.model_copy(
                update={
                    "analysis_plan_sha256": sha256_file(analysis_plan_path),
                    "evidence_bundle_sha256": sha256_file(evidence_path),
                    "comparison_sha256s": tuple(
                        sha256_file(path) for path in comparison_paths
                    ),
                    "gate_metrics_sha256s": tuple(
                        sha256_file(path) for path in gate_metrics_paths
                    ),
                }
            )
            write_stage1_decision_report(Path(str(args.output)), report)
            return 0
    except (
        OSError,
        ValueError,
        ResourceQuarantinedError,
        StageEvidenceError,
        StageExecutorError,
        StageRunnerError,
    ) as exc:
        print(f"vr-asr-benchmark: {exc}", file=sys.stderr)
        return 2
    raise RuntimeError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
