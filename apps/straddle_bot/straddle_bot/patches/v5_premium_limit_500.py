from __future__ import annotations

import frappe


def execute():
	settings = frappe.get_single("Straddle Bot Settings")
	settings.max_total_ask = 400
	settings.max_premium_gap = 130
	settings.entry_window_open = "05:30:00"
	settings.entry_window_close = "08:00:00"
	settings.min_expiry_hours = 0
	settings.save(ignore_permissions=True)

	for key in ("MAX_TOTAL_MARK", "MAX_TOTAL_ASK"):
		if frappe.db.exists("Straddle Config Item", key):
			frappe.db.set_value(
				"Straddle Config Item",
				key,
				"value",
				"400",
				update_modified=False,
			)
	if frappe.db.exists("Straddle Config Item", "WINDOW_END"):
		frappe.db.set_value(
			"Straddle Config Item",
			"WINDOW_END",
			"value",
			"08:00",
			update_modified=False,
		)
	if frappe.db.exists("Straddle Config Item", "MIN_EXPIRY_HOURS"):
		frappe.db.set_value(
			"Straddle Config Item",
			"MIN_EXPIRY_HOURS",
			"value",
			"0",
			update_modified=False,
		)
	if frappe.db.exists("Straddle Config Item", "MAX_PREMIUM_GAP"):
		frappe.db.set_value(
			"Straddle Config Item",
			"MAX_PREMIUM_GAP",
			"value",
			"130",
			update_modified=False,
		)
