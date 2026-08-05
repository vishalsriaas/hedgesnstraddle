from __future__ import annotations

import frappe


def execute():
	settings = frappe.get_single("Straddle Bot Settings")
	settings.entry_window_open = "05:30:00"
	settings.entry_window_close = "08:00:00"
	settings.futures_entry_cutoff = "11:00:00"
	settings.squareoff_start = "11:00:00"
	settings.squareoff_end = "12:00:00"
	settings.futures_squareoff = "12:00:00"
	settings.trade_qty = 10
	settings.min_strike_gap = 0
	settings.max_total_ask = 400
	settings.max_premium_gap = 130
	settings.futures_tp_multiplier = 2
	settings.paper_wallet_usdt = 100000
	settings.save(ignore_permissions=True)

	values = {
		"WINDOW_START": "05:30",
		"WINDOW_END": "08:00",
		"FUTURES_ENTRY_CUTOFF": "11:00",
		"SQ_START": "11:00",
		"SQ_END": "12:00",
		"FUTURES_SQUAREOFF": "12:00",
		"TRADE_QTY": "10",
		"MIN_EXPIRY_HOURS": "0",
		"MIN_STRIKE_GAP": "0",
		"MAX_TOTAL_MARK": "400",
		"MAX_TOTAL_ASK": "400",
		"MAX_PREMIUM_GAP": "130",
		"FUTURES_TP_MULTIPLIER": "2",
		"PAPER_WALLET_USDT": "100000",
	}
	for key, value in values.items():
		if frappe.db.exists("Straddle Config Item", key):
			frappe.db.set_value(
				"Straddle Config Item",
				key,
				"value",
				value,
				update_modified=False,
			)
