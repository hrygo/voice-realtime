#!/usr/bin/env bash
# 一键启动 voice-realtime 全套服务 (vr-bridge + vr-subtitles + vr-ui) 默认绑定 localhost: 127.0.0.1
set -euo pipefail

cd "$(dirname "$0")/.."
source "scripts/common.sh"

LAN_IP=$(get_lan_ip)

export VR_UI_HOST="$(resolve_bind_host "${VR_UI_HOST:-}" "127.0.0.1")"
export VR_UI_PORT="${VR_UI_PORT:-8100}"
export VR_BRIDGE_HOST="$(resolve_bind_host "${VR_BRIDGE_HOST:-}" "127.0.0.1")"
export VR_BRIDGE_PORT="${VR_BRIDGE_PORT:-8765}"
export VR_SUBTITLE_HOST="$(resolve_bind_host "${VR_SUBTITLE_HOST:-}" "127.0.0.1")"
export VR_SUBTITLE_PORT="${VR_SUBTITLE_PORT:-8001}"
export VR_MEETING_DATABASE_URL="${VR_MEETING_DATABASE_URL:-postgresql://voice_realtime_app@/knowledge}"
export VR_MEETING_SCHEMA="${VR_MEETING_SCHEMA:-voice_realtime}"
export VR_SUBTITLE_DIARIZATION_MODEL_PATH="${VR_SUBTITLE_DIARIZATION_MODEL_PATH:-runtime/sortformer.nemo}"

echo "========================================================"
echo "🚀  启动 voice-realtime 全套服务"
echo "========================================================"
if [[ "$VR_UI_HOST" == "127.0.0.1" || "$VR_UI_HOST" == "localhost" ]]; then
    echo "🔒 监听模式: 本机独占 (127.0.0.1，默认)"
    echo "🎙️   Voice Studio Web 控制台: http://127.0.0.1:${VR_UI_PORT}"
    echo "🔊  TTS 语音合成桥:         http://127.0.0.1:${VR_BRIDGE_PORT}"
    echo "📝  字幕识别服务:           ws://127.0.0.1:${VR_SUBTITLE_PORT}"
elif [[ "$VR_UI_HOST" == "0.0.0.0" ]]; then
    echo "🌐 监听模式: 全部网络接口 (0.0.0.0)"
    echo "🎙️   Voice Studio Web 控制台:"
    echo "    👉 本机访问: http://127.0.0.1:${VR_UI_PORT}"
    if [[ "$LAN_IP" != "127.0.0.1" ]]; then
        echo "    👉 局域网访问: http://${LAN_IP}:${VR_UI_PORT}"
    fi
    echo "🔊  TTS 语音合成桥: http://127.0.0.1:${VR_BRIDGE_PORT} / http://${LAN_IP}:${VR_BRIDGE_PORT}"
    echo "📝  字幕识别服务:   ws://127.0.0.1:${VR_SUBTITLE_PORT} / ws://${LAN_IP}:${VR_SUBTITLE_PORT}"
else
    echo "🏠 监听模式: 局域网/指定地址 (${VR_UI_HOST})"
    echo "🎙️   Voice Studio Web 控制台: http://${VR_UI_HOST}:${VR_UI_PORT} (本机: http://127.0.0.1:${VR_UI_PORT})"
    echo "🔊  TTS 语音合成桥:         http://${VR_BRIDGE_HOST}:${VR_BRIDGE_PORT}"
    echo "📝  字幕识别服务:           ws://${VR_SUBTITLE_HOST}:${VR_SUBTITLE_PORT}"
fi
echo "========================================================"
echo "按 Ctrl+C 停止所有服务"
echo ""

pids=()

cleanup() {
    echo ""
    echo "🛑 正在停止所有服务..."
    for pid in "${pids[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -TERM "$pid" 2>/dev/null || true
        fi
    done
    wait 2>/dev/null || true
    echo "✅ 服务已全部停止"
}

trap cleanup SIGINT SIGTERM EXIT

# 1. 启动 TTS 桥
uv run vr-bridge &
pids+=($!)

# 2. 启动字幕识别服务
uv run vr-subtitles &
pids+=($!)

# 3. 启动 UI 主服务
uv run vr-ui &
pids+=($!)

# 等待任意子进程退出
wait -n "${pids[@]}" 2>/dev/null || wait "${pids[@]}" 2>/dev/null || true
