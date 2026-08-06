#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p logs

UNIFIED_PROXY_PORT="${UNIFIED_PROXY_PORT:-9100}"
UNIFIED_PROXY_HOST="${UNIFIED_PROXY_HOST:-127.0.0.1}"
PID_FILE="logs/unified_proxy.pid"
LOG_FILE="logs/unified_proxy.log"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
	echo "Unified proxy already running with PID $(cat "$PID_FILE")."
	exit 0
fi

if [ ! -x "env/bin/python" ]; then
	echo "This folder does not look like a Linux Frappe bench with env/bin/python."
	exit 1
fi

nohup env/bin/python scripts/unified_proxy.py \
	--host "$UNIFIED_PROXY_HOST" \
	--port "$UNIFIED_PROXY_PORT" \
	>> "$LOG_FILE" 2>&1 &

echo $! > "$PID_FILE"
echo "Unified proxy started: http://$UNIFIED_PROXY_HOST:$UNIFIED_PROXY_PORT"
