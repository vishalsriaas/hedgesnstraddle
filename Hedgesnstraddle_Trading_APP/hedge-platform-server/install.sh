#!/usr/bin/env bash
set -euo pipefail

usage() {
	cat <<'EOF'
Usage:
  ./install.sh --bench /path/to/frappe-bench --site site.name

Options:
  --bench PATH       Existing Frappe v15 bench root
  --site NAME        Existing Frappe site to install into
  --start-runtimes   Start trading workers after installation (requires explicit confirmation)
  --no-restart       Do not run "bench restart"
  -h, --help         Show this help
EOF
}

PACKAGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_DIR=""
SITE=""
START_RUNTIMES=0
RESTART_BENCH=1

while [ "$#" -gt 0 ]; do
	case "$1" in
		--bench)
			BENCH_DIR="${2:-}"
			shift 2
			;;
		--site)
			SITE="${2:-}"
			shift 2
			;;
		--start-runtimes)
			START_RUNTIMES=1
			shift
			;;
		--no-restart)
			RESTART_BENCH=0
			shift
			;;
		-h|--help)
			usage
			exit 0
			;;
		*)
			echo "Unknown argument: $1" >&2
			usage >&2
			exit 2
			;;
	esac
done

if [ -z "$BENCH_DIR" ] || [ -z "$SITE" ]; then
	usage >&2
	exit 2
fi

BENCH_DIR="$(cd "$BENCH_DIR" 2>/dev/null && pwd)" || {
	echo "Bench directory does not exist: $BENCH_DIR" >&2
	exit 1
}

if [ ! -x "$BENCH_DIR/env/bin/python" ] || [ ! -d "$BENCH_DIR/apps/frappe" ]; then
	echo "Not a usable Linux Frappe bench: $BENCH_DIR" >&2
	exit 1
fi

if [ ! -d "$BENCH_DIR/sites/$SITE" ]; then
	echo "Frappe site does not exist: $BENCH_DIR/sites/$SITE" >&2
	echo "Create it first with: bench new-site $SITE" >&2
	exit 1
fi

if [ ! -d "$PACKAGE_DIR/apps/hedge_trader" ] || [ ! -d "$PACKAGE_DIR/apps/straddle_bot" ]; then
	echo "Package payload is incomplete; both app directories are required." >&2
	exit 1
fi

timestamp="$(date +%Y%m%d_%H%M%S)"
backup_dir="$BENCH_DIR/backups/hedge-platform-package-$timestamp"
mkdir -p "$backup_dir"

for app in hedge_trader straddle_bot; do
	if [ -e "$BENCH_DIR/apps/$app" ]; then
		echo "Backing up existing $app app..."
		cp -a "$BENCH_DIR/apps/$app" "$backup_dir/$app"
		rm -rf "$BENCH_DIR/apps/$app"
	fi
	cp -a "$PACKAGE_DIR/apps/$app" "$BENCH_DIR/apps/$app"
done

mkdir -p "$BENCH_DIR/scripts"
for script in "$PACKAGE_DIR"/scripts/*; do
	cp -a "$script" "$BENCH_DIR/scripts/"
done
chmod +x "$BENCH_DIR"/scripts/*.sh

cd "$BENCH_DIR"

env/bin/python -m pip install -e apps/hedge_trader -e apps/straddle_bot

touch sites/apps.txt
for app in frappe hedge_trader straddle_bot; do
	if ! grep -qx "$app" sites/apps.txt; then
		echo "$app" >> sites/apps.txt
	fi
done

install_app_if_needed() {
	local app="$1"
	if ! bench --site "$SITE" list-apps 2>/dev/null | awk '{print $1}' | grep -qx "$app"; then
		bench --site "$SITE" install-app "$app"
	fi
}

install_app_if_needed hedge_trader
install_app_if_needed straddle_bot

bench --site "$SITE" migrate
bench build --app hedge_trader
bench build --app straddle_bot

if [ "$RESTART_BENCH" = "1" ]; then
	bench restart
fi

FRAPPE_SITE="$SITE" scripts/health_check.sh

if [ "$START_RUNTIMES" = "1" ]; then
	echo
	echo "WARNING: this starts market-connected trading processes."
	printf "Type START TRADING to continue: "
	read -r confirmation
	if [ "$confirmation" != "START TRADING" ]; then
		echo "Runtime startup cancelled; application installation is complete."
		exit 0
	fi
	START_TRADING_WORKERS=1 FRAPPE_SITE="$SITE" scripts/start_runtime_workers.sh
fi

echo
echo "Hedge Platform installed successfully on site $SITE."
echo "Previous app sources, if any, were saved under: $backup_dir"
echo "Trading runtimes were not started unless --start-runtimes was supplied and confirmed."
