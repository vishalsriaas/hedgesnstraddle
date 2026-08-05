from __future__ import annotations

import frappe


def execute():
	settings = frappe.get_single("Straddle Bot Settings")
	settings.entry_window_open = "05:00:00"
	settings.entry_window_close = "07:00:00"
	settings.futures_entry_cutoff = "10:00:00"
	settings.squareoff_start = "11:00:00"
	settings.squareoff_end = "12:00:00"
	settings.futures_squareoff = "13:00:00"
	settings.min_strike_gap = 0
	settings.min_ask_per_leg = 0
	settings.max_total_ask = 500
	settings.max_premium_gap = 250
	settings.options_recovery_percent = 75
	settings.futures_tp_multiplier = 2
	settings.save(ignore_permissions=True)
