from __future__ import annotations

import frappe


def after_install():
	seed_default_strategy_configs()


def seed_default_strategy_configs():
	defaults = [
		{
			"strategy_name": "Bullish Hedge",
			"strategy_key": "bull",
			"strategy_type": "Directional Hedge",
			"direction": "Bullish",
			"executor_name": "BullishExecutor_Paper",
			"trade_start_h": 4,
			"trade_start_m": 0,
			"trade_end_h": 6,
			"trade_end_m": 0,
			"force_close_h": 12,
			"force_close_m": 0,
			"contract_qty": 10,
			"max_premium": 220,
			"max_time_value": 219,
			"price_diff_percent": 0,
			"session_pnl_target": 600,
			"partial_profit_ratio": 1.1,
			"partial_tp_multiplier": 1.1,
			"rebuy_mode": "tv_based",
		},
		{
			"strategy_name": "Bearish Hedge",
			"strategy_key": "bear",
			"strategy_type": "Directional Hedge",
			"direction": "Bearish",
			"executor_name": "BearishExecutor_Paper",
			"trade_start_h": 4,
			"trade_start_m": 0,
			"trade_end_h": 6,
			"trade_end_m": 0,
			"force_close_h": 12,
			"force_close_m": 0,
			"contract_qty": 10,
			"max_premium": 220,
			"max_time_value": 219,
			"price_diff_percent": 0,
			"session_pnl_target": 600,
			"partial_profit_ratio": 1.1,
			"partial_tp_multiplier": 1.1,
			"rebuy_mode": "tv_based",
		},
	]

	for row in defaults:
		if frappe.db.exists("Hedge Strategy Config", row["strategy_name"]):
			continue
		frappe.get_doc({"doctype": "Hedge Strategy Config", **row}).insert(ignore_permissions=True)
