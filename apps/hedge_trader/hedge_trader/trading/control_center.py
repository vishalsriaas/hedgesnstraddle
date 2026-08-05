"""Unified Frappe control-plane API for Hedge and Straddle.

Only durable/operator work lives here. Market loops and exchange execution stay
inside the dedicated runtime workers.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

OPERATOR_ROLES = {"System Manager", "Trading Operator", "Trading Manager"}
VIEWER_ROLES = OPERATOR_ROLES | {"Trading Viewer"}
DESTRUCTIVE_COMMANDS = {"FORCE_CLOSE", "SQUARE_OFF", "EMERGENCY_SQUARE_OFF"}


def _require_operator() -> None:
	roles = set(frappe.get_roles())
	if not roles.intersection(OPERATOR_ROLES):
		frappe.throw("Trading Operator or System Manager role is required.", frappe.PermissionError)


def _require_viewer() -> None:
	if not set(frappe.get_roles()).intersection(VIEWER_ROLES):
		frappe.throw("Trading Viewer role is required.", frappe.PermissionError)


def _table(name: str) -> bool:
	try:
		return bool(frappe.db.table_exists(name))
	except Exception:
		return False


def _rows(sql: str, values: tuple = ()) -> list[dict]:
	try:
		return frappe.db.sql(sql, values, as_dict=True)
	except Exception:
		return []


def _json(value: Any, default: Any = None) -> Any:
	if not value:
		return default
	if isinstance(value, (dict, list)):
		return value
	try:
		return json.loads(value)
	except (TypeError, ValueError):
		return default


def _heartbeat_rows(doctype: str) -> list[dict]:
	if not frappe.db.exists("DocType", doctype):
		return []
	return frappe.get_all(
		doctype,
		fields=["component", "status", "last_heartbeat", "worker_id", "mode", "summary"],
		order_by="last_heartbeat desc",
		limit_page_length=50,
	)


def _health() -> dict:
	now = now_datetime()
	components = []
	for algo, doctype in (
		("Hedge", "Hedge Runtime Status"),
		("Straddle", "Straddle Runtime Status"),
	):
		for row in _heartbeat_rows(doctype):
			heartbeat = row.get("last_heartbeat")
			age = (now - heartbeat).total_seconds() if heartbeat else None
			row.update(
				{
					"algo": algo,
					"age_seconds": round(age, 1) if age is not None else None,
					"healthy": bool(age is not None and age <= 90 and row.get("status") in {"OK", "Running", "Paused"}),
				}
			)
			components.append(row)

	required = {
		"hedge_kv_state": _table("hedge_kv_state"),
		"hedge_paper_trades": _table("hedge_paper_trades"),
		"straddle_sessions": _table("straddle_sessions"),
		"straddle_wallet_ledger": _table("straddle_wallet_ledger"),
	}
	return {
		"database": {"healthy": all(required.values()), "tables": required},
		"components": components,
		"healthy": all(required.values()) and bool(components) and all(row["healthy"] for row in components),
	}


def _hedge_summary() -> dict:
	sessions = _rows(
		"""SELECT session_id,trader_name,session_date,status,entry_price,futures_pnl,
		          hedge_pnl,total_pnl,balance_before,balance_after,entry_ts_ist,close_ts_ist
		   FROM hedge_trading_sessions ORDER BY id DESC LIMIT 20"""
	) if _table("hedge_trading_sessions") else []
	trades = _rows(
		"""SELECT id,trader_name,session_id,ts_ist,action,symbol,side,qty,fill_price,pnl
		   FROM hedge_paper_trades ORDER BY id DESC LIMIT 30"""
	) if _table("hedge_paper_trades") else []
	states = {}
	if _table("hedge_kv_state"):
		for row in _rows(
			"""SELECT `key`,value,updated_at FROM hedge_kv_state
			   WHERE `key` IN ('BullishExecutor_Paper_state','BearishExecutor_Paper_state')"""
		):
			states[row["key"]] = _json(row.get("value"), {})
	open_positions = []
	if frappe.db.exists("DocType", "Hedge Open Position"):
		open_positions = frappe.get_all(
			"Hedge Open Position",
			filters={"status": ["in", ["Open", "Partial"]]},
			fields=[
				"executor", "symbol", "side", "qty", "remaining_qty", "entry_price",
				"mark_price", "unrealized_pnl", "realized_pnl", "updated_at",
			],
			order_by="updated_at desc",
			limit_page_length=30,
		)
	return {"sessions": sessions, "trades": trades, "states": states, "positions": open_positions}


def _straddle_summary() -> dict:
	sessions = _rows(
		"""SELECT id,date,expiry_sym,expiry_dt,state,call_sym,put_sym,call_fill,put_fill,
		          call_qty,put_qty,total_premium,long_entry,short_entry,long_qty,short_qty,
		          options_pnl,futures_pnl,net_pnl,sq_type,wallet_before,wallet_after,start_dt,end_dt
		   FROM straddle_sessions ORDER BY id DESC LIMIT 20"""
	) if _table("straddle_sessions") else []
	orders = _rows(
		"""SELECT id,session_id,symbol,asset_type,leg_label,side,order_type,qty,
		          limit_price,fill_price,status,placed_at,filled_at,cancel_reason
		   FROM straddle_orders ORDER BY id DESC LIMIT 30"""
	) if _table("straddle_orders") else []
	status = _rows(
		"SELECT ts,state,btc_mark,session_id,detail FROM straddle_bot_status WHERE id=1"
	) if _table("straddle_bot_status") else []
	wallet = _rows(
		"""SELECT id,ts,session_id,type,amount,balance_after,note
		   FROM straddle_wallet_ledger ORDER BY id DESC LIMIT 20"""
	) if _table("straddle_wallet_ledger") else []
	return {
		"sessions": sessions,
		"orders": orders,
		"status": status[0] if status else {},
		"wallet": wallet,
		"balance": wallet[0]["balance_after"] if wallet else 100000,
	}


def _audit(hedge: dict, straddle: dict) -> list[dict]:
	now = now_datetime()
	issues = []
	for session in straddle["sessions"]:
		expiry = session.get("expiry_dt")
		if session.get("state") == "ACTIVE" and expiry:
			try:
				expiry_dt = datetime.fromisoformat(str(expiry))
				if expiry_dt.tzinfo is not None and now.tzinfo is None:
					expiry_dt = expiry_dt.replace(tzinfo=None)
				if expiry_dt < now:
					issues.append(
						{"severity": "Critical", "algo": "Straddle", "type": "EXPIRED_ACTIVE_SESSION",
						 "reference": session.get("id"), "detail": str(expiry)}
					)
			except (TypeError, ValueError):
				issues.append(
					{"severity": "Warning", "algo": "Straddle", "type": "INVALID_EXPIRY",
					 "reference": session.get("id"), "detail": str(expiry)}
				)
	active_ids = {row["id"] for row in straddle["sessions"] if row.get("state") == "ACTIVE"}
	for order in straddle["orders"]:
		if order.get("status") == "NEW" and order.get("session_id") not in active_ids:
			issues.append(
				{"severity": "Critical", "algo": "Straddle", "type": "ORPHAN_OPEN_ORDER",
				 "reference": order.get("id"), "detail": f"session {order.get('session_id')}"}
			)
	for session in hedge["sessions"]:
		if session.get("status") in {"open", "running"}:
			date = session.get("session_date")
			try:
				if date and datetime.fromisoformat(str(date)).date() < (now - timedelta(days=1)).date():
					issues.append(
						{"severity": "Critical", "algo": "Hedge", "type": "STALE_RUNNING_SESSION",
						 "reference": session.get("session_id"), "detail": str(date)}
					)
			except ValueError:
				pass
	return issues


@frappe.whitelist()
def get_dashboard() -> dict:
	"""Return one consistent snapshot for the native operations dashboard."""
	_require_viewer()
	hedge = _hedge_summary()
	straddle = _straddle_summary()

	hedge_settings = {}
	if frappe.db.exists("DocType", "Hedge Trader Settings"):
		doc = frappe.get_single("Hedge Trader Settings")
		hedge_settings = {
			"engine_enabled": doc.engine_enabled,
			"global_pause": doc.global_pause,
			"paper_trading_enabled": doc.paper_trading_enabled,
			"runtime_mode": doc.runtime_mode,
		}

	straddle_settings = {}
	if frappe.db.exists("DocType", "Straddle Bot Settings"):
		doc = frappe.get_single("Straddle Bot Settings")
		straddle_settings = {
			"bot_enabled": doc.bot_enabled,
			"paper_trading_enabled": doc.paper_trading_enabled,
			"runtime_mode": doc.runtime_mode,
		}

	hedge_strategies = []
	if frappe.db.exists("DocType", "Hedge Strategy Config"):
		hedge_strategies = frappe.get_all(
			"Hedge Strategy Config",
			fields=["strategy_name", "enabled", "strategy_type", "direction"],
		)

	return {
		"generated_at": now_datetime(),
		"health": _health(),
		"hedge": hedge,
		"straddle": straddle,
		"audit_issues": _audit(hedge, straddle),
		"settings": {
			"hedge": hedge_settings,
			"straddle": straddle_settings,
			"hedge_strategies": hedge_strategies,
		},
		"permissions": {
			"can_operate": bool(set(frappe.get_roles()).intersection(OPERATOR_ROLES)),
			"can_manage": "System Manager" in frappe.get_roles() or "Trading Manager" in frappe.get_roles(),
		},
	}


@frappe.whitelist(methods=["POST"])
def issue_command(algo: str, command: str, target: str = "all", confirmed: int = 0) -> dict:
	_require_operator()
	algo = (algo or "").strip().lower()
	command = (command or "").strip().upper()
	if algo not in {"hedge", "straddle"}:
		frappe.throw("Algo must be hedge or straddle.")
	allowed = {"PAUSE", "RESUME", "FORCE_CLOSE", "SQUARE_OFF", "EMERGENCY_SQUARE_OFF"}
	if command not in allowed:
		frappe.throw("Unsupported runtime command.")
	if command in DESTRUCTIVE_COMMANDS and not cint(confirmed):
		frappe.throw("Confirmed flag is required for a square-off command.")

	if algo == "hedge":
		from hedge_trader.trading.commands import create_command
	else:
		from straddle_bot.trading.commands import create_command
	return create_command(
		command=command,
		target=target or "all",
		priority="Critical" if command in DESTRUCTIVE_COMMANDS else "High",
		confirmed=1 if command in DESTRUCTIVE_COMMANDS else cint(confirmed),
		payload={"source": "Frappe Control Center", "requested_at": str(now_datetime())},
	)


def scheduled_audit() -> None:
	"""Persist the latest self-audit in cache and raise a deduplicated error log."""
	hedge = _hedge_summary()
	straddle = _straddle_summary()
	issues = _audit(hedge, straddle)
	payload = {"checked_at": str(now_datetime()), "issues": issues}
	frappe.cache().set_value("hedge_platform:latest_audit", payload, expires_in_sec=7200)
	critical = [row for row in issues if row.get("severity") == "Critical"]
	if critical:
		signature = frappe.generate_hash(json.dumps(critical, sort_keys=True, default=str), 12)
		cache_key = f"hedge_platform:audit_alert:{signature}"
		if not frappe.cache().get_value(cache_key):
			frappe.log_error(
				title="Trading Control Center reconciliation alert",
				message=json.dumps(critical, indent=2, default=str),
			)
			frappe.cache().set_value(cache_key, 1, expires_in_sec=21600)
