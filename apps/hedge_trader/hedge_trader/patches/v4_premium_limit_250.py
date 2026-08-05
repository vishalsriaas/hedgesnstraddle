from __future__ import annotations

import frappe


def execute():
	for strategy_name in ("Bullish Hedge", "Bearish Hedge"):
		if frappe.db.exists("Hedge Strategy Config", strategy_name):
			frappe.db.set_value(
				"Hedge Strategy Config",
				strategy_name,
				{
					"max_premium": 220,
					"trade_start_h": 5,
					"trade_start_m": 0,
					"trade_end_h": 7,
					"trade_end_m": 30,
				},
				update_modified=False,
			)
