#!/usr/bin/env bash
# 启动 WhisperLiveKit 字幕服务 (qwen3-streaming 后端)
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run vr-subtitles "$@"