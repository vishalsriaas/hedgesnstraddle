"""Restart-safe mirror from the legacy Straddle SQLite database into Frappe."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import frappe

from straddle_bot.trading import ingest

log = logging.getLogger("straddle_bot.sqlite_sync")

APPEND_TABLES = ("pnl_snapshots", "events", "wallet_ledger", "fills")


def _session_id(legacy_id: Any) -> str | None:
	if legacy_id in (None, "", 0, "0"):
		return None
	return f"STR-SESSION-{int(legacy_id)}"


def _session_name(legacy_id: Any) -> str | None:
	session_id = _session_id(legacy_id)
	if not session_id:
		return None
	return frappe.db.get_value("Straddle Trading Session", {"session_id": session_id}, "name")


def _rows(db: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
	return [dict(row) for row in db.execute(sql, params).fetchall()]


def _ensure_cursor_table(db: sqlite3.Connection) -> None:
	db.execute(
		"CREATE TABLE IF NOT EXISTS frappe_sync_cursor ("
		"table_name TEXT PRIMARY KEY, last_id INTEGER NOT NULL DEFAULT 0)"
	)
	db.commit()


def _cursor(db: sqlite3.Connection, table: str) -> int:
	row = db.execute(
		"SELECT last_id FROM frappe_sync_cursor WHERE table_name=?", (table,)
	).fetchone()
	return int(row[0] or 0) if row else 0


def _advance(db: sqlite3.Connection, table: str, row_id: int) -> None:
	db.execute(
		"INSERT INTO frappe_sync_cursor(table_name,last_id) VALUES (?,?) "
		"ON CONFLICT(table_name) DO UPDATE SET last_id=excluded.last_id",
		(table, int(row_id)),
	)
	db.commit()


def _sync_sessions(db: sqlite3.Connection) -> int:
	count = 0
	for row in _rows(db, "SELECT * FROM sessions ORDER BY id"):
		payload = {
			"session_id": _session_id(row["id"]),
			"legacy_id": row["id"],
			"session_date": row.get("date"),
			"expiry_symbol": row.get("expiry_sym"),
			"expiry_datetime": row.get("expiry_dt"),
			"state": row.get("state"),
			"start_datetime": row.get("start_dt"),
			"end_datetime": row.get("end_dt"),
			"entry_ts_ms": row.get("entry_ts_ms"),
			"call_symbol": row.get("call_sym"),
			"put_symbol": row.get("put_sym"),
			"call_fill": row.get("call_fill"),
			"put_fill": row.get("put_fill"),
			"call_qty": row.get("call_qty"),
			"put_qty": row.get("put_qty"),
			"total_premium": row.get("total_premium"),
			"long_entry": row.get("long_entry"),
			"short_entry": row.get("short_entry"),
			"long_qty": row.get("long_qty"),
			"short_qty": row.get("short_qty"),
			"long_tp_price": row.get("long_tp_px"),
			"short_tp_price": row.get("short_tp_px"),
			"long_liq_price": row.get("long_liq_px"),
			"short_liq_price": row.get("short_liq_px"),
			"margin_used": row.get("margin_used"),
			"entry_btc": row.get("entry_btc"),
			"tp_distance": row.get("tp_dist"),
			"wallet_before": row.get("wallet_before"),
			"sq_call_exit": row.get("sq_call_exit"),
			"sq_put_exit": row.get("sq_put_exit"),
			"sq_long_exit": row.get("sq_long_exit"),
			"sq_short_exit": row.get("sq_short_exit"),
			"options_pnl": row.get("options_pnl"),
			"futures_pnl": row.get("futures_pnl"),
			"net_pnl": row.get("net_pnl"),
			"squareoff_type": row.get("sq_type"),
			"wallet_after": row.get("wallet_after"),
		}
		ingest.upsert_session(payload)
		count += 1
	return count


def _sync_orders(db: sqlite3.Connection) -> int:
	count = 0
	for row in _rows(db, "SELECT * FROM orders ORDER BY id"):
		ingest.record_order(
			{
				"legacy_id": row["id"],
				"session": _session_name(row.get("session_id")),
				"session_legacy_id": row.get("session_id"),
				"paper_order_id": row.get("paper_order_id"),
				"symbol": row.get("symbol"),
				"asset_type": row.get("asset_type"),
				"leg_label": row.get("leg_label"),
				"side": row.get("side"),
				"order_type": row.get("order_type"),
				"status": row.get("status"),
				"qty": row.get("qty"),
				"limit_price": row.get("limit_price"),
				"fill_price": row.get("fill_price"),
				"placed_at": row.get("placed_at"),
				"filled_at": row.get("filled_at"),
				"updated_at": row.get("updated_at"),
				"cancel_reason": row.get("cancel_reason"),
			}
		)
		count += 1
	return count


def _append_payload(table: str, row: dict) -> tuple[str, dict]:
	common = {
		"legacy_id": row["id"],
		"session": _session_name(row.get("session_id")),
		"session_legacy_id": row.get("session_id"),
	}
	if table == "pnl_snapshots":
		return "record_pnl_snapshot", {
			**common,
			"snapshot_time": row.get("ts"),
			**{key: row.get(key) for key in (
				"btc_mark", "call_mark", "put_mark", "call_upnl", "put_upnl",
				"long_upnl", "short_upnl", "total_upnl", "long_liq",
				"short_liq", "margin_used",
			)},
		}
	if table == "events":
		return "record_session_event", {
			**common, "event_time": row.get("ts"),
			"event_type": row.get("event_type"), "detail": row.get("detail"),
		}
	if table == "wallet_ledger":
		return "record_wallet_ledger_entry", {
			**common, "posting_time": row.get("ts"), "entry_type": row.get("type"),
			"amount": row.get("amount"), "balance_after": row.get("balance_after"),
			"note": row.get("note"),
		}
	return "record_fill", {
		**common, "fill_time": row.get("ts"), "instrument": row.get("instrument"),
		"side": row.get("side"), "qty": row.get("qty"),
		"ask_at_order": row.get("ask_at_order"), "fill_price": row.get("fill_price"),
		"slippage": row.get("slippage"), "order_id": row.get("order_id"),
		"note": row.get("note"),
	}


def _sync_append_table(db: sqlite3.Connection, table: str) -> int:
	count = 0
	last_id = _cursor(db, table)
	for row in _rows(db, f"SELECT * FROM {table} WHERE id>? ORDER BY id", (last_id,)):
		method_name, payload = _append_payload(table, row)
		getattr(ingest, method_name)(payload)
		_advance(db, table, row["id"])
		count += 1
	return count


def sync_once(db_path: str | Path) -> dict[str, int]:
	path = Path(db_path)
	if not path.exists():
		return {"sessions": 0, "orders": 0, **{table: 0 for table in APPEND_TABLES}}
	db = sqlite3.connect(str(path), timeout=10)
	db.row_factory = sqlite3.Row
	try:
		_ensure_cursor_table(db)
		result = {"sessions": _sync_sessions(db), "orders": _sync_orders(db)}
		for table in APPEND_TABLES:
			result[table] = _sync_append_table(db, table)
		return result
	finally:
		db.close()


def _sync_in_site(db_path: str, site: str, sites_path: str) -> dict[str, int]:
	frappe.init(site=site, sites_path=sites_path)
	frappe.connect()
	try:
		return sync_once(db_path)
	finally:
		frappe.destroy()


async def sync_loop(interval_seconds: float = 5.0) -> None:
	db_path = os.environ.get("DB_PATH")
	if not db_path:
		raise RuntimeError("DB_PATH is required for Straddle SQLite sync")
	site = getattr(frappe.local, "site", None)
	sites_path = str(getattr(frappe.local, "sites_path", None) or (Path.cwd() / "sites"))
	if not site:
		raise RuntimeError("Connected Frappe site is required for Straddle SQLite sync")
	while True:
		try:
			result = await asyncio.to_thread(_sync_in_site, db_path, site, sites_path)
			frappe.cache().set_value(
				"straddle_bot:sqlite_sync",
				{"status": "OK", "db_path": db_path, "last_batch": result},
				expires_in_sec=300,
			)
		except asyncio.CancelledError:
			raise
		except Exception as exc:
			frappe.db.rollback()
			log.exception("Straddle SQLite sync failed; backlog will retry: %s", exc)
			frappe.cache().set_value(
				"straddle_bot:sqlite_sync",
				{"status": "Failed", "db_path": db_path, "error": str(exc)},
				expires_in_sec=300,
			)
		await asyncio.sleep(max(1.0, interval_seconds))
