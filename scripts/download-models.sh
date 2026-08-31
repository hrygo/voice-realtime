#!/usr/bin/env bash
# 下载语音链路所需模型（使用供应商标准外部缓存，不写入 Git 工作树）
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 下载 Qwen3-TTS (VoiceDesign) =="
uv run python - <<'PY'
from huggingface_hub import snapshot_download

p = snapshot_download("mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-bf16")
print(f"Qwen3-TTS 缓存: {p}")
PY

echo "== 下载 CAM++ 声纹识别模型 (3D-Speaker ONNX) =="
uv run python - <<'PY'
from huggingface_hub import hf_hub_download
p = hf_hub_download("csukuangfj/speaker-embedding-models", "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx")
print(f"CAM++ 声纹模型缓存: {p}")
PY

echo "TTS 与会议声纹模型就绪；ASR 模型由独立 SpeechRail 服务管理。"
