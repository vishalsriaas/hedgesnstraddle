from __future__ import annotations

import frappe


def execute():
	for strategy_name in ("Bullish Hedge", "Bearish Hedge"):
		if not frappe.db.exists("Hedge Strategy Config", strategy_name):
			continue
		frappe.db.set_value(
			"Hedge Strategy Config",
			strategy_name,
			{"force_close_h": 12, "force_close_m": 0},
			update_modified=False,
		)
