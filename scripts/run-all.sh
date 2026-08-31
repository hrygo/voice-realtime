#!/usr/bin/env bash
# 一键启动 voice-realtime UI；ASR/TTS 都由外部 SpeechRail 提供。
set -euo pipefail

cd "$(dirname "$0")/.."
source "scripts/common.sh"

LAN_IP=$(get_lan_ip)

export VR_UI_HOST="$(resolve_bind_host "${VR_UI_HOST:-}" "127.0.0.1")"
export VR_UI_PORT="${VR_UI_PORT:-8100}"
export VR_SUBTITLE_SPEECHRAIL_URL="${VR_SUBTITLE_SPEECHRAIL_URL:-ws://127.0.0.1:8201/v2/realtime}"
export VR_INTERACTION_SPEECHRAIL_REALTIME_URL="${VR_INTERACTION_SPEECHRAIL_REALTIME_URL:-$VR_SUBTITLE_SPEECHRAIL_URL}"
export VR_INTERACTION_SPEECHRAIL_TTS_REST_URL="${VR_INTERACTION_SPEECHRAIL_TTS_REST_URL:-http://127.0.0.1:8201/v1}"
export VR_MEETING_DATABASE_URL="${VR_MEETING_DATABASE_URL:-postgresql://voice_realtime_app@/knowledge}"
export VR_MEETING_SCHEMA="${VR_MEETING_SCHEMA:-voice_realtime}"


echo "========================================================"
echo "🚀  启动 voice-realtime 全套服务"
echo "========================================================"
if [[ "$VR_UI_HOST" == "127.0.0.1" || "$VR_UI_HOST" == "localhost" ]]; then
    echo "🔒 监听模式: 本机独占 (127.0.0.1，默认)"
    echo "🎙️   Voice Studio Web 控制台: http://127.0.0.1:${VR_UI_PORT}"
    echo "🔊  SpeechRail TTS:          ${VR_INTERACTION_SPEECHRAIL_TTS_REST_URL}"
    echo "📝  SpeechRail ASR:          ${VR_SUBTITLE_SPEECHRAIL_URL}"
elif [[ "$VR_UI_HOST" == "0.0.0.0" ]]; then
    echo "🌐 监听模式: 全部网络接口 (0.0.0.0)"
    echo "🎙️   Voice Studio Web 控制台:"
    echo "    👉 本机访问: http://127.0.0.1:${VR_UI_PORT}"
    if [[ "$LAN_IP" != "127.0.0.1" ]]; then
        echo "    👉 局域网访问: http://${LAN_IP}:${VR_UI_PORT}"
    fi
    echo "🔊  SpeechRail TTS:   ${VR_INTERACTION_SPEECHRAIL_TTS_REST_URL}"
    echo "📝  SpeechRail ASR:   ${VR_SUBTITLE_SPEECHRAIL_URL}"
else
    echo "🏠 监听模式: 局域网/指定地址 (${VR_UI_HOST})"
    echo "🎙️   Voice Studio Web 控制台: http://${VR_UI_HOST}:${VR_UI_PORT} (本机: http://127.0.0.1:${VR_UI_PORT})"
    echo "🔊  SpeechRail TTS:          ${VR_INTERACTION_SPEECHRAIL_TTS_REST_URL}"
    echo "📝  SpeechRail ASR:          ${VR_SUBTITLE_SPEECHRAIL_URL}"
fi
echo "📄  服务日志目录:           runtime/logs/"
echo "    - UI 控制台日志:        runtime/logs/ui.log"
echo "    - ASR/TTS 服务日志:      由 SpeechRail 独立管理"
echo "👉  实时跟踪日志: tail -n 50 -f runtime/logs/*.log"
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
    # `uv run` 是包装进程；先终止完整子进程树，避免 TTS/UI 被重新托管到 PID 1。
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

# 启动 UI 主服务；SpeechRail 由独立 supervisor 管理。
uv run vr-ui &
pids+=($!)

# 等待任意子进程退出
wait -n "${pids[@]}" 2>/dev/null || wait "${pids[@]}" 2>/dev/null || true
