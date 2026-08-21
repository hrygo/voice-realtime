#!/usr/bin/env bash
# 下载语音链路所需模型（HF 缓存统一由 huggingface-cli 管理）
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 下载 Qwen3-TTS (VoiceDesign) =="
uv run python - <<'PY'
from huggingface_hub import snapshot_download

p = snapshot_download("mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-bf16")
print(f"Qwen3-TTS 缓存: {p}")
PY

echo "== 下载 SenseVoice (FunASR) =="
uv run python - <<'PY'
# modelscope 在该环境被 SSRF 拦截 → 统一经 HuggingFace 快照落到本地缓存，
# pipecat 的 FunASRSTTService 用本地路径加载（pipeline._resolve_stt_model）。
from huggingface_hub import snapshot_download
p = snapshot_download("FunAudioLLM/SenseVoiceSmall")
print(f"SenseVoice 缓存: {p}")
PY

echo "== 下载 Qwen3-ASR streaming (WhisperLiveKit) =="
uv run python - <<'PY'
from huggingface_hub import snapshot_download

p = snapshot_download(
    "Qwen/Qwen3-ASR-1.7B",
    local_dir="runtime/qwen3-asr-1.7b",
)
print(f"Qwen3-ASR 1.7B 本地目录: {p}")
PY

echo "全部模型就绪"
