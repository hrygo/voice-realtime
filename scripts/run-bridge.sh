#!/usr/bin/env bash
# 启动 qwen3-tts-openai 桥 (OpenAI 兼容 /v1/audio/speech，默认绑定 localhost: 127.0.0.1)
set -euo pipefail

cd "$(dirname "$0")/.."
source "scripts/common.sh"

LAN_IP=$(get_lan_ip)
export VR_BRIDGE_HOST="$(resolve_bind_host "${VR_BRIDGE_HOST:-}" "127.0.0.1")"
export VR_BRIDGE_PORT="${VR_BRIDGE_PORT:-8765}"

echo "========================================================"
echo "🔊  MLX Qwen3-TTS 语音合成桥"
echo "========================================================"
echo "🌐 监听地址: ${VR_BRIDGE_HOST}:${VR_BRIDGE_PORT}"
if [[ "$VR_BRIDGE_HOST" == "127.0.0.1" || "$VR_BRIDGE_HOST" == "localhost" ]]; then
    echo "🔒 访问模式: 本机独占 (Localhost Only，默认)"
    echo "👉 本机端点: http://127.0.0.1:${VR_BRIDGE_PORT}/v1/audio/speech"
elif [[ "$VR_BRIDGE_HOST" == "0.0.0.0" ]]; then
    echo "🌐 访问模式: 所有接口 (0.0.0.0)"
    echo "👉 本机端点: http://127.0.0.1:${VR_BRIDGE_PORT}/v1/audio/speech"
    if [[ "$LAN_IP" != "127.0.0.1" ]]; then
        echo "👉 局域网端点: http://${LAN_IP}:${VR_BRIDGE_PORT}/v1/audio/speech"
    fi
else
    echo "🏠 访问模式: 指定地址/局域网 (${VR_BRIDGE_HOST})"
    echo "👉 服务端点: http://${VR_BRIDGE_HOST}:${VR_BRIDGE_PORT}/v1/audio/speech"
fi
echo "📄  服务日志文件: runtime/logs/bridge.log"
echo "👉  实时查看日志: tail -n 50 -f runtime/logs/bridge.log"
echo "========================================================"

exec uv run vr-bridge "$@"