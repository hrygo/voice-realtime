#!/usr/bin/env bash
# scripts/common.sh: 共享网络探测与环境变量绑定解析工具
set -euo pipefail

# 获取本机当前活动局域网 IP
get_lan_ip() {
    local ip=""
    # 1. macOS: 优先查询默认出网路由网卡
    if [[ "$OSTYPE" == "darwin"* ]]; then
        local def_if
        def_if=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}' || true)
        if [[ -n "$def_if" ]]; then
            ip=$(ipconfig getifaddr "$def_if" 2>/dev/null || true)
        fi
        if [[ -z "$ip" ]]; then
            ip=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || ipconfig getifaddr bridge0 2>/dev/null || ipconfig getifaddr en2 2>/dev/null || ipconfig getifaddr en3 2>/dev/null || true)
        fi
    fi

    # 2. Linux: 优先查询 hostname -I 或 ip route
    if [[ -z "$ip" ]]; then
        ip=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
    fi
    if [[ -z "$ip" ]]; then
        ip=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7}' || true)
    fi

    # 3. Python 跨平台 socket 出网路由探测兜底
    if [[ -z "$ip" ]]; then
        ip=$(python3 -c "import socket; s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('1.1.1.1', 80)); print(s.getsockname()[0]); s.close()" 2>/dev/null || true)
    fi

    # 4. 最终兜底 127.0.0.1
    if [[ -z "$ip" ]]; then
        ip="127.0.0.1"
    fi
    echo "$ip"
}

# 获取全局绑定的目标变量（优先级：SONA_BIND_HOST > SONA_HOST > BIND_HOST > HOST）
get_global_bind_target() {
    local global_target="${SONA_BIND_HOST:-${SONA_HOST:-${BIND_HOST:-${HOST:-}}}}"
    echo "$global_target"
}

# 解析绑定地址：支持 localhost (默认)、0.0.0.0、lan、或具体 IP
# 用法: resolve_bind_host "$SPECIFIC_VAR" "$DEFAULT_FALLBACK"
resolve_bind_host() {
    local specific="${1:-}"
    local default_mode="${2:-127.0.0.1}"
    local global_target
    global_target=$(get_global_bind_target)

    # 优先级: 专用变量 > 全局变量 > 默认模式 (默认 127.0.0.1)
    local raw_target="${specific:-${global_target:-$default_mode}}"
    local lower_target
    lower_target=$(echo "$raw_target" | tr '[:upper:]' '[:lower:]' | xargs)

    case "$lower_target" in
        lan|lan_ip|local_network)
            # 通配监听同时覆盖本机回环与局域网；实际 LAN IP 仅用于启动横幅展示。
            echo "0.0.0.0"
            ;;
        localhost|local|loopback|127.0.0.1)
            echo "127.0.0.1"
            ;;
        0.0.0.0|all|any|\*)
            echo "0.0.0.0"
            ;;
        *)
            echo "$raw_target"
            ;;
    esac
}

# 将监听地址转换为同机客户端可连接的地址。
# 通配监听地址不能作为 HTTP 客户端目标；其余地址保持不变。
resolve_connect_host() {
    local listen_host="${1:-127.0.0.1}"
    local lower_host
    lower_host=$(echo "$listen_host" | tr '[:upper:]' '[:lower:]' | xargs)

    case "$lower_host" in
        0.0.0.0)
            echo "127.0.0.1"
            ;;
        ::|\[::\])
            echo "::1"
            ;;
        localhost|local|loopback)
            echo "127.0.0.1"
            ;;
        *)
            echo "$listen_host"
            ;;
    esac
}

# 将主机名格式化为 URL authority；IPv6 地址需要方括号。
format_url_host() {
    local host="${1:-127.0.0.1}"
    if [[ "$host" == *:* && "$host" != \[*\] ]]; then
        echo "[$host]"
    else
        echo "$host"
    fi
}
