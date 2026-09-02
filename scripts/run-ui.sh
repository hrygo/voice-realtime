#!/usr/bin/env bash
# 启动 Sona Web 控制台与主运行协调服务 (默认绑定 localhost: 127.0.0.1)
set -euo pipefail

cd "$(dirname "$0")/.."
source "scripts/common.sh"

LAN_IP=$(get_lan_ip)
export SONA_UI_HOST="$(resolve_bind_host "${SONA_UI_HOST:-}" "127.0.0.1")"
export SONA_UI_PORT="${SONA_UI_PORT:-8100}"
export SONA_MEETING_DATABASE_URL="${SONA_MEETING_DATABASE_URL:-postgresql://sona_app@/knowledge}"
export SONA_MEETING_SCHEMA="${SONA_MEETING_SCHEMA:-sona}"

echo "========================================================"
echo "🎙️  Sona Web 控制台"
echo "========================================================"
echo "🌐 监听地址: ${SONA_UI_HOST}:${SONA_UI_PORT}"
if [[ "$SONA_UI_HOST" == "127.0.0.1" || "$SONA_UI_HOST" == "localhost" ]]; then
    echo "🔒 访问模式: 本机独占 (Localhost Only，默认)"
    echo "👉 本机访问: http://127.0.0.1:${SONA_UI_PORT}"
elif [[ "$SONA_UI_HOST" == "0.0.0.0" ]]; then
    echo "🌐 访问模式: 所有接口 (0.0.0.0)"
    echo "👉 本机访问: http://127.0.0.1:${SONA_UI_PORT}"
    if [[ "$LAN_IP" != "127.0.0.1" ]]; then
        echo "👉 局域网访问: http://${LAN_IP}:${SONA_UI_PORT}"
    fi
else
    echo "🏠 访问模式: 指定地址/局域网 (${SONA_UI_HOST})"
    echo "👉 访问地址: http://${SONA_UI_HOST}:${SONA_UI_PORT}"
    if [[ "$SONA_UI_HOST" != "127.0.0.1" ]]; then
        echo "👉 本机亦可: http://127.0.0.1:${SONA_UI_PORT}"
    fi
fi
echo "📄  服务日志文件: runtime/logs/ui.log"
echo "👉  实时查看日志: tail -n 50 -f runtime/logs/ui.log"
echo "========================================================"

exec uv run sona-ui "$@"
