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

# UI 内的交互管道是 TTS 桥的同机客户端。LAN/自定义地址必须连接桥的实际监听地址；
# 0.0.0.0 / :: 等通配监听则转换为可连接的回环地址。显式 URL 配置始终优先。
BRIDGE_CONNECT_HOST="$(resolve_connect_host "$VR_BRIDGE_HOST")"
BRIDGE_URL_HOST="$(format_url_host "$BRIDGE_CONNECT_HOST")"
export VR_INTERACTION_TTS_BRIDGE_URL="${VR_INTERACTION_TTS_BRIDGE_URL:-http://${BRIDGE_URL_HOST}:${VR_BRIDGE_PORT}/v1}"

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
cleanup_started=false

list_descendant_pids() {
    local parent_pid="$1"
    local child_pid
    for child_pid in $(pgrep -P "$parent_pid" 2>/dev/null || true); do
        list_descendant_pids "$child_pid"
        echo "$child_pid"
    done
}

cleanup() {
    if [[ "$cleanup_started" == "true" ]]; then
        return
    fi
    cleanup_started=true
    trap - SIGINT SIGTERM EXIT

    echo ""
    echo "🛑 正在停止所有服务..."
    local targets=()
    for pid in "${pids[@]}"; do
        while IFS= read -r descendant_pid; do
            if [[ -n "$descendant_pid" ]]; then
                targets+=("$descendant_pid")
            fi
        done < <(list_descendant_pids "$pid")
        targets+=("$pid")
    done

    # `uv run` 是一层包装进程。先向启动时捕获的完整进程树发送 TERM，
    # 防止包装进程先退出后 vr-subtitles / WLK 被重新托管到 PID 1。
    for target_pid in "${targets[@]}"; do
        if kill -0 "$target_pid" 2>/dev/null; then
            kill -TERM "$target_pid" 2>/dev/null || true
        fi
    done

    for _attempt in {1..50}; do
        local any_running=false
        for target_pid in "${targets[@]}"; do
            if kill -0 "$target_pid" 2>/dev/null; then
                any_running=true
                break
            fi
        done
        if [[ "$any_running" == "false" ]]; then
            break
        fi
        sleep 0.1
    done

    for target_pid in "${targets[@]}"; do
        if kill -0 "$target_pid" 2>/dev/null; then
            kill -KILL "$target_pid" 2>/dev/null || true
        fi
    done
    for pid in "${pids[@]}"; do
        wait "$pid" 2>/dev/null || true
    done
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
