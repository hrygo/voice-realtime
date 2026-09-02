#!/usr/bin/env bash
# 下载 sona 自有的会议声纹模型（使用供应商标准外部缓存，不写入 Git 工作树）。
# ASR/TTS 模型快照由 SpeechRail 独立配置和管理，本脚本不下载它们。
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 下载 CAM++ 声纹识别模型 (3D-Speaker ONNX) =="
uv run python - <<'PY'
from huggingface_hub import hf_hub_download
p = hf_hub_download("csukuangfj/speaker-embedding-models", "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx")
print(f"CAM++ 声纹模型缓存: {p}")
PY

echo "会议声纹模型就绪；ASR/TTS 模型由独立 SpeechRail 服务管理。"
