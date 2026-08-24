#!/usr/bin/env bash
# 启动 Voice Studio Web 控制台与主运行协调服务 (默认绑定 localhost: 127.0.0.1)
set -euo pipefail

cd "$(dirname "$0")/.."
source "scripts/common.sh"

LAN_IP=$(get_lan_ip)
export VR_UI_HOST="$(resolve_bind_host "${VR_UI_HOST:-}" "127.0.0.1")"
export VR_UI_PORT="${VR_UI_PORT:-8100}"
export VR_MEETING_DATABASE_URL="${VR_MEETING_DATABASE_URL:-postgresql://voice_realtime_app@/knowledge}"
export VR_MEETING_SCHEMA="${VR_MEETING_SCHEMA:-voice_realtime}"
export VR_SUBTITLE_DIARIZATION_MODEL_PATH="${VR_SUBTITLE_DIARIZATION_MODEL_PATH:-runtime/sortformer.nemo}"

echo "========================================================"
echo "🎙️  Voice Studio Web 控制台"
echo "========================================================"
echo "🌐 监听地址: ${VR_UI_HOST}:${VR_UI_PORT}"
if [[ "$VR_UI_HOST" == "127.0.0.1" || "$VR_UI_HOST" == "localhost" ]]; then
    echo "🔒 访问模式: 本机独占 (Localhost Only，默认)"
    echo "👉 本机访问: http://127.0.0.1:${VR_UI_PORT}"
elif [[ "$VR_UI_HOST" == "0.0.0.0" ]]; then
    echo "🌐 访问模式: 所有接口 (0.0.0.0)"
    echo "👉 本机访问: http://127.0.0.1:${VR_UI_PORT}"
    if [[ "$LAN_IP" != "127.0.0.1" ]]; then
        echo "👉 局域网访问: http://${LAN_IP}:${VR_UI_PORT}"
    fi
else
    echo "🏠 访问模式: 指定地址/局域网 (${VR_UI_HOST})"
    echo "👉 访问地址: http://${VR_UI_HOST}:${VR_UI_PORT}"
    if [[ "$VR_UI_HOST" != "127.0.0.1" ]]; then
        echo "👉 本机亦可: http://127.0.0.1:${VR_UI_PORT}"
    fi
fi
echo "========================================================"

exec uv run vr-ui "$@"
