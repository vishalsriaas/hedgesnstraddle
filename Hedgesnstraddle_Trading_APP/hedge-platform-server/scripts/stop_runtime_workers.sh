#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

stop_pid() {
	local pid_file="$1"
	local name="$2"

	if [ ! -f "$pid_file" ]; then
		echo "$name is not running."
		return
	fi

	local pid
	pid="$(cat "$pid_file")"
	if kill -0 "$pid" 2>/dev/null; then
		echo "Stopping $name PID $pid..."
		kill "$pid"
	else
		echo "$name PID $pid is not active."
	fi
	rm -f "$pid_file"
}

stop_pid logs/hedge_legacy_api.pid "Hedge legacy API/runtime"
stop_pid logs/hedge_commands.pid "Hedge command monitor"
stop_pid logs/straddle_bot.pid "Straddle bot runtime"
stop_pid logs/straddle_commands.pid "Straddle command monitor"
stop_pid logs/straddle_dashboard.pid "Straddle dashboard"
stop_pid logs/unified_proxy.pid "Unified Frappe/panel proxy"
