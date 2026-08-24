"""`vr-asr-benchmark` 的 run、score、compare 命令。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Mapping, Sequence
from ipaddress import ip_address
from pathlib import Path

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
    AnalysisPlan,
    freeze_analysis_plan,
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

    freeze_parser = subparsers.add_parser(
        "freeze-analysis", help="在 Core 输出可见前冻结序贯分析计划"
    )
    freeze_parser.add_argument("--core-manifest", required=True)
    freeze_parser.add_argument("--reserve-manifest", required=True)
    freeze_parser.add_argument("--core-references", required=True)
    freeze_parser.add_argument("--reserve-references", required=True)
    freeze_parser.add_argument("--candidate", action="append", required=True)
    freeze_parser.add_argument(
        "--bootstrap-seed", action="append", required=True, type=int
    )
    freeze_parser.add_argument("--pilot-baseline-cer", required=True, type=float)
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
    compare_parser.add_argument("--candidate", required=True)
    compare_parser.add_argument(
        "--corpus",
        help="冻结输入 manifest；提供后按 content_group_id/session_id 做 cluster bootstrap",
    )
    compare_parser.add_argument("--output", required=True)
    compare_parser.add_argument("--bootstrap-iterations", type=int, default=10_000)
    compare_parser.add_argument("--seed", type=int, default=0)
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
    baseline = Path(str(args.baseline))
    candidate = Path(str(args.candidate))
    corpus_path = Path(str(args.corpus)) if args.corpus is not None else None
    cluster_by_sample: dict[str, str] | None = None
    if corpus_path is not None:
        corpus = load_corpus_input_manifest(corpus_path)
        cluster_by_sample = {
            sample.sample_id: sample.content_group_id or sample.session_id
            for sample in corpus.samples
        }
    comparison = compare_hypotheses(
        load_hypotheses(baseline / "scored-hypotheses.jsonl"),
        load_hypotheses(candidate / "scored-hypotheses.jsonl"),
        iterations=int(args.bootstrap_iterations),
        seed=int(args.seed),
        cluster_by_sample=cluster_by_sample,
    )
    comparison["baseline"] = str(baseline)
    comparison["candidate"] = str(candidate)
    if corpus_path is not None:
        comparison["corpus_manifest_sha256"] = sha256_file(corpus_path)
    write_json(Path(str(args.output)), comparison)
    return 0


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
        if args.command == "freeze-analysis":
            seeds = tuple(args.bootstrap_seed)
            if len(seeds) != 2:
                raise ValueError("freeze-analysis requires exactly two bootstrap seeds")
            core_manifest = Path(str(args.core_manifest))
            reserve_manifest = Path(str(args.reserve_manifest))
            core_reference = Path(str(args.core_references))
            reserve_reference = Path(str(args.reserve_references))
            plan = AnalysisPlan(
                candidate_ids=tuple(args.candidate),
                core_manifest_sha256=sha256_file(core_manifest),
                reserve_manifest_sha256=sha256_file(reserve_manifest),
                core_reference_sha256=sealed_sha256(core_reference),
                reserve_reference_sha256=sealed_sha256(reserve_reference),
                bootstrap_seeds=seeds,
                pilot_baseline_cer=float(args.pilot_baseline_cer),
            )
            freeze_analysis_plan(
                Path(str(args.output)),
                plan,
                core_manifest=core_manifest,
                reserve_manifest=reserve_manifest,
                core_reference=core_reference,
                reserve_reference=reserve_reference,
            )
            return 0
        if args.command == "run":
            return _run_command(args)
        if args.command == "score":
            return _score_command(args)
        if args.command == "compare":
            return _compare_command(args)
    except (OSError, ValueError) as exc:
        print(f"vr-asr-benchmark: {exc}", file=sys.stderr)
        return 2
    raise RuntimeError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
