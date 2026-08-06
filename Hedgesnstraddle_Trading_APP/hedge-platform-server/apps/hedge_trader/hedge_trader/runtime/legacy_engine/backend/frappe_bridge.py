"""Optional Frappe mirror for the legacy Hedge Trader backend.

The legacy engine still writes to its SQLite state store. When it is launched
inside the Frappe worker, this bridge mirrors durable records into the Frappe
DocTypes so Desk becomes the operator-facing control plane.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Any

log = logging.getLogger("frappe_bridge")

_DISABLED_VALUES = {"0", "false", "no", "off"}

_STRATEGY_BY_TRADER = {
    "BullishExecutor_Paper": "Bullish Hedge",
    "BearishExecutor_Paper": "Bearish Hedge",
}

_SESSION_STATUS = {
    "open": "Open",
    "running": "Running",
    "sleep": "Running",
    "done": "Done",
    "closed": "Done",
    "complete": "Done",
    "completed": "Done",
    "no_trade": "No Trade",
    "no trade": "No Trade",
    "force_closed": "Force Closed",
    "force closed": "Force Closed",
    "failed": "Failed",
    "error": "Failed",
}


def _enabled() -> bool:
    return os.environ.get("HEDGE_FRAPPE_ENABLED", "1").strip().lower() not in _DISABLED_VALUES


def _frappe():
    if not _enabled():
        return None
    try:
        import frappe
    except Exception:
        return None

    try:
        if not getattr(frappe.local, "site", None):
            return None
    except Exception:
        return None
    return frappe


def _json(value: Any) -> str:
    try:
        return json.dumps(value, default=str, sort_keys=True)
    except Exception:
        return json.dumps({"value": str(value)}, sort_keys=True)


def _hash_id(prefix: str, row: dict, fields: tuple[str, ...]) -> str:
    material = {field: row.get(field) for field in fields}
    digest = hashlib.sha1(_json(material).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dt(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(" IST"):
        text = text[:-4]
    return text.replace("T", " ")[:19]


def _side(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if text in {"BUY", "SELL"}:
        return text
    if "BUY" in text or "LONG" in text:
        return "BUY"
    if "SELL" in text or "SHORT" in text:
        return "SELL"
    return None


def _instrument_type(symbol: Any, action: Any = "") -> str:
    text = f"{symbol or ''} {action or ''}".upper()
    if "CALL" in text or "PUT" in text or "-C" in text or "-P" in text or "HEDGE" in text:
        return "Option"
    if "USDT" in text or "FUTURES" in text:
        return "Futures"
    return "Spot"


def _order_status(value: Any) -> str:
    text = str(value or "").strip().upper()
    if "CANCEL" in text:
        return "Cancelled"
    if "REJECT" in text:
        return "Rejected"
    if "FAIL" in text or "ERROR" in text:
        return "Failed"
    if "ACCEPT" in text:
        return "Accepted"
    if "REQUEST" in text:
        return "Requested"
    return "Filled"


def _session_status(value: Any) -> str:
    text = str(value or "").strip()
    return _SESSION_STATUS.get(text.lower(), "Running" if text else "Open")


class FrappeBridge:
    def __init__(self):
        self._warned_unavailable = False

    def _ready(self):
        frappe = _frappe()
        if frappe is None and not self._warned_unavailable and _enabled():
            log.debug("Frappe bridge is inactive because no Frappe site is connected.")
            self._warned_unavailable = True
        return frappe

    def _strategy(self, frappe, trader_name: str | None) -> str | None:
        if not trader_name:
            return None
        name = _STRATEGY_BY_TRADER.get(trader_name)
        if name and frappe.db.exists("Hedge Strategy Config", name):
            return name
        if frappe.db.exists("Hedge Strategy Config", {"executor_name": trader_name}):
            return frappe.db.get_value("Hedge Strategy Config", {"executor_name": trader_name}, "name")
        return None

    def _session(self, frappe, session_id: str | None) -> str | None:
        if session_id and frappe.db.exists("Hedge Trading Session", session_id):
            return session_id
        return None

    async def mirror_session(self, row: dict):
        frappe = self._ready()
        if frappe is None:
            return False
        try:
            from hedge_trader.trading.ingest import upsert_session

            trader_name = row.get("trader_name")
            payload = {
                "session_id": row.get("session_id"),
                "strategy": self._strategy(frappe, trader_name),
                "trader_name": trader_name,
                "session_date": row.get("session_date"),
                "display_name": row.get("display_name") or row.get("session_id"),
                "status": _session_status(row.get("status")),
                "mode": "Paper",
                "runtime_state": row.get("status"),
                "window_open_ts": _dt(row.get("window_open_ts")),
                "entry_ts_ist": _dt(row.get("entry_ts_ist")),
                "close_ts_ist": _dt(row.get("close_ts_ist")),
                "close_reason": row.get("close_reason"),
                "entry_price": _float(row.get("entry_price")),
                "target_line": _float(row.get("target_line")),
                "futures_pnl": _float(row.get("futures_pnl")),
                "hedge_pnl": _float(row.get("hedge_pnl")),
                "total_pnl": _float(row.get("total_pnl")),
                "balance_before": _float(row.get("balance_before")),
                "balance_after": _float(row.get("balance_after")),
                "snapshot_json": _json({"source": "legacy_trading_sessions", "row": row}),
            }
            upsert_session({k: v for k, v in payload.items() if v is not None})
            return True
        except Exception as exc:
            log.warning("Frappe session mirror failed: %s", exc)
            return False

    async def mirror_paper_trade(self, row: dict):
        return await self._mirror_order_and_ledger(
            row=row,
            external_prefix="legacy-paper",
            fields=("trader_name", "session_id", "ts_utc", "action", "symbol", "side", "qty", "fill_price", "pnl"),
            executor=row.get("trader_name"),
            symbol=row.get("symbol"),
            price=row.get("fill_price"),
            fill_price=row.get("fill_price"),
            requested_at=row.get("ts_ist") or row.get("ts_utc"),
            filled_at=row.get("ts_ist") or row.get("ts_utc"),
            status="Filled",
            detail_source="legacy_paper_trades",
        )

    async def mirror_trade_log(self, row: dict):
        return await self._mirror_order_and_ledger(
            row=row,
            external_prefix="legacy-trade",
            fields=("executor", "ts_utc", "action", "instrument", "side", "qty", "price", "pnl", "status"),
            executor=row.get("executor"),
            symbol=row.get("instrument"),
            price=row.get("price"),
            fill_price=row.get("price"),
            requested_at=row.get("ts_ist") or row.get("ts_utc"),
            filled_at=row.get("ts_ist") or row.get("ts_utc"),
            status=_order_status(row.get("status")),
            detail_source="legacy_trade_log",
        )

    async def _mirror_order_and_ledger(
        self,
        *,
        row: dict,
        external_prefix: str,
        fields: tuple[str, ...],
        executor: str | None,
        symbol: str | None,
        price: Any,
        fill_price: Any,
        requested_at: Any,
        filled_at: Any,
        status: str,
        detail_source: str,
    ):
        frappe = self._ready()
        if frappe is None:
            return False
        try:
            from hedge_trader.trading.ingest import record_ledger_entry, record_order

            external_order_id = _hash_id(external_prefix, row, fields)
            existing = frappe.db.get_value("Hedge Trade Order", {"external_order_id": external_order_id}, "name")

            side = _side(row.get("side"))
            session_id = row.get("session_id")
            strategy = self._strategy(frappe, executor)
            qty = _float(row.get("qty"))
            fill = _float(fill_price)
            amount = (qty or 0) * (fill or 0)
            order_payload = {
                    "external_order_id": external_order_id,
                    "session": self._session(frappe, session_id),
                    "strategy": strategy,
                    "executor": executor,
                    "mode": "Paper" if row.get("is_paper", True) else "Live",
                    "status": status,
                    "action": row.get("action"),
                    "instrument_type": _instrument_type(symbol, row.get("action")),
                    "symbol": symbol,
                    "side": side,
                    "order_type": "Paper Fill" if row.get("is_paper", True) else "Market",
                    "qty": qty,
                    "price": _float(price),
                    "fill_price": fill,
                    "pnl": _float(row.get("pnl")),
                    "requested_at": _dt(requested_at),
                    "filled_at": _dt(filled_at),
                    "exchange_ts": str(row.get("ts_utc") or ""),
                    "detail": {"source": detail_source, "row": row},
                    "notes": row.get("notes"),
                }
            order_doc = {"name": existing} if existing else record_order(order_payload)
            order_name = order_doc.get("name")
            ledger_exists = bool(
                order_name and frappe.db.exists("Hedge Paper Ledger Entry", {"trade_order": order_name})
            )
            if not ledger_exists:
                record_ledger_entry(
                    {
                    "session": self._session(frappe, session_id),
                    "trade_order": order_name,
                    "executor": executor,
                    "account": "Paper" if row.get("is_paper", True) else "Live",
                    "posting_time": _dt(filled_at) or _dt(requested_at),
                    "entry_type": "Trade",
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "fill_price": fill,
                    "debit": amount if side == "BUY" else None,
                    "credit": amount if side == "SELL" else None,
                    "pnl": _float(row.get("pnl")),
                    "notes": row.get("notes") or row.get("status"),
                    "detail": {"source": detail_source, "row": row},
                    }
                )
            return True
        except Exception as exc:
            log.warning("Frappe order mirror failed: %s", exc)
            return False

    async def mirror_session_event(self, row: dict):
        frappe = self._ready()
        if frappe is None:
            return False
        try:
            from hedge_trader.trading.ingest import record_session_event

            session_id = row.get("session_id")
            external_event_id = _hash_id(
                "legacy-event",
                row,
                ("trader_name", "session_id", "session_date", "event_ts_ist",
                 "event_type", "state", "price", "locked_line", "message"),
            )
            record_session_event(
                {
                    "external_event_id": external_event_id,
                    "session": self._session(frappe, session_id),
                    "trader_name": row.get("trader_name"),
                    "session_date": row.get("session_date"),
                    "event_type": row.get("event_type"),
                    "event_ts": _dt(row.get("event_ts_ist")),
                    "state": row.get("state"),
                    "price": _float(row.get("price")),
                    "locked_line": _float(row.get("locked_line")),
                    "severity": "Info",
                    "message": row.get("message"),
                    "detail": {"source": "legacy_session_event_log", "row": row},
                }
            )
            return True
        except Exception as exc:
            log.warning("Frappe session event mirror failed: %s", exc)
            return False

bridge = FrappeBridge()
