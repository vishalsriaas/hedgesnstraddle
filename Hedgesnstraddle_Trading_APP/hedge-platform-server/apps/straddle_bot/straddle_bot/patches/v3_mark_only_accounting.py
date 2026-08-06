import frappe


def execute():
	settings = frappe.get_single("Straddle Bot Settings")
	settings.entry_window_open = "05:00:00"
	settings.entry_window_close = "07:00:00"
	settings.futures_entry_cutoff = "11:00:00"
	settings.squareoff_start = "11:00:00"
	settings.squareoff_end = "12:00:00"
	settings.futures_squareoff = "12:00:00"
	settings.trade_qty = 10
	settings.max_total_ask = 400
	settings.save(ignore_permissions=True)

	values = {
		"WINDOW_START": "05:00",
		"WINDOW_END": "07:00",
		"FUTURES_ENTRY_CUTOFF": "11:00",
		"SQ_START": "11:00",
		"SQ_END": "12:00",
		"FUTURES_SQUAREOFF": "12:00",
		"TRADE_QTY": "10",
		"MAX_TOTAL_ASK": "400",
	}
	for key, value in values.items():
		if frappe.db.exists("Straddle Config Item", key):
			frappe.db.set_value("Straddle Config Item", key, "value", value, update_modified=False)

	for key in ("MAX_ASK_MARK_PCT", "MAX_ASK_MARK_RETRY", "MIN_ASK_PER_LEG", "OPTIONS_RECOVERY_PCT"):
		if frappe.db.exists("Straddle Config Item", key):
			frappe.delete_doc("Straddle Config Item", key, ignore_permissions=True)
