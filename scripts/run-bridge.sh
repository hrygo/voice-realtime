#!/usr/bin/env bash
# 启动 qwen3-tts-openai 桥 (OpenAI 兼容 /v1/audio/speech)
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run vr-bridge "$@"