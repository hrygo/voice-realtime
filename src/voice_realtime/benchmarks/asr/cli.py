"""`vr-asr-benchmark` 的 run、score、compare 命令。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Mapping, Sequence
from ipaddress import ip_address
from pathlib import Path

from pydantic import TypeAdapter

from voice_realtime.asr.contracts import ASRSessionContext, StreamingTranscriber
from voice_realtime.asr.defaults import (
    build_funasr_nano_ws_registry,
    build_wlk_registry,
)
from voice_realtime.asr.profiles import ASRProfile, FunASRNanoWSProfile
from voice_realtime.asr.registry import ASRBackendRegistry
from voice_realtime.benchmarks.asr.manifest import (
    CorpusSample,
    load_corpus_manifest,
    load_run_manifest,
    sha256_file,
    verify_file_hashes,
    verify_git_checkout,
)
from voice_realtime.benchmarks.asr.replay import (
    ReplayMode,
    compare_hypotheses,
    load_hypotheses,
    run_benchmark,
    score_hypotheses,
    write_json,
)

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
    service_url: str,
    raw_event_sink: Callable[[Mapping[str, object]], None] | None = None,
) -> ASRBackendRegistry:
    """按判别 profile 选择对应协议 registry。"""
    if isinstance(profile, FunASRNanoWSProfile):
        return build_funasr_nano_ws_registry(
            service_url,
            raw_event_sink=raw_event_sink,
        )
    return build_wlk_registry(service_url, raw_event_sink=raw_event_sink)


def _require_external_model_dir(model_dir: Path, repo_root: Path) -> Path:
    """拒绝把 benchmark 模型制品放在 Git 工作树内。"""
    resolved_model_dir = model_dir.resolve(strict=True)
    resolved_repo_root = repo_root.resolve(strict=True)
    if resolved_model_dir.is_relative_to(resolved_repo_root):
        raise ValueError("benchmark model_dir must be outside the repository")
    return resolved_model_dir


def build_parser() -> argparse.ArgumentParser:
    """构造稳定的 benchmark CLI 参数树。"""
    parser = argparse.ArgumentParser(description="运行和分析 ASR 科学对比测试")
    subparsers = parser.add_subparsers(dest="command", required=True)

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

    score_parser = subparsers.add_parser("score", help="从 hypotheses.jsonl 重新生成汇总")
    score_parser.add_argument("--run-dir", required=True)

    compare_parser = subparsers.add_parser("compare", help="按 sample_id 配对比较两个实验臂")
    compare_parser.add_argument("--baseline", required=True)
    compare_parser.add_argument("--candidate", required=True)
    compare_parser.add_argument("--output", required=True)
    compare_parser.add_argument("--bootstrap-iterations", type=int, default=10_000)
    compare_parser.add_argument("--seed", type=int, default=0)
    return parser


def _run_command(args: argparse.Namespace) -> int:
    manifest_path = Path(str(args.manifest))
    corpus_path = Path(str(args.corpus))
    profile_path = Path(str(args.profile))
    manifest = load_run_manifest(manifest_path)
    corpus = load_corpus_manifest(corpus_path)
    if sha256_file(corpus_path) != manifest.corpus_manifest_sha256:
        raise ValueError("corpus manifest SHA-256 does not match run manifest")
    repo_root = Path(str(args.repo_root))
    verify_git_checkout(repo_root, manifest.git_commit)
    profile = _PROFILE_ADAPTER.validate_json(profile_path.read_text(encoding="utf-8"))
    if profile.kind != manifest.backend_id:
        raise ValueError("ASR profile backend_id does not match run manifest")
    model_dir = _require_external_model_dir(profile.model_dir, repo_root)
    verify_file_hashes(model_dir, manifest.model_files_sha256)
    service_url = _loopback_service_url(profile.host, profile.port)

    def transcriber_factory(
        sample: CorpusSample,
        context: ASRSessionContext,
        vendor_event_sink: Callable[[Mapping[str, object]], None],
    ) -> StreamingTranscriber:
        del sample
        registry = _build_streaming_registry(profile, service_url, vendor_event_sink)
        return registry.create_streaming(profile, context)

    output_value = args.output_dir
    output_dir = (
        Path(str(output_value))
        if output_value
        else Path("runtime") / "benchmarks" / "asr" / manifest.run_id
    )
    result = asyncio.run(
        run_benchmark(
            manifest=manifest,
            corpus=corpus,
            corpus_root=Path(str(args.corpus_root)),
            output_dir=output_dir,
            transcriber_factory=transcriber_factory,
            mode=ReplayMode(str(args.mode)),
            chunk_ms=int(args.chunk_ms),
            final_timeout_secs=float(args.final_timeout_secs),
        )
    )
    return 0 if result.failed_samples == 0 else 1


def _score_command(args: argparse.Namespace) -> int:
    run_dir = Path(str(args.run_dir))
    rows = load_hypotheses(run_dir / "hypotheses.jsonl")
    write_json(run_dir / "summary.json", score_hypotheses(rows))
    return 0


def _compare_command(args: argparse.Namespace) -> int:
    baseline = Path(str(args.baseline))
    candidate = Path(str(args.candidate))
    comparison = compare_hypotheses(
        load_hypotheses(baseline / "hypotheses.jsonl"),
        load_hypotheses(candidate / "hypotheses.jsonl"),
        iterations=int(args.bootstrap_iterations),
        seed=int(args.seed),
    )
    comparison["baseline"] = str(baseline)
    comparison["candidate"] = str(candidate)
    write_json(Path(str(args.output)), comparison)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """解析命令并把边界错误转换为稳定非零退出码。"""
    args = build_parser().parse_args(argv)
    try:
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
