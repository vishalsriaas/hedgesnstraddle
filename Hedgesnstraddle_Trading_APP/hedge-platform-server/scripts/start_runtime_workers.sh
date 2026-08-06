#!/usr/bin/env bash
set -euo pipefail

SITE="${1:-${FRAPPE_SITE:-}}"
if [ -z "$SITE" ]; then
	echo "Usage: START_TRADING_WORKERS=1 FRAPPE_SITE=site.name scripts/start_runtime_workers.sh"
	echo "   or: START_TRADING_WORKERS=1 scripts/start_runtime_workers.sh site.name"
	exit 2
fi

if [ "${START_TRADING_WORKERS:-0}" != "1" ]; then
	echo "Refusing to start trading runtimes until START_TRADING_WORKERS=1 is set."
	echo "This avoids accidentally starting market-connected loops during deploy or testing."
	exit 2
fi

cd "$(dirname "$0")/.."
mkdir -p logs

WORKER_ID="${WORKER_ID:-server}"
HEDGE_RUNTIME_PORT="${HEDGE_RUNTIME_PORT:-8100}"
STRADDLE_DASHBOARD_PORT="${STRADDLE_DASHBOARD_PORT:-8080}"

start_proc() {
	local name="$1"
	local pid_file="$2"
	local log_file="$3"
	shift 3

	if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
		echo "$name already running with PID $(cat "$pid_file")."
		return
	fi

	echo "Starting $name..."
	nohup "$@" >> "$log_file" 2>&1 &
	echo $! > "$pid_file"
	echo "$name PID $(cat "$pid_file"), log $log_file"
}

start_proc "Hedge legacy API/runtime" logs/hedge_legacy_api.pid logs/hedge_legacy_api.log \
	env/bin/python -m hedge_trader.feeds.worker \
		--site "$SITE" \
		--worker-id "$WORKER_ID-hedge-api" \
		--serve-legacy-api \
		--legacy-api-port "$HEDGE_RUNTIME_PORT"

start_proc "Hedge command monitor" logs/hedge_commands.pid logs/hedge_commands.log \
	env/bin/python -m hedge_trader.feeds.worker \
		--site "$SITE" \
		--worker-id "$WORKER_ID-hedge-commands"

start_proc "Straddle bot runtime" logs/straddle_bot.pid logs/straddle_bot.log \
	env/bin/python -m straddle_bot.feeds.worker \
		--site "$SITE" \
		--worker-id "$WORKER_ID-straddle-bot" \
		--mode bot

start_proc "Straddle command monitor" logs/straddle_commands.pid logs/straddle_commands.log \
	env/bin/python -m straddle_bot.feeds.worker \
		--site "$SITE" \
		--worker-id "$WORKER_ID-straddle-commands" \
		--mode command-monitor

if [ "${START_STRADDLE_DASHBOARD:-1}" = "1" ]; then
	start_proc "Straddle dashboard" logs/straddle_dashboard.pid logs/straddle_dashboard.log \
		env/bin/python -m straddle_bot.feeds.worker \
			--site "$SITE" \
			--worker-id "$WORKER_ID-straddle-dashboard" \
			--mode dashboard \
			--dashboard-port "$STRADDLE_DASHBOARD_PORT"
fi

if [ "${START_UNIFIED_PROXY:-0}" = "1" ]; then
	UNIFIED_PROXY_PORT="${UNIFIED_PROXY_PORT:-9100}"
	start_proc "Unified Frappe/panel proxy" logs/unified_proxy.pid logs/unified_proxy.log \
		env/bin/python scripts/unified_proxy.py \
			--host "${UNIFIED_PROXY_HOST:-127.0.0.1}" \
			--port "$UNIFIED_PROXY_PORT"
	echo "Unified Frappe URL: http://${UNIFIED_PROXY_HOST:-127.0.0.1}:$UNIFIED_PROXY_PORT"
fi

echo "Runtime workers requested. Hedge API: http://127.0.0.1:$HEDGE_RUNTIME_PORT"
echo "Straddle dashboard: http://127.0.0.1:$STRADDLE_DASHBOARD_PORT"
