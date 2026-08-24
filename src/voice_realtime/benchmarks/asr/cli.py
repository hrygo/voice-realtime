"""`vr-asr-benchmark` 的 run、score、compare 命令。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Mapping, Sequence
from ipaddress import ip_address
from pathlib import Path
from typing import cast

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
from voice_realtime.benchmarks.resource_lock import exclusive_resource_lock

_PROFILE_ADAPTER: TypeAdapter[ASRProfile] = TypeAdapter(ASRProfile)


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
        "--profile",
        action="append",
        required=True,
        help="固定候选 profile，格式 candidate_id=/path/to/profile.json",
    )
    freeze_parser.add_argument("--output", required=True)

    run_parser = subparsers.add_parser("run", help="运行一个冻结实验臂")
    run_parser.add_argument("--manifest", required=True, help="预冻结 run manifest JSON")
    run_parser.add_argument("--corpus", required=True, help="冻结 corpus manifest JSON")
    run_parser.add_argument("--corpus-root", required=True, help="外部 PCM 语料根目录")
    run_parser.add_argument("--profile", required=True, help="ASR profile JSON")
    run_parser.add_argument("--repo-root", default=".", help="用于核验 git commit 的仓库根目录")
    run_parser.add_argument("--output-dir", help="输出目录；默认 runtime/benchmarks/asr/<run_id>")
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

    decide_parser = subparsers.add_parser(
        "decide",
        help="按 formal analysis plan 生成 Stage 1 Core/Final 决策",
    )
    decide_parser.add_argument("--analysis-plan", required=True)
    decide_parser.add_argument("--look", choices=("core", "final"), required=True)
    decide_parser.add_argument("--evidence", required=True)
    decide_parser.add_argument("--output", required=True)
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
    verify_git_checkout(repo_root, manifest.git_commit)
    profile = _PROFILE_ADAPTER.validate_json(profile_path.read_text(encoding="utf-8"))
    if profile.kind != manifest.backend_id:
        raise ValueError("ASR profile backend_id does not match run manifest")
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

    output_value = args.output_dir
    output_dir = (
        Path(str(output_value))
        if output_value
        else Path("runtime") / "benchmarks" / "asr" / manifest.run_id
    )
    async def run_with_cleanup() -> BenchmarkRunResult:
        try:
            return await run_benchmark(
                manifest=manifest,
                corpus=corpus,
                corpus_root=Path(str(args.corpus_root)),
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
    run_dir = Path(str(args.run_dir))
    manifest = load_run_manifest(run_dir / "manifest.json")
    reference_path = Path(str(args.references))
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
    comparison["baseline"] = str(baseline_paths[0])
    comparison["candidate"] = str(candidate_paths[0])
    comparison["baseline_runs"] = [str(path) for path in baseline_paths]
    comparison["candidate_runs"] = [str(path) for path in candidate_paths]
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
                    final_alpha=plan.look_alpha[1],
                )
            else:
                comparison["conditional_power"] = None
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
            design = load_analysis_plan_design(Path(str(args.design)))
            freeze_formal_analysis_plan(
                Path(str(args.output)),
                design,
                core_manifest=core_manifest,
                reserve_manifest=reserve_manifest,
                core_reference=core_reference,
                reserve_reference=reserve_reference,
                preflight_report=Path(str(args.preflight_report)),
                profile_paths=_parse_profile_paths(tuple(args.profile)),
            )
            return 0
        if args.command == "run":
            return _run_command(args)
        if args.command == "score":
            return _score_command(args)
        if args.command == "compare":
            return _compare_command(args)
        if args.command == "decide":
            analysis_plan_path = Path(str(args.analysis_plan))
            look = cast(Look, str(args.look))
            report = evaluate_stage1_look(
                load_analysis_plan(analysis_plan_path),
                look=look,
                evidence=load_family_look_evidence(
                    Path(str(args.evidence)),
                    expected_plan_sha256=sha256_file(analysis_plan_path),
                    expected_look=look,
                ),
            )
            write_stage1_decision_report(Path(str(args.output)), report)
            return 0
    except (OSError, ValueError) as exc:
        print(f"vr-asr-benchmark: {exc}", file=sys.stderr)
        return 2
    raise RuntimeError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
