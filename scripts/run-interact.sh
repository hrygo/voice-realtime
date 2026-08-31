#!/usr/bin/env bash
# 启动 Pipecat 语音交互管道 (SpeechRail STT → LM Studio → TTS 桥 → 播放)
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run vr-interact "$@"
