#!/usr/bin/env python3
"""生成项目外 ASR Dev 专项语料；synthetic 证据不得进入正式 blind。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import shutil
import struct
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from voice_realtime.benchmarks.asr.manifest import (
    CorpusInputManifest,
    CorpusInputSample,
    CorpusReference,
    CorpusReferenceManifest,
    sha256_file,
    write_corpus_input_manifest,
    write_reference_manifest,
)
from voice_realtime.benchmarks.asr.metrics import normalize_primary_text

CorpusVersion = "synthetic-dev-special-v1-20260825"
NormalizationVersion = "nfkc-casefold-punct-space-v1"
_BYTES_PER_MILLISECOND = 32
_LICENSE = (
    "locally generated macOS system voice; internal evaluation only; "
    "not for redistribution"
)


@dataclass(frozen=True, slots=True)
class Utterance:
    key: str
    scenario: str
    language: str
    text: str
    hotwords: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Voice:
    name: str
    slug: str
    rate: int


_VOICES = (
    Voice("Tingting", "tingting", 190),
    Voice("Meijia", "meijia", 185),
    Voice("Sinji", "sinji", 190),
    Voice("Eddy (中文（中国大陆）)", "eddy-zh-cn", 195),
)

_UTTERANCES = (
    Utterance("short-continue", "dev-short", "zh", "继续。"),
    Utterance("short-cancel", "dev-short", "zh", "取消刚才的操作。"),
    Utterance("short-confirm", "dev-short", "zh", "确认并保存。"),
    Utterance("command-next", "dev-command", "zh", "停止播报，现在开始下一轮输入。"),
    Utterance("command-meeting", "dev-command", "zh", "打开会议助手并开始记录。"),
    Utterance("command-subtitle", "dev-command", "zh", "关闭语音助手，只保留实时字幕。"),
    Utterance(
        "entity-project",
        "dev-entity",
        "zh",
        "请记录项目代号玄武三号，负责人是欧阳子墨。",
        ("玄武三号", "欧阳子墨"),
    ),
    Utterance(
        "entity-product",
        "dev-entity",
        "zh-en",
        "Voice Studio 的 AudioHub 由林若安负责。",
        ("Voice Studio", "AudioHub", "林若安"),
    ),
    Utterance(
        "entity-model",
        "dev-entity",
        "zh-en",
        "对比 Qwen3 ASR、SenseVoice 和 Fun ASR Nano。",
        ("Qwen3 ASR", "SenseVoice", "Fun ASR Nano"),
    ),
    Utterance("number-date", "dev-number", "zh", "会议安排在二零二六年八月二十六日上午九点半。"),
    Utterance("number-money", "dev-number", "zh", "预算是十二万三千四百五十六元七角八分。"),
    Utterance(
        "number-percent",
        "dev-number",
        "zh",
        "准确率提升百分之五点二，延迟下降一百二十毫秒。",
    ),
    Utterance("number-phone", "dev-number", "zh", "联系电话是一三八零零一三八零零零。"),
    Utterance(
        "number-version",
        "dev-number",
        "zh-en",
        "请升级到版本一点零点零，提交编号是 A S R 二零七。",
    ),
    Utterance(
        "mixed-api",
        "dev-code-switch",
        "zh-en",
        "请检查 API gateway 的 health check 和 timeout 配置。",
    ),
    Utterance(
        "mixed-release",
        "dev-code-switch",
        "zh-en",
        "今天完成 release candidate，然后执行 smoke test。",
    ),
    Utterance(
        "mixed-metric",
        "dev-code-switch",
        "zh-en",
        "重点观察 P ninety five latency 和 word error rate。",
    ),
    Utterance(
        "long-meeting",
        "dev-long",
        "zh",
        "请把本次会议中关于交付时间、风险负责人、资源冲突和回退方案的讨论整理成行动项，并在每一项后面标注截止日期。",
    ),
    Utterance(
        "long-assistant",
        "dev-long",
        "zh",
        "当语音播报结束以后，系统应当立即恢复下一轮输入，同时保留必要的回声尾部抑制，不能因为等待状态不稳定而阻塞用户。",
    ),
    Utterance(
        "long-technical",
        "dev-long",
        "zh-en",
        "在同一台机器上依次运行 baseline 和 candidate，固定 chunk schedule、"
        "normalization version 与 analysis cluster，禁止并发加载模型。",
    ),
)


def _run(argv: list[str]) -> None:
    subprocess.run(argv, check=True, stdin=subprocess.DEVNULL)


def _canonical_pcm(path: Path) -> bytes:
    payload = path.read_bytes()
    usable = len(payload) - len(payload) % _BYTES_PER_MILLISECOND
    if usable <= 0:
        raise ValueError(f"generated PCM is empty: {path.name}")
    payload = payload[:usable]
    path.write_bytes(payload)
    path.chmod(0o600)
    return payload


def _write_pcm(path: Path, samples: list[int]) -> bytes:
    payload = struct.pack(f"<{len(samples)}h", *samples)
    usable = len(payload) - len(payload) % _BYTES_PER_MILLISECOND
    payload = payload[:usable]
    path.write_bytes(payload)
    path.chmod(0o600)
    return payload


def _decode_pcm(payload: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(payload) // 2}h", payload))


def _add_noise(payload: bytes, *, seed: str, snr_db: float = 10.0) -> list[int]:
    samples = _decode_pcm(payload)
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    noise_rms = max(rms / (10 ** (snr_db / 20)), 1.0)
    generator = random.Random(int(hashlib.sha256(seed.encode()).hexdigest()[:16], 16))
    return [
        max(-32768, min(32767, round(sample + generator.gauss(0.0, noise_rms))))
        for sample in samples
    ]


def _add_reverb(payload: bytes, *, delay_ms: int = 80, decay: float = 0.3) -> list[int]:
    samples = _decode_pcm(payload)
    delay = 16 * delay_ms
    output = samples.copy()
    for index in range(delay, len(output)):
        output[index] = max(
            -32768,
            min(32767, round(samples[index] + decay * samples[index - delay])),
        )
    return output


def _input_sample(
    *,
    sample_id: str,
    relative_path: str,
    payload_path: Path,
    scenario: str,
    language: str,
    content_group: str,
    speaker: str | None,
    tags: tuple[str, ...],
    hotwords: tuple[str, ...] = (),
) -> CorpusInputSample:
    digest = sha256_file(payload_path)
    return CorpusInputSample(
        sample_id=sample_id,
        audio_path=relative_path,
        source_sha256=digest,
        audio_sha256=digest,
        duration_ms=payload_path.stat().st_size // _BYTES_PER_MILLISECOND,
        session_id=f"dev-{speaker or 'negative'}",
        source_id=f"synthetic:{sample_id}",
        content_group_id=content_group,
        analysis_cluster_id=content_group,
        source_sample_rate_hz=16_000,
        scenario=scenario,
        language=language,
        license_or_consent=_LICENSE,
        speakers=() if speaker is None else (f"synthetic-{speaker}",),
        tags=("dev", "synthetic", *tags),
        hotwords=hotwords,
    )


def generate(*, output_root: Path, repository_root: Path) -> None:
    if output_root.resolve(strict=False).is_relative_to(repository_root.resolve(strict=True)):
        raise ValueError("Dev corpus output must be outside the repository")
    if output_root.exists():
        raise FileExistsError(f"Dev corpus output exists: {output_root}")
    output_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_root.name}.", dir=output_root.parent))
    staging.chmod(0o700)
    pcm_root = staging / "pcm"
    pcm_root.mkdir(mode=0o700)
    samples: list[CorpusInputSample] = []
    references: list[CorpusReference] = []
    clean_payloads: dict[str, bytes] = {}
    try:
        for voice in _VOICES:
            for utterance in _UTTERANCES:
                sample_id = f"dev-{voice.slug}-{utterance.key}"
                aiff = staging / f".{sample_id}.aiff"
                pcm = pcm_root / f"{sample_id}.pcm"
                _run(
                    [
                        "say",
                        "-v",
                        voice.name,
                        "-r",
                        str(voice.rate),
                        "-o",
                        str(aiff),
                        utterance.text,
                    ]
                )
                _run(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-y",
                        "-i",
                        str(aiff),
                        "-f",
                        "s16le",
                        "-acodec",
                        "pcm_s16le",
                        "-ar",
                        "16000",
                        "-ac",
                        "1",
                        str(pcm),
                    ]
                )
                aiff.unlink()
                payload = _canonical_pcm(pcm)
                if voice.slug == "tingting" and len(clean_payloads) < 8:
                    clean_payloads[utterance.key] = payload
                samples.append(
                    _input_sample(
                        sample_id=sample_id,
                        relative_path=f"pcm/{sample_id}.pcm",
                        payload_path=pcm,
                        scenario=utterance.scenario,
                        language=utterance.language,
                        content_group=f"dev-content-{utterance.key}",
                        speaker=voice.slug,
                        tags=("clean", utterance.scenario),
                        hotwords=utterance.hotwords,
                    )
                )
                references.append(
                    CorpusReference(
                        sample_id=sample_id,
                        reference_raw=utterance.text,
                        reference_normalized=normalize_primary_text(utterance.text),
                    )
                )

        by_key = {utterance.key: utterance for utterance in _UTTERANCES}
        for key, payload in clean_payloads.items():
            utterance = by_key[key]
            for transform, transformed in (
                ("noise-10db", _add_noise(payload, seed=key)),
                ("reverb-80ms", _add_reverb(payload)),
            ):
                sample_id = f"dev-tingting-{key}-{transform}"
                pcm = pcm_root / f"{sample_id}.pcm"
                _write_pcm(pcm, transformed)
                samples.append(
                    _input_sample(
                        sample_id=sample_id,
                        relative_path=f"pcm/{sample_id}.pcm",
                        payload_path=pcm,
                        scenario="dev-acoustic",
                        language=utterance.language,
                        content_group=f"dev-content-{key}",
                        speaker="tingting",
                        tags=(transform, utterance.scenario),
                        hotwords=utterance.hotwords,
                    )
                )
                references.append(
                    CorpusReference(
                        sample_id=sample_id,
                        reference_raw=utterance.text,
                        reference_normalized=normalize_primary_text(utterance.text),
                    )
                )

        for duration_ms in (1_000, 3_000, 10_000):
            sample_id = f"dev-silence-{duration_ms}ms"
            pcm = pcm_root / f"{sample_id}.pcm"
            _write_pcm(pcm, [0] * (16 * duration_ms))
            samples.append(
                _input_sample(
                    sample_id=sample_id,
                    relative_path=f"pcm/{sample_id}.pcm",
                    payload_path=pcm,
                    scenario="dev-negative",
                    language="zh",
                    content_group=sample_id,
                    speaker=None,
                    tags=("silence",),
                )
            )
            references.append(
                CorpusReference(sample_id=sample_id, reference_raw="", reference_normalized="")
            )

        input_path = staging / "dev.json"
        write_corpus_input_manifest(
            input_path,
            CorpusInputManifest(
                corpus_version=CorpusVersion,
                normalization_version=NormalizationVersion,
                split="dev",
                samples=tuple(samples),
            ),
        )
        reference_path = staging / "dev.references.json"
        write_reference_manifest(
            reference_path,
            CorpusReferenceManifest(
                corpus_version=CorpusVersion,
                normalization_version=NormalizationVersion,
                split="dev",
                input_manifest_sha256=sha256_file(input_path),
                samples=tuple(references),
            ),
        )
        provenance = {
            "schema_version": "1.0",
            "corpus_version": CorpusVersion,
            "evidence_class": "synthetic-dev-only",
            "formal_blind_eligible": False,
            "voices": [asdict(voice) for voice in _VOICES],
            "utterance_count": len(_UTTERANCES),
            "sample_count": len(samples),
            "acoustic_variants": ["noise-10db", "reverb-80ms"],
            "negative_samples": ["digital-silence-1s", "digital-silence-3s", "digital-silence-10s"],
            "input_manifest_sha256": sha256_file(input_path),
            "reference_manifest_sha256": sha256_file(reference_path),
        }
        (staging / "provenance.json").write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (staging / "provenance.json").chmod(0o600)
        staging.replace(output_root)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    os.umask(0o077)
    generate(output_root=args.output_root, repository_root=args.repo_root)
    print(json.dumps({"corpus_version": CorpusVersion, "output_root": str(args.output_root)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
