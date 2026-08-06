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

	frappe.publish_realtime("straddle_bot_record_update", {"doctype": doctype, "name": doc.name}, after_commit=True)
	frappe.db.commit()
	return doc.as_dict()


@frappe.whitelist()
def upsert_config_item(data: Any = None, **kwargs) -> dict:
	payload = _payload(data, **kwargs)
	return _upsert("Straddle Config Item", "config_key", payload)


def upsert_session(data: Any = None, **kwargs) -> dict:
	payload = _payload(data, **kwargs)
	if payload.get("legacy_id") and not payload.get("session_id"):
		payload["session_id"] = f"STR-SESSION-{payload['legacy_id']}"
	return _upsert("Straddle Trading Session", "session_id", payload)


def record_order(data: Any = None, **kwargs) -> dict:
	payload = _payload(data, **kwargs)
	name = (frappe.db.get_value("Straddle Trade Order", {"legacy_id": payload.get("legacy_id")})
		if payload.get("legacy_id") is not None else None)
	if name:
		doc = frappe.get_doc("Straddle Trade Order", name)
		doc.update(payload)
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		return doc.as_dict()
	doc = frappe.get_doc({"doctype": "Straddle Trade Order", **payload})
	doc.insert(ignore_permissions=True)
	frappe.publish_realtime("straddle_bot_order", doc.as_dict(), after_commit=True)
	frappe.db.commit()
	return doc.as_dict()


def record_pnl_snapshot(data: Any = None, **kwargs) -> dict:
	payload = _payload(data, **kwargs)
	name = (frappe.db.get_value("Straddle PnL Snapshot", {"legacy_id": payload.get("legacy_id")})
		if payload.get("legacy_id") is not None else None)
	if name:
		return frappe.get_doc("Straddle PnL Snapshot", name).as_dict()
	payload["snapshot_time"] = payload.get("snapshot_time") or payload.get("ts") or now_datetime()
	doc = frappe.get_doc({"doctype": "Straddle PnL Snapshot", **payload})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.as_dict()


def record_session_event(data: Any = None, **kwargs) -> dict:
	payload = _payload(data, **kwargs)
	name = (frappe.db.get_value("Straddle Session Event", {"legacy_id": payload.get("legacy_id")})
		if payload.get("legacy_id") is not None else None)
	if name:
		return frappe.get_doc("Straddle Session Event", name).as_dict()
	payload["event_time"] = payload.get("event_time") or payload.get("ts") or now_datetime()
	doc = frappe.get_doc({"doctype": "Straddle Session Event", **payload})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.as_dict()


def record_wallet_ledger_entry(data: Any = None, **kwargs) -> dict:
	payload = _payload(data, **kwargs)
	name = (frappe.db.get_value("Straddle Wallet Ledger Entry", {"legacy_id": payload.get("legacy_id")})
		if payload.get("legacy_id") is not None else None)
	if name:
		return frappe.get_doc("Straddle Wallet Ledger Entry", name).as_dict()
	payload["posting_time"] = payload.get("posting_time") or payload.get("ts") or now_datetime()
	doc = frappe.get_doc({"doctype": "Straddle Wallet Ledger Entry", **payload})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.as_dict()


def record_fill(data: Any = None, **kwargs) -> dict:
	payload = _payload(data, **kwargs)
	name = (frappe.db.get_value("Straddle Fill", {"legacy_id": payload.get("legacy_id")})
		if payload.get("legacy_id") is not None else None)
	if name:
		return frappe.get_doc("Straddle Fill", name).as_dict()
	payload["fill_time"] = payload.get("fill_time") or payload.get("ts") or now_datetime()
	doc = frappe.get_doc({"doctype": "Straddle Fill", **payload})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.as_dict()


@frappe.whitelist()
def heartbeat(
	component: str = "straddle_bot",
	status: str = "OK",
	worker_id: str | None = None,
	mode: str | None = None,
	state: str | None = None,
	btc_mark: float | None = None,
	session: str | None = None,
	summary: str | None = None,
	detail: Any = None,
) -> dict:
	payload = {
		"component": component,
		"status": status or "Unknown",
		"worker_id": worker_id,
		"mode": mode,
		"state": state,
		"btc_mark": btc_mark,
		"session": session,
		"summary": summary,
		"detail_json": _json(detail),
		"last_heartbeat": now_datetime(),
	}
	return _upsert("Straddle Runtime Status", "component", payload)
