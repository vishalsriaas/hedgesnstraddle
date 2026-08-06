#!/usr/bin/env bash
set -euo pipefail

SITE="${1:-${FRAPPE_SITE:-}}"
if [ -z "$SITE" ]; then
	echo "Usage: FRAPPE_SITE=site.name scripts/install_apps.sh"
	echo "   or: scripts/install_apps.sh site.name"
	exit 2
fi

cd "$(dirname "$0")/.."

if [ ! -x "env/bin/python" ]; then
	echo "This folder does not look like a Linux Frappe bench with env/bin/python."
	echo "Create or restore the bench virtualenv first, then rerun this script."
	exit 1
fi

env/bin/python -m pip install -e apps/hedge_trader
env/bin/python -m pip install -e apps/straddle_bot

mkdir -p sites
touch sites/apps.txt

ensure_app_txt() {
	local app="$1"
	if ! grep -qx "$app" sites/apps.txt; then
		echo "$app" >> sites/apps.txt
	fi
}

ensure_app_txt frappe
ensure_app_txt hedge_trader
ensure_app_txt straddle_bot

is_installed() {
	bench --site "$SITE" list-apps 2>/dev/null | awk '{print $1}' | grep -qx "$1"
}

if ! is_installed hedge_trader; then
	bench --site "$SITE" install-app hedge_trader
fi

if ! is_installed straddle_bot; then
	bench --site "$SITE" install-app straddle_bot
fi

bench --site "$SITE" migrate
bench build --app hedge_trader
bench build --app straddle_bot

echo "Installed and migrated hedge_trader + straddle_bot on $SITE."
