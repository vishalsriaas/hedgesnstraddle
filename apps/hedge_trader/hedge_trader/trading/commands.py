from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

COMMAND_DOCTYPE = "Hedge Runtime Command"
STATUS_DOCTYPE = "Hedge Runtime Status"


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
	"""Create an operator command for the trading worker to claim."""
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
		}
	)
	doc.insert()
	frappe.db.commit()
	return doc.as_dict()


@frappe.whitelist()
def get_pending_commands(target: str | None = None, limit: int = 10) -> list[dict]:
	"""Return pending commands without claiming them."""
	filters: dict[str, Any] = {"status": "Pending"}
	if target:
		filters["target"] = ["in", ["all", target]]

	rows = frappe.get_all(
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
	return rows


@frappe.whitelist()
def claim_pending_commands(worker_id: str, target: str | None = None, limit: int = 10) -> list[dict]:
	"""Atomically claim pending commands for a worker.

	The worker should execute only commands returned by this method. Targets are
	either "all" or the caller-provided target, usually an executor name.
	"""
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
	"""Mark a command as Completed or Failed after the worker handles it."""
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
def heartbeat(
	component: str,
	status: str = "OK",
	worker_id: str | None = None,
	mode: str | None = None,
	summary: str | None = None,
	snapshot: Any = None,
) -> dict:
	"""Update the latest runtime status for a feed, worker, or executor."""
	name = frappe.db.get_value(STATUS_DOCTYPE, {"component": component})
	if name:
		doc = frappe.get_doc(STATUS_DOCTYPE, name)
	else:
		doc = frappe.get_doc({"doctype": STATUS_DOCTYPE, "component": component})

	doc.status = status or "Unknown"
	doc.worker_id = worker_id
	doc.mode = mode
	doc.summary = summary
	doc.snapshot_json = _json(snapshot)
	doc.last_heartbeat = now_datetime()

	if doc.is_new():
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)

	frappe.cache().set_value(f"hedge_trader:heartbeat:{component}", doc.as_dict(), expires_in_sec=300)
	frappe.publish_realtime("hedge_trader_runtime_status", doc.as_dict(), after_commit=True)
	frappe.db.commit()
	return doc.as_dict()


@frappe.whitelist()
def cancel_command(command_name: str, message: str = "Cancelled by operator") -> dict:
	return complete_command(command_name, "Cancelled", message)
