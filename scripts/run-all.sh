#!/usr/bin/env bash
# 一键启动 sona UI；ASR/TTS 由外部 SpeechRail 提供。
# 等价于 scripts/sona-ctl.sh start（横幅、依赖健康检查、进程树清理均统一由 sona-ctl 提供）。
# 兼容用法: SONA_BIND_HOST=lan scripts/run-all.sh
set -euo pipefail

cd "$(dirname "$0")/.."
exec scripts/sona-ctl.sh start "$@"