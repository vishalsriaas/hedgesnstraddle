#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PID_FILE="logs/unified_proxy.pid"
if [ ! -f "$PID_FILE" ]; then
	echo "Unified proxy is not running."
	exit 0
fi

PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
	echo "Stopping unified proxy PID $PID..."
	kill "$PID"
else
	echo "Unified proxy PID $PID is not active."
fi
rm -f "$PID_FILE"
