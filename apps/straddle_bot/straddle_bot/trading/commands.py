from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

COMMAND_DOCTYPE = "Straddle Runtime Command"


def _json(value: Any) -> str | None:
	if value in (None, ""):
		return None
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError:
			return value
	return json.dumps(value, default=str, sort_keys=True)


@frappe.whitelist()
def create_command(
	command: str,
	target: str = "all",
	payload: Any = None,
	priority: str = "Normal",
	confirmed: int = 0,
) -> dict:
	roles = set(frappe.get_roles())
	if not roles.intersection({"System Manager", "Trading Operator", "Trading Manager"}):
		frappe.throw("Trading Operator or System Manager role is required.", frappe.PermissionError)
	if command.upper() in {"FORCE_CLOSE", "SQUARE_OFF", "EMERGENCY_SQUARE_OFF"} and not cint(confirmed):
		frappe.throw("Confirmed flag is required for a square-off command.")
	doc = frappe.get_doc(
		{
			"doctype": COMMAND_DOCTYPE,
			"command": command,
			"target": target or "all",
			"priority": priority or "Normal",
			"confirmed": cint(confirmed),
			"payload_json": _json(payload),
			"status": "Pending",
			"requested_by": frappe.session.user,
			"requested_at": now_datetime(),
		}
	)
	doc.insert()
	frappe.db.commit()
	return doc.as_dict()


@frappe.whitelist()
def get_pending_commands(target: str | None = None, limit: int = 10) -> list[dict]:
	filters: dict[str, Any] = {"status": "Pending"}
	if target:
		filters["target"] = ["in", ["all", target]]

	return frappe.get_all(
		COMMAND_DOCTYPE,
		fields=[
			"name",
			"command",
			"target",
			"priority",
			"requested_by",
			"requested_at",
			"payload_json",
			"confirmed",
		],
		filters=filters,
		order_by="creation asc",
		limit_page_length=cint(limit) or 10,
	)


@frappe.whitelist()
def claim_pending_commands(worker_id: str, target: str | None = None, limit: int = 10) -> list[dict]:
	filters: dict[str, Any] = {"status": "Pending"}
	if target:
		filters["target"] = ["in", ["all", target]]

	rows = frappe.get_all(
		COMMAND_DOCTYPE,
		fields=["name"],
		filters=filters,
		order_by="creation asc",
		limit_page_length=cint(limit) or 10,
	)

	claimed: list[dict] = []
	for row in rows:
		doc = frappe.get_doc(COMMAND_DOCTYPE, row.name)
		if doc.status != "Pending":
			continue
		doc.status = "Claimed"
		doc.worker_id = worker_id
		doc.claimed_at = now_datetime()
		doc.save(ignore_permissions=True)
		claimed.append(doc.as_dict())

	if claimed:
		frappe.db.commit()
	return claimed


@frappe.whitelist()
def complete_command(command_name: str, status: str = "Completed", message: str = "", result: Any = None) -> dict:
	if status not in {"Completed", "Failed", "Cancelled"}:
		frappe.throw("Status must be Completed, Failed, or Cancelled.")

	doc = frappe.get_doc(COMMAND_DOCTYPE, command_name)
	doc.status = status
	doc.message = message
	doc.result_json = _json(result)
	doc.completed_at = now_datetime()
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return doc.as_dict()


@frappe.whitelist()
def heartbeat(*args, **kwargs) -> dict:
	from straddle_bot.trading.ingest import heartbeat as update_heartbeat

	return update_heartbeat(*args, **kwargs)


@frappe.whitelist()
def cancel_command(command_name: str, message: str = "Cancelled by operator") -> dict:
	return complete_command(command_name, "Cancelled", message)
