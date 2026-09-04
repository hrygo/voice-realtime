#!/usr/bin/env bash
# scripts/sona-ctl.sh: sona 服务统一启停 / 状态 / 日志工具。
# 支持命令: start (前台/后台) / stop / restart / status / logs / help
# 兼容环境变量: 与 run-all.sh 一致（SONA_BIND_HOST / SONA_UI_PORT / SONA_* 均生效）。
set -euo pipefail

SONA_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/common.sh
source "$SONA_ROOT/scripts/common.sh"

RUNTIME_DIR="$SONA_ROOT/runtime"
LOG_DIR="$RUNTIME_DIR/logs"
PID_FILE="$RUNTIME_DIR/sona-ui.pid"
INTERACT_LOCK="$RUNTIME_DIR/interaction.lock"
UI_LOG="$LOG_DIR/ui.log"
UI_PORT="${SONA_UI_PORT:-8100}"
MAX_LOG_SIZE_BYTES=$((50 * 1024 * 1024)) # 50MB 触发轮转
CURL_TIMEOUT="--max-time 2"

# ---------------------------------------------------------------- 工具函数

usage() {
    cat <<'EOF'
用法: scripts/sona-ctl.sh <命令> [选项]

命令:
  start [--daemon|-d] [-- <sona-ui 参数...>]
      启动 sona UI 服务。默认前台运行（Ctrl+C 停止）；
      --daemon 后台运行，日志写入 runtime/logs/ui.log。
      `--` 之后的参数原样透传给 `uv run sona-ui`。
  stop
      停止 sona UI 服务（含 `uv run` 包装进程树与全部子进程）。
  restart [--daemon|-d]
      停止后重新启动（选项同 start）。
  status
      查看 sona UI 运行状态、端口监听与依赖服务健康。
  logs [--follow|-f] [行数]
      查看服务日志（默认最近 100 行；-f 实时跟随）。
  help
      显示本帮助。

环境变量: SONA_BIND_HOST / SONA_UI_HOST / SONA_UI_PORT / SONA_* 均按
run-all.sh 相同语义生效（例: SONA_BIND_HOST=lan scripts/sona-ctl.sh start -d）。
EOF
}

log_info() { printf '%s\n' "$*"; }

# 单进程存活探测
pid_alive() {
    local pid="$1"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

# pid 文件中的进程是否存活
pid_file_alive() {
    [[ -f "$PID_FILE" ]] || return 1
    local pid
    pid=$(cat "$PID_FILE" 2>/dev/null || true)
    pid_alive "$pid"
}

# 返回监听指定端口的进程 pid（无则为空）
port_pid() {
    local port="$1"
    lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | head -n 1 || true
}

# 递归收集某进程的全部后代 pid
list_descendant_pids() {
    local parent_pid="$1"
    local child_pid
    for child_pid in $(pgrep -P "$parent_pid" 2>/dev/null || true); do
        list_descendant_pids "$child_pid"
        echo "$child_pid"
    done
}

# 对根 pid 列表执行 TERM → 等待 → KILL 的进程树清理
stop_tree() {
    local roots=("$@")
    local targets=()
    local root pid d
    for root in "${roots[@]}"; do
        while IFS= read -r d; do
            [[ -n "$d" ]] && targets+=("$d")
        done < <(list_descendant_pids "$root")
        targets+=("$root")
    done

    for pid in "${targets[@]}"; do
        pid_alive "$pid" && kill -TERM "$pid" 2>/dev/null || true
    done
    for _ in {1..50}; do
        local any=false
        for pid in "${targets[@]}"; do
            if pid_alive "$pid"; then
                any=true
                break
            fi
        done
        [[ "$any" == "false" ]] && break
        sleep 0.1
    done
    for pid in "${targets[@]}"; do
        pid_alive "$pid" && kill -KILL "$pid" 2>/dev/null || true
    done
}

# HTTP 健康探测（仅告警不阻断）
check_http() {
    local name="$1" url="$2"
    if curl -sS $CURL_TIMEOUT -o /dev/null "$url" 2>/dev/null; then
        log_info "  ✅ $name 就绪 ($url)"
        return 0
    fi
    log_info "  ⚠️  $name 不可达 ($url)"
    return 1
}

speechrail_health() {
    local base="${SONA_SUBTITLE_SPEECHRAIL_URL:-ws://127.0.0.1:8201/v1/realtime}"
    local host
    host=$(echo "$base" | sed -E 's|^wss?://([^/]+).*|\1|')
    local proto="http"
    [[ "${base%%://*}" == "wss" ]] && proto="https"
    check_http "SpeechRail(ASR/TTS)" "$proto://$host/health"
}

lmstudio_health() {
    local base="${SONA_INTERACTION_LLM_BASE_URL:-http://localhost:1234/v1}"
    base="${base%/}"
    local probe
    case "$base" in
        */v1) probe="${base%/v1}/v1/models" ;;
        *) probe="$base/models" ;;
    esac
    check_http "LM Studio(LLM)" "$probe"
}

health_check() {
    log_info "── 依赖健康检查 ──────────────"
    speechrail_health || true
    lmstudio_health || true
    log_info ""
}

# 日志滚动：超过上限时保留一份 .1
rotate_log() {
    [[ -f "$UI_LOG" ]] || return 0
    local size
    size=$(stat -f%z "$UI_LOG" 2>/dev/null || stat -c%s "$UI_LOG" 2>/dev/null || echo 0)
    if (( size >= MAX_LOG_SIZE_BYTES )); then
        mv "$UI_LOG" "$UI_LOG.1"
        log_info "🔄 日志已轮转: ui.log → ui.log.1"
    fi
}

# 启动横幅（与 run-all.sh 原输出对齐）
print_banner() {
    local lan_ip
    lan_ip=$(get_lan_ip)
    local ui_host ui_port
    ui_host=$(resolve_bind_host "${SONA_UI_HOST:-}" "127.0.0.1")
    ui_port="${SONA_UI_PORT:-8100}"

    echo "========================================================"
    echo "🚀  启动 sona 全套服务"
    echo "========================================================"
    if [[ "$ui_host" == "127.0.0.1" || "$ui_host" == "localhost" ]]; then
        echo "🔒 监听模式: 本机独占 (127.0.0.1，默认)"
        echo "🎙️   Sona Web 控制台: http://127.0.0.1:${ui_port}"
    elif [[ "$ui_host" == "0.0.0.0" ]]; then
        echo "🌐 监听模式: 全部网络接口 (0.0.0.0)"
        echo "🎙️   Sona Web 控制台:"
        echo "    👉 本机访问: http://127.0.0.1:${ui_port}"
        if [[ "$lan_ip" != "127.0.0.1" ]]; then
            echo "    👉 局域网访问: http://${lan_ip}:${ui_port}"
        fi
    else
        echo "🏠 监听模式: 局域网/指定地址 (${ui_host})"
        echo "🎙️   Sona Web 控制台: http://${ui_host}:${ui_port} (本机: http://127.0.0.1:${ui_port})"
    fi
    echo "🔊  SpeechRail TTS:  ${SONA_INTERACTION_SPEECHRAIL_TTS_REST_URL:-http://127.0.0.1:8201/v1}"
    echo "📝  SpeechRail ASR:  ${SONA_SUBTITLE_SPEECHRAIL_URL:-ws://127.0.0.1:8201/v1/realtime}"
    echo "📄  服务日志: runtime/logs/ui.log"
    echo "========================================================"
}

# ---------------------------------------------------------------- 各子命令

start_cmd() {
    local daemon=false
    local extra=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -d | --daemon) daemon=true ;;
            --) shift; extra+=("$@"); break ;;
            -*) log_info "未知选项: $1"; usage; exit 2 ;;
            *) extra+=("$1") ;;
        esac
        shift
    done

    mkdir -p "$RUNTIME_DIR" "$LOG_DIR"

    # 幂等保护：已运行或端口被占则拒绝启动
    if pid_file_alive; then
        local running_pid
        running_pid=$(cat "$PID_FILE" 2>/dev/null || true)
        log_info "⚠️  sona UI 已在运行 (pid=$running_pid)，如需重启请先: scripts/sona-ctl.sh stop"
        exit 1
    fi
    local existing
    existing=$(port_pid "$UI_PORT")
    if [[ -n "$existing" ]]; then
        local existing_name
        existing_name=$(ps -o comm= -p "$existing" 2>/dev/null | xargs || true)
        log_info "⚠️  端口 ${UI_PORT} 已被占用 (pid=$existing ${existing_name:-未知进程})，请先释放或执行 stop"
        exit 1
    fi

    health_check
    print_banner
    rotate_log

    # cmd 数组始终非空（set -u 下空数组展开会报 unbound variable，macOS bash 3.2 尤甚）
    local cmd=(uv run sona-ui)
    if (( ${#extra[@]} > 0 )); then
        cmd+=("${extra[@]}")
    fi

    if [[ "$daemon" == "true" ]]; then
        nohup "${cmd[@]}" >>"$UI_LOG" 2>&1 &
        echo "$!" > "$PID_FILE"
        log_info "✅ sona UI 后台启动成功 (pid=$(cat "$PID_FILE"))"
        log_info "📄 日志: $UI_LOG  →  scripts/sona-ctl.sh logs -f"
        log_info "🛑 停止: scripts/sona-ctl.sh stop"
    else
        "${cmd[@]}" &
        local ui_pid=$!
        pids=("$ui_pid")
        echo "$ui_pid" > "$PID_FILE"
        log_info "🛑 按 Ctrl+C 停止所有服务"
        trap 'cleanup_fg' INT TERM EXIT
        wait "$ui_pid" 2>/dev/null || true
    fi
}

# 前台运行时的清理入口
cleanup_fg() {
    trap - INT TERM EXIT
    echo ""
    log_info "🛑 正在停止所有服务..."
    stop_tree "${pids[@]}"
    rm -f "$PID_FILE"
    log_info "✅ 服务已全部停止"
}

stop_cmd() {
    local pids=()

    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null || true)
        if pid_alive "$pid"; then
            pids+=("$pid")
        else
            rm -f "$PID_FILE" # 残留 pid 文件清理
        fi
    fi

    if (( ${#pids[@]} == 0 )); then
        local existing
        existing=$(port_pid "$UI_PORT")
        if [[ -n "$existing" ]]; then
            log_info "ℹ️  pid 文件缺失，按端口 ${UI_PORT} 定位进程 (pid=$existing)"
            pids+=("$existing")
        fi
    fi

    if (( ${#pids[@]} == 0 )); then
        log_info "✅ sona UI 未在运行"
        return 0
    fi

    log_info "🛑 正在停止 sona UI (pid: ${pids[*]}) ..."
    stop_tree "${pids[@]}"
    rm -f "$PID_FILE"
    log_info "✅ sona UI 已停止"
}

restart_cmd() {
    stop_cmd
    log_info ""
    start_cmd "$@"
}

status_cmd() {
    log_info "── sona UI ─────────────────●"
    if pid_file_alive; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null || true)
        local etime=""
        etime=$(ps -o etime= -p "$pid" 2>/dev/null | xargs || true)
        log_info "  ✅ 运行中  pid=$pid${etime:+ (已运行 $etime)}"
    else
        log_info "  ❌ 未运行"
    fi
    local listener
    listener=$(port_pid "$UI_PORT")
    if [[ -n "$listener" ]]; then
        local listener_name
        listener_name=$(ps -o comm= -p "$listener" 2>/dev/null | xargs || true)
        log_info "  🔌 端口 ${UI_PORT} 监听: pid=$listener (${listener_name:-未知进程})"
    else
        log_info "  🔌 端口 ${UI_PORT}: 无监听"
    fi
    log_info ""
    log_info "── 依赖服务 ─────────────────●"
    speechrail_health || true
    lmstudio_health || true
    log_info ""
    log_info "── 并发入口 ─────────────────●"
    # macOS 无 flock 命令；改用 lsof 探测锁文件持有者（flock 锁与打开的 fd 绑定，
    # 持锁进程必然保留 fd，lsof 可见）。
    if [[ -f "$INTERACT_LOCK" ]] && { lsof "$INTERACT_LOCK" 2>/dev/null | grep -q .; }; then
        log_info "  ℹ️  sona-interact: 运行中（已持有交互锁）"
    else
        log_info "  ℹ️  sona-interact: 未运行"
    fi
}

logs_cmd() {
    local follow=false
    local lines=100
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -f | --follow) follow=true ;;
            *) [[ "$1" =~ ^[0-9]+$ ]] && lines="$1" ;;
        esac
        shift
    done

    if [[ ! -f "$UI_LOG" ]]; then
        log_info "日志尚不存在: $UI_LOG（服务尚未启动过？）"
        return 0
    fi
    if [[ "$follow" == "true" ]]; then
        tail -n "$lines" -f "$UI_LOG"
    else
        tail -n "$lines" "$UI_LOG"
    fi
}

# ---------------------------------------------------------------- 入口分发

main() {
    [[ $# -ge 1 ]] || { usage; exit 2; }
    local cmd="$1"
    shift

    case "$cmd" in
        start) start_cmd "$@" ;;
        stop) stop_cmd ;;
        restart) restart_cmd "$@" ;;
        status) status_cmd ;;
        logs) logs_cmd "$@" ;;
        help | -h | --help) usage ;;
        *) log_info "未知命令: $cmd"; usage; exit 2 ;;
    esac
}

main "$@"