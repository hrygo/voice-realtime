#!/usr/bin/env bash
# 下载语音链路所需模型（HF 缓存统一由 huggingface-cli 管理）
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 下载 Qwen3-TTS (VoiceDesign) =="
uv run python - <<'PY'
from mlx_audio.tts.utils import load
m = load("mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-bf16")
print(f"Qwen3-TTS 加载成功: {m.config.tts_model_type}")
PY

echo "== 下载 SenseVoice (FunASR) =="
uv run python - <<'PY'
from funasr import AutoModel
m = AutoModel(model="iic/SenseVoiceSmall")
print("SenseVoice 就绪")
PY

echo "== 下载 Qwen3-ASR streaming (WhisperLiveKit) =="
uv run python - <<'PY'
from huggingface_hub import snapshot_download
p = snapshot_download("qfuxa/qwen3-asr-0.6b-streaming")
print(f"qwen3-asr streaming 缓存: {p}")
PY

echo "全部模型就绪"