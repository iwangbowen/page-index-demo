#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
#  service.sh  —  PageIndex webapp 服务管理脚本
#  用法:
#    ./service.sh start    启动服务（后台）
#    ./service.sh stop     停止服务
#    ./service.sh restart  重启服务
#    ./service.sh status   查看运行状态
#    ./service.sh logs     实时查看日志
#    ./service.sh dev      前台调试模式（热重载）
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── 配置 ──────────────────────────────────────────────────────────────────────
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv"
APP_MODULE="webapp.app:app"
HOST="${PAGEINDEX_HOST:-0.0.0.0}"
PORT="${PAGEINDEX_PORT:-9999}"
WORKERS="${PAGEINDEX_WORKERS:-1}"
LOG_DIR="$ROOT/logs"
LOG_FILE="$LOG_DIR/webapp.log"
PID_FILE="$LOG_DIR/webapp.pid"

mkdir -p "$LOG_DIR"

# ── 检查 venv ─────────────────────────────────────────────────────────────────
if [[ ! -f "$VENV/bin/activate" ]]; then
    echo "[ERROR] 虚拟环境不存在: $VENV"
    echo "        请先运行: python -m venv .venv && pip install -r requirements.txt"
    exit 1
fi

# shellcheck source=/dev/null
source "$VENV/bin/activate"
UVICORN="$VENV/bin/uvicorn"

# ── 工具函数 ──────────────────────────────────────────────────────────────────
_pid() {
    [[ -f "$PID_FILE" ]] && cat "$PID_FILE" || echo ""
}

_is_running() {
    local pid
    pid=$(_pid)
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

_wait_start() {
    local pid=$1
    local tries=20
    while (( tries-- > 0 )); do
        if curl -sf "http://127.0.0.1:$PORT/" > /dev/null 2>&1; then
            return 0
        fi
        sleep 0.5
    done
    echo "[WARN] 服务未在 10s 内响应，请查看日志: $LOG_FILE"
    return 1
}

# ── 命令 ──────────────────────────────────────────────────────────────────────
cmd_start() {
    if _is_running; then
        echo "[INFO] 服务已在运行 (PID $(_pid))，地址: http://$HOST:$PORT"
        return 0
    fi

    echo "[INFO] 启动 PageIndex webapp …"
    cd "$ROOT"
    nohup "$UVICORN" "$APP_MODULE" \
        --host "$HOST" \
        --port "$PORT" \
        --workers "$WORKERS" \
        --log-level info \
        >> "$LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"
    echo "[INFO] 进程 PID: $pid，日志: $LOG_FILE"

    if _wait_start "$pid"; then
        echo "[OK]  服务已启动 → http://$HOST:$PORT"
    fi
}

cmd_stop() {
    local pid
    pid=$(_pid)
    if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
        echo "[INFO] 服务未在运行"
        rm -f "$PID_FILE"
        return 0
    fi
    echo "[INFO] 停止服务 (PID $pid) …"
    kill "$pid"
    local tries=20
    while (( tries-- > 0 )) && kill -0 "$pid" 2>/dev/null; do
        sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "[WARN] 进程未退出，强制 kill"
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "[OK]  服务已停止"
}

cmd_restart() {
    cmd_stop
    sleep 1
    cmd_start
}

cmd_status() {
    if _is_running; then
        local pid
        pid=$(_pid)
        echo "[OK]  服务正在运行 (PID $pid) → http://$HOST:$PORT"
        echo "      日志文件: $LOG_FILE"
    else
        echo "[--]  服务未在运行"
        rm -f "$PID_FILE"
    fi
}

cmd_logs() {
    if [[ ! -f "$LOG_FILE" ]]; then
        echo "[INFO] 日志文件不存在: $LOG_FILE"
        exit 0
    fi
    echo "[INFO] 实时日志 (Ctrl+C 退出): $LOG_FILE"
    tail -n 80 -f "$LOG_FILE"
}

cmd_dev() {
    echo "[INFO] 开发模式（热重载）→ http://127.0.0.1:$PORT"
    cd "$ROOT"
    "$UVICORN" "$APP_MODULE" \
        --host 127.0.0.1 \
        --port "$PORT" \
        --reload \
        --log-level debug
}

# ── 入口 ──────────────────────────────────────────────────────────────────────
case "${1:-}" in
    start)   cmd_start   ;;
    stop)    cmd_stop    ;;
    restart) cmd_restart ;;
    status)  cmd_status  ;;
    logs)    cmd_logs    ;;
    dev)     cmd_dev     ;;
    *)
        echo "用法: $0 {start|stop|restart|status|logs|dev}"
        echo ""
        echo "  start    后台启动服务"
        echo "  stop     停止服务"
        echo "  restart  重启服务"
        echo "  status   查看运行状态"
        echo "  logs     实时查看日志"
        echo "  dev      前台调试模式（热重载）"
        echo ""
        echo "环境变量:"
        echo "  PAGEINDEX_HOST     监听地址（默认 0.0.0.0）"
        echo "  PAGEINDEX_PORT     端口（默认 8000）"
        echo "  PAGEINDEX_WORKERS  worker 数量（默认 1）"
        exit 1
        ;;
esac
