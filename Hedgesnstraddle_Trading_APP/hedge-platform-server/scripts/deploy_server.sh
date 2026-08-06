#!/usr/bin/env bash
set -euo pipefail

SITE="${1:-${FRAPPE_SITE:-}}"
if [ -z "$SITE" ]; then
	echo "Usage: FRAPPE_SITE=site.name scripts/deploy_server.sh"
	echo "   or: scripts/deploy_server.sh site.name"
	exit 2
fi

cd "$(dirname "$0")/.."

scripts/install_apps.sh "$SITE"

if [ "${RESTART_BENCH:-1}" = "1" ]; then
	if bench restart; then
		echo "Bench restarted."
	else
		echo "bench restart failed. If this is a development bench, stop and rerun bench start manually."
		exit 3
	fi
else
	echo "Skipped bench restart because RESTART_BENCH=0."
fi

scripts/health_check.sh "$SITE"
