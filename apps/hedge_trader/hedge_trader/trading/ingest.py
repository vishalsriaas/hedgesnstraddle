from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import now_datetime


def _json(value: Any) -> str | None:
	if value in (None, ""):
		return None
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError:
			return value
	return json.dumps(value, default=str, sort_keys=True)


def _payload(data: Any | None = None, **kwargs) -> dict:
	if data is None:
		data = {}
	elif isinstance(data, str):
		data = json.loads(data)
	elif not isinstance(data, dict):
		data = dict(data)
	data.update({key: value for key, value in kwargs.items() if value is not None})
	return data


def _upsert(doctype: str, key_field: str, data: dict) -> dict:
	key_value = data.get(key_field)
	if not key_value:
		frappe.throw(f"{key_field} is required for {doctype}.")

	name = frappe.db.get_value(doctype, {key_field: key_value})
	if name:
		doc = frappe.get_doc(doctype, name)
		doc.update(data)
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc({"doctype": doctype, **data})
		doc.insert(ignore_permissions=True)

	frappe.publish_realtime("hedge_trader_record_update", {"doctype": doctype, "name": doc.name}, after_commit=True)
	frappe.db.commit()
	return doc.as_dict()


def upsert_session(data: Any = None, **kwargs) -> dict:
	payload = _payload(data, **kwargs)
	return _upsert("Hedge Trading Session", "session_id", payload)


def record_order(data: Any = None, **kwargs) -> dict:
	payload = _payload(data, **kwargs)
	if "detail" in payload and "detail_json" not in payload:
		payload["detail_json"] = _json(payload.pop("detail"))
	if not payload.get("requested_at"):
		payload["requested_at"] = now_datetime()
	doc = frappe.get_doc({"doctype": "Hedge Trade Order", **payload})

	existing = frappe.db.get_value(
		"Hedge Trade Order",
		{
			"executor": doc.executor,
			"symbol": doc.symbol,
			"side": doc.side,
			"qty": doc.qty,
			"price": doc.price,
		},
		"name",
	)
	if existing:
		doc.name = existing
		return doc.as_dict()

	doc.insert(ignore_permissions=True)
	frappe.publish_realtime("hedge_trader_order", doc.as_dict(), after_commit=True)
	frappe.db.commit()
	return doc.as_dict()


@frappe.whitelist()
def upsert_position(data: Any = None, **kwargs) -> dict:
	payload = _payload(data, **kwargs)
	if "snapshot" in payload and "snapshot_json" not in payload:
		payload["snapshot_json"] = _json(payload.pop("snapshot"))
	payload["updated_at"] = payload.get("updated_at") or now_datetime()
	return _upsert("Hedge Open Position", "position_key", payload)


@frappe.whitelist()
def close_position(position_key: str, realized_pnl: float | None = None, snapshot: Any = None) -> dict:
	name = frappe.db.get_value("Hedge Open Position", {"position_key": position_key})
	if not name:
		frappe.throw(f"Open position not found: {position_key}")
	doc = frappe.get_doc("Hedge Open Position", name)
	doc.status = "Closed"
	doc.remaining_qty = 0
	doc.realized_pnl = realized_pnl if realized_pnl is not None else doc.realized_pnl
	doc.updated_at = now_datetime()
	if snapshot is not None:
		doc.snapshot_json = _json(snapshot)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return doc.as_dict()


def record_ledger_entry(data: Any = None, **kwargs) -> dict:
	payload = _payload(data, **kwargs)
	if "detail" in payload and "detail_json" not in payload:
		payload["detail_json"] = _json(payload.pop("detail"))
	payload["posting_time"] = payload.get("posting_time") or now_datetime()

	executor = payload.get("executor")
	symbol = payload.get("symbol")
	side = payload.get("side")
	qty = payload.get("qty")
	fill_price = payload.get("fill_price")
	session = payload.get("session")

	filter_args = {}
	if executor: filter_args["executor"] = executor
	if symbol: filter_args["symbol"] = symbol
	if side: filter_args["side"] = side
	if qty: filter_args["qty"] = qty
	if fill_price: filter_args["fill_price"] = fill_price
	if session: filter_args["session"] = session

	if filter_args and frappe.db.exists("Hedge Paper Ledger Entry", filter_args):
		existing_name = frappe.db.get_value("Hedge Paper Ledger Entry", filter_args)
		return frappe.get_doc("Hedge Paper Ledger Entry", existing_name).as_dict()

	doc = frappe.get_doc({"doctype": "Hedge Paper Ledger Entry", **payload})
	doc.insert(ignore_permissions=True)
	frappe.publish_realtime("hedge_trader_ledger", doc.as_dict(), after_commit=True)
	frappe.db.commit()
	return doc.as_dict()


def record_session_event(data: Any = None, **kwargs) -> dict:
	payload = _payload(data, **kwargs)
	external_id = payload.get("external_event_id")
	if external_id:
		name = frappe.db.get_value("Hedge Session Event", {"external_event_id": external_id})
		if name:
			return frappe.get_doc("Hedge Session Event", name).as_dict()
	if "detail" in payload and "detail_json" not in payload:
		payload["detail_json"] = _json(payload.pop("detail"))
	payload["event_ts"] = payload.get("event_ts") or now_datetime()
	doc = frappe.get_doc({"doctype": "Hedge Session Event", **payload})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.as_dict()


@frappe.whitelist()
def record_health_snapshot(data: Any = None, **kwargs) -> dict:
	payload = _payload(data, **kwargs)
	if "snapshot" in payload and "snapshot_json" not in payload:
		payload["snapshot_json"] = _json(payload.pop("snapshot"))
	payload["snapshot_time"] = payload.get("snapshot_time") or now_datetime()
	doc = frappe.get_doc({"doctype": "Hedge System Health Snapshot", **payload})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.as_dict()
