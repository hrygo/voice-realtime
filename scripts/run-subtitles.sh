#!/usr/bin/env bash
# 启动 WhisperLiveKit 字幕服务 (qwen3-streaming 后端，默认绑定 localhost: 127.0.0.1)
set -euo pipefail

cd "$(dirname "$0")/.."
source "scripts/common.sh"

LAN_IP=$(get_lan_ip)
export VR_SUBTITLE_HOST="$(resolve_bind_host "${VR_SUBTITLE_HOST:-}" "127.0.0.1")"
export VR_SUBTITLE_PORT="${VR_SUBTITLE_PORT:-8001}"

echo "========================================================"
echo "📝  WhisperLiveKit 实时语音识别与字幕服务"
echo "========================================================"
echo "🌐 监听地址: ${VR_SUBTITLE_HOST}:${VR_SUBTITLE_PORT}"
if [[ "$VR_SUBTITLE_HOST" == "127.0.0.1" || "$VR_SUBTITLE_HOST" == "localhost" ]]; then
    echo "🔒 访问模式: 本机独占 (Localhost Only，默认)"
    echo "👉 本机 WebSocket: ws://127.0.0.1:${VR_SUBTITLE_PORT}"
elif [[ "$VR_SUBTITLE_HOST" == "0.0.0.0" ]]; then
    echo "🌐 访问模式: 所有接口 (0.0.0.0)"
    echo "👉 本机 WebSocket: ws://127.0.0.1:${VR_SUBTITLE_PORT}"
    if [[ "$LAN_IP" != "127.0.0.1" ]]; then
        echo "👉 局域网 WebSocket: ws://${LAN_IP}:${VR_SUBTITLE_PORT}"
    fi
else
    echo "🏠 访问模式: 指定地址/局域网 (${VR_SUBTITLE_HOST})"
    echo "👉 服务 WebSocket: ws://${VR_SUBTITLE_HOST}:${VR_SUBTITLE_PORT}"
fi
echo "📄  服务日志文件: runtime/logs/subtitles.log"
echo "👉  实时查看日志: tail -n 50 -f runtime/logs/subtitles.log"
echo "========================================================"

exec uv run vr-subtitles "$@"