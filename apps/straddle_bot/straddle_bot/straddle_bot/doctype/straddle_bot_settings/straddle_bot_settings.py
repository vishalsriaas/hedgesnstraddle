import frappe
from frappe.model.document import Document
from frappe.utils import flt, cint

class StraddleBotSettings(Document):
	def after_save(self):
		self.sync_to_legacy_engine()

	def on_update(self):
		self.sync_to_legacy_engine()

	def sync_to_legacy_engine(self):
		keys_map = {
			"entry_window_open": "WINDOW_START",
			"entry_window_close": "WINDOW_END",
			"futures_entry_cutoff": "FUTURES_ENTRY_CUTOFF",
			"squareoff_start": "SQ_START",
			"squareoff_end": "SQ_END",
			"futures_squareoff": "FUTURES_SQUAREOFF",
			"expiry_time": "STRADDLE_EXPIRY_TIME",
			"trade_qty": "TRADE_QTY",
			"min_expiry_hours": "MIN_EXPIRY_HOURS",
			"min_strike_gap": "MIN_STRIKE_GAP",
			"max_total_ask": "MAX_TOTAL_MARK",
			"max_premium_gap": "MAX_PREMIUM_GAP",
			"futures_tp_multiplier": "FUTURES_TP_MULTIPLIER",
			"scan_interval_seconds": "SCAN_INTERVAL",
			"retry_timeout_seconds": "RETRY_TIMEOUT",
			"futures_leverage": "FUTURES_LEVERAGE",
			"futures_maintenance_margin_rate": "FUTURES_MM_RATE",
			"paper_wallet_usdt": "PAPER_WALLET_USDT",
		}

		for fieldname, legacy_key in keys_map.items():
			value = self.get(fieldname)
			if value is None or value == "":
				continue
			frappe.db.sql(
				"REPLACE INTO `straddle_config` (`key`, `value`) VALUES (%s, %s)",
				(legacy_key, str(value))
			)

		frappe.db.sql("REPLACE INTO `straddle_config` (`key`, `value`) VALUES ('_config_dirty', '1')")
		frappe.db.commit()
