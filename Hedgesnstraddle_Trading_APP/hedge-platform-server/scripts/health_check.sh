#!/usr/bin/env bash
set -euo pipefail

SITE="${1:-${FRAPPE_SITE:-}}"
if [ -z "$SITE" ]; then
	echo "Usage: FRAPPE_SITE=site.name scripts/health_check.sh"
	echo "   or: scripts/health_check.sh site.name"
	exit 2
fi

cd "$(dirname "$0")/.."

echo "Bench app list for $SITE:"
bench --site "$SITE" list-apps

SITE="$SITE" env/bin/python - <<'PY'
import os
import sys

import frappe

site = os.environ["SITE"]
required = {
	"hedge_trader": [
		"Hedge Trader Settings",
		"Hedge Strategy Config",
		"Hedge Runtime Command",
		"Hedge Runtime Status",
		"Hedge Trading Session",
		"Hedge Trade Order",
		"Hedge Open Position",
		"Hedge Paper Ledger Entry",
		"Hedge Session Event",
		"Hedge Order Block Zone",
		"Hedge Macro Event",
		"Hedge System Health Snapshot",
	],
	"straddle_bot": [
		"Straddle Bot Settings",
		"Straddle Config Item",
		"Straddle Runtime Command",
		"Straddle Runtime Status",
		"Straddle Trading Session",
		"Straddle Trade Order",
		"Straddle Fill",
		"Straddle PnL Snapshot",
		"Straddle Session Event",
		"Straddle Wallet Ledger Entry",
	],
}
workspaces = ["Hedge Trader", "Straddle Bot"]
pages = ["hedge-panel", "straddle-dashboard"]

frappe.init(site=site, sites_path="sites")
frappe.connect()
try:
	installed = set(frappe.get_installed_apps())
	print("Installed apps:", ", ".join(sorted(installed)))
	failed = False
	for app, doctypes in required.items():
		if app not in installed:
			print(f"ERROR: {app} is not installed on {site}")
			failed = True
		missing = [doctype for doctype in doctypes if not frappe.db.exists("DocType", doctype)]
		if missing:
			print(f"ERROR: missing DocTypes for {app}: {', '.join(missing)}")
			failed = True
		else:
			print(f"OK: {app} DocTypes present ({len(doctypes)})")

	for workspace in workspaces:
		if not frappe.db.exists("Workspace", workspace):
			print(f"ERROR: Workspace missing: {workspace}")
			failed = True
			continue
		doc = frappe.get_doc("Workspace", workspace)
		print(f"OK: {workspace} workspace shortcuts: {len(getattr(doc, 'shortcuts', []) or [])}")

	for page in pages:
		if not frappe.db.exists("Page", page):
			print(f"ERROR: Page missing: {page}")
			failed = True
		else:
			print(f"OK: Page present: /app/{page}")

	for doctype in ("Hedge Runtime Status", "Straddle Runtime Status"):
		if frappe.db.exists("DocType", doctype):
			rows = frappe.get_all(doctype, fields=["component", "status", "last_heartbeat"], limit_page_length=20)
			print(f"{doctype}: {len(rows)} status row(s)")
			for row in rows:
				print(f"  - {row.component}: {row.status} {row.last_heartbeat or ''}")

	if failed:
		sys.exit(1)
finally:
	frappe.destroy()
PY

if command -v curl >/dev/null 2>&1; then
	FRAPPE_PORT="${FRAPPE_PORT:-8000}"
	curl -fsS "http://127.0.0.1:$FRAPPE_PORT/app/hedge-trader" >/dev/null \
		&& echo "OK: Frappe route /app/hedge-trader responds on port $FRAPPE_PORT" \
		|| echo "WARN: Frappe route /app/hedge-trader not reachable on port $FRAPPE_PORT"
	curl -fsS "http://127.0.0.1:$FRAPPE_PORT/app/straddle-bot" >/dev/null \
		&& echo "OK: Frappe route /app/straddle-bot responds on port $FRAPPE_PORT" \
		|| echo "WARN: Frappe route /app/straddle-bot not reachable on port $FRAPPE_PORT"
	curl -fsS "http://127.0.0.1:$FRAPPE_PORT/app/hedge-panel" >/dev/null \
		&& echo "OK: Frappe route /app/hedge-panel responds on port $FRAPPE_PORT" \
		|| echo "WARN: Frappe route /app/hedge-panel not reachable on port $FRAPPE_PORT"
	curl -fsS "http://127.0.0.1:$FRAPPE_PORT/app/straddle-dashboard" >/dev/null \
		&& echo "OK: Frappe route /app/straddle-dashboard responds on port $FRAPPE_PORT" \
		|| echo "WARN: Frappe route /app/straddle-dashboard not reachable on port $FRAPPE_PORT"
fi
