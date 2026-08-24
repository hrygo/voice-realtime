"""Benchmark profile 调度、冻结身份校验与 run 级共享资源生命周期。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path

from voice_realtime.asr.adapters.funasr_nano_pytorch import (
    FunASRNanoPyTorchEngine,
    FunASRNanoPyTorchInference,
)
from voice_realtime.asr.adapters.qwen3_native import (
    Qwen3NativeWorker,
    Qwen3NativeWorkerConfig,
)
from voice_realtime.asr.adapters.sensevoice_native import (
    SenseVoiceNativeEngine,
    SenseVoiceNativeInference,
)
from voice_realtime.asr.contracts import ASRSessionContext, StreamingTranscriber
from voice_realtime.asr.defaults import (
    build_funasr_nano_pytorch_registry,
    build_funasr_nano_ws_registry,
    build_qwen3_native_registry,
    build_sensevoice_native_registry,
    build_wlk_registry,
)
from voice_realtime.asr.profiles import (
    ASRProfile,
    FunASRNanoPyTorchProfile,
    FunASRNanoWSProfile,
    Qwen3NativeProfile,
    SenseVoiceNativeProfile,
)
from voice_realtime.benchmarks.asr.manifest import BenchmarkSample
from voice_realtime.benchmarks.asr.replay import ReplayMode

RawEventSink = Callable[[Mapping[str, object]], None]
_AUTO_DETECT_LANGUAGE_LABELS = frozenset({"auto", "mixed", "zh-en", "en-zh"})


def loopback_service_url(host: str, port: int) -> str:
    """只允许本机服务进入全本地 benchmark。"""
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


def require_compatible_mode(profile: ASRProfile, mode: str) -> None:
    """禁止把整段离线 adapter 的缓冲时间误报为流式指标。"""
    if isinstance(
        profile,
        (FunASRNanoPyTorchProfile, Qwen3NativeProfile, SenseVoiceNativeProfile),
    ) and mode != ReplayMode.OFFLINE.value:
        raise ValueError(f"{profile.kind} profile requires --mode offline")


def sample_profile(profile: ASRProfile, sample: BenchmarkSample) -> ASRProfile:
    """仅对显式 corpus 策略使用冻结样本语言。"""
    if isinstance(
        profile,
        (FunASRNanoPyTorchProfile, Qwen3NativeProfile, SenseVoiceNativeProfile),
    ) and profile.language_source == "corpus":
        language = sample.language.strip()
        if language.lower() in _AUTO_DETECT_LANGUAGE_LABELS:
            language = "auto"
        return profile.model_copy(update={"language": language})
    return profile


def _require_parameters(
    expected: Mapping[str, object],
    parameters: Mapping[str, object],
) -> None:
    for name, value in expected.items():
        if parameters.get(name) != value:
            raise ValueError(f"ASR profile {name} does not match run manifest")


def verify_profile_identity(
    profile: ASRProfile,
    *,
    device: str,
    dtype: str,
    parameters: Mapping[str, object],
) -> None:
    """核对原生实验 profile 与盲测前冻结的 manifest。"""
    if isinstance(profile, FunASRNanoPyTorchProfile):
        if device != profile.device:
            raise ValueError("ASR profile device does not match run manifest")
        _require_parameters(
            {
                "language": profile.language,
                "language_source": profile.language_source,
                "hotwords": list(profile.hotwords),
                "itn": profile.itn,
                "ncpu": profile.ncpu,
            },
            parameters,
        )
        return
    if isinstance(profile, SenseVoiceNativeProfile):
        if device != "cpu":
            raise ValueError("ASR profile device does not match run manifest")
        if dtype != "float32":
            raise ValueError("ASR profile dtype does not match run manifest")
        _require_parameters(
            {
                "language": profile.language,
                "language_source": profile.language_source,
                "use_itn": profile.use_itn,
                "ncpu": profile.ncpu,
            },
            parameters,
        )
        return
    if isinstance(profile, Qwen3NativeProfile):
        expected_dtype = "float16" if profile.device == "mps" else "float32"
        if device != profile.device:
            raise ValueError("ASR profile device does not match run manifest")
        if dtype != expected_dtype:
            raise ValueError("ASR profile dtype does not match run manifest")
        _require_parameters(
            {
                "language": profile.language,
                "language_source": profile.language_source,
                "context": profile.context,
                "max_new_tokens": profile.max_new_tokens,
                "timeout_secs": profile.timeout_secs,
            },
            parameters,
        )


@dataclass(slots=True)
class BenchmarkBackendRuntime:
    """一个 run 的共享重型资源；样本 adapter 不拥有这些资源。"""

    profile: ASRProfile
    repo_root: Path
    model_dir: Path
    service_url: str | None = None
    funasr_engine: FunASRNanoPyTorchInference | None = None
    sensevoice_engine: SenseVoiceNativeInference | None = None
    qwen_worker: Qwen3NativeWorker | None = None
    _closed: bool = field(default=False, init=False)

    def create_transcriber(
        self,
        sample: BenchmarkSample,
        context: ASRSessionContext,
        raw_event_sink: RawEventSink,
    ) -> StreamingTranscriber:
        if self._closed:
            raise RuntimeError("benchmark backend runtime is closed")
        effective = sample_profile(self.profile, sample)
        if isinstance(effective, FunASRNanoPyTorchProfile):
            if self.funasr_engine is None:
                raise RuntimeError("Fun-ASR shared engine is missing")
            registry = build_funasr_nano_pytorch_registry(
                self.funasr_engine,
                raw_event_sink=raw_event_sink,
            )
        elif isinstance(effective, SenseVoiceNativeProfile):
            if self.sensevoice_engine is None:
                raise RuntimeError("SenseVoice shared engine is missing")
            registry = build_sensevoice_native_registry(
                self.sensevoice_engine,
                raw_event_sink=raw_event_sink,
            )
        elif isinstance(effective, Qwen3NativeProfile):
            if self.qwen_worker is None:
                raise RuntimeError("Qwen3 shared worker is missing")
            registry = build_qwen3_native_registry(
                self.qwen_worker,
                raw_event_sink=raw_event_sink,
            )
        elif isinstance(effective, FunASRNanoWSProfile):
            if self.service_url is None:
                raise RuntimeError("Fun-ASR service URL is missing")
            registry = build_funasr_nano_ws_registry(
                self.service_url,
                raw_event_sink=raw_event_sink,
            )
        else:
            if self.service_url is None:
                raise RuntimeError("WLK service URL is missing")
            registry = build_wlk_registry(
                self.service_url,
                raw_event_sink=raw_event_sink,
            )
        return registry.create_streaming(effective, context)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.qwen_worker is not None:
            await self.qwen_worker.close()


def build_backend_runtime(
    profile: ASRProfile,
    *,
    repo_root: Path,
    model_dir: Path,
    funasr_engine: FunASRNanoPyTorchInference | None = None,
    sensevoice_engine: SenseVoiceNativeInference | None = None,
    qwen_worker: Qwen3NativeWorker | None = None,
) -> BenchmarkBackendRuntime:
    """按判别 profile 构造惰性 run 级资源，不在此处加载模型。"""
    service_url: str | None = None
    if isinstance(profile, FunASRNanoPyTorchProfile):
        funasr_engine = funasr_engine or FunASRNanoPyTorchEngine(
            model_dir=model_dir,
            device=profile.device,
            ncpu=profile.ncpu,
        )
    elif isinstance(profile, SenseVoiceNativeProfile):
        sensevoice_engine = sensevoice_engine or SenseVoiceNativeEngine(
            model_dir=model_dir,
            device="cpu",
            ncpu=profile.ncpu,
        )
    elif isinstance(profile, Qwen3NativeProfile):
        qwen_worker = qwen_worker or Qwen3NativeWorker(
            Qwen3NativeWorkerConfig(
                repo_root=repo_root,
                python_executable=profile.python_executable,
                model_dir=model_dir,
                device=profile.device,
                max_new_tokens=profile.max_new_tokens,
                timeout_secs=profile.timeout_secs,
            )
        )
    else:
        service_url = loopback_service_url(profile.host, profile.port)
    return BenchmarkBackendRuntime(
        profile=profile,
        repo_root=repo_root,
        model_dir=model_dir,
        service_url=service_url,
        funasr_engine=funasr_engine,
        sensevoice_engine=sensevoice_engine,
        qwen_worker=qwen_worker,
    )
