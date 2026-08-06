from __future__ import annotations

import frappe


DEFAULT_CONFIG_ITEMS = [
	("WINDOW_START", "05:00", "Entry Window Open", "time", 0, "time", 1),
	("WINDOW_END", "07:00", "Entry Window Close", "time", 0, "time", 2),
	("FUTURES_ENTRY_CUTOFF", "11:00", "Futures Entry Cutoff", "time", 0, "time", 3),
	("SQ_START", "11:00", "Options Mark Recovery Window", "time", 0, "time", 4),
	("SQ_END", "12:00", "Universal Hard Squareoff", "time", 0, "time", 5),
	("FUTURES_SQUAREOFF", "12:00", "Futures Hard Squareoff", "time", 0, "time", 6),
	("EXPIRY_TIME", "13:30", "Expiry Time", "time", 0, "time", 5),
	("TRADE_QTY", "10", "BTC Quantity Per Leg", "number", 0, "trade", 10),
	("MIN_EXPIRY_HOURS", "6.0", "Minimum Expiry Hours", "number", 0, "entry", 20),
	("MIN_STRIKE_GAP", "500", "Minimum Strike Gap", "number", 0, "entry", 21),
	("MAX_TOTAL_ASK", "400", "Maximum Combined Mark Premium", "number", 0, "entry", 25),
	("MAX_PREMIUM_GAP", "100", "Maximum Premium Gap", "number", 0, "entry", 26),
	("FUTURES_TP_MULTIPLIER", "2", "Futures TP Multiplier", "number", 0, "exit", 28),
	("SCAN_INTERVAL", "2.0", "Scan Interval Seconds", "number", 0, "runtime", 30),
	("RETRY_TIMEOUT", "60", "Retry Timeout Seconds", "number", 0, "runtime", 31),
	("PAPER_TRADE", "1", "Paper Trading", "checkbox", 0, "runtime", 32),
	("FUTURES_LEVERAGE", "100", "Futures Leverage", "number", 0, "risk", 40),
	("FUTURES_MM_RATE", "0.004", "Futures Maintenance Margin Rate", "number", 0, "risk", 41),
	("PAPER_WALLET_USDT", "10000", "Paper Wallet USDT", "number", 0, "risk", 42),
]


def after_install():
	seed_default_config_items()
	seed_single_settings()
	seed_runtime_status()


def seed_default_config_items():
	for key, value, label, input_type, is_sensitive, section, sort_order in DEFAULT_CONFIG_ITEMS:
		if frappe.db.exists("Straddle Config Item", key):
			continue

		frappe.get_doc(
			{
				"doctype": "Straddle Config Item",
				"config_key": key,
				"value": value,
				"label": label,
				"input_type": input_type,
				"is_sensitive": is_sensitive,
				"section": section,
				"sort_order": sort_order,
			}
		).insert(ignore_permissions=True)


def seed_single_settings():
	settings = frappe.get_single("Straddle Bot Settings")
	settings.runtime_mode = settings.runtime_mode or "Paper"
	settings.bot_enabled = 0 if settings.bot_enabled is None else settings.bot_enabled
	settings.paper_trading_enabled = 1 if settings.paper_trading_enabled is None else settings.paper_trading_enabled
	settings.entry_window_open = settings.entry_window_open or "05:00:00"
	settings.entry_window_close = settings.entry_window_close or "07:00:00"
	settings.futures_entry_cutoff = "11:00:00"
	settings.squareoff_start = "11:00:00"
	settings.squareoff_end = "12:00:00"
	settings.futures_squareoff = "12:00:00"
	settings.expiry_time = settings.expiry_time or "13:30:00"
	settings.trade_qty = 10
	settings.min_expiry_hours = settings.min_expiry_hours or 6.0
	settings.min_strike_gap = 0
	settings.max_total_ask = 400
	settings.max_premium_gap = settings.max_premium_gap or 100
	settings.futures_tp_multiplier = settings.futures_tp_multiplier or 2
	settings.scan_interval_seconds = settings.scan_interval_seconds or 2.0
	settings.retry_timeout_seconds = settings.retry_timeout_seconds or 60
	settings.futures_leverage = settings.futures_leverage or 100
	settings.futures_maintenance_margin_rate = settings.futures_maintenance_margin_rate or 0.004
	settings.paper_wallet_usdt = settings.paper_wallet_usdt or 10000
	settings.save(ignore_permissions=True)


def seed_runtime_status():
	if frappe.db.exists("Straddle Runtime Status", "straddle_bot"):
		return

	frappe.get_doc(
		{
			"doctype": "Straddle Runtime Status",
			"component": "straddle_bot",
			"status": "Unknown",
			"mode": "Paper",
			"summary": "Waiting for worker heartbeat.",
		}
	).insert(ignore_permissions=True)
