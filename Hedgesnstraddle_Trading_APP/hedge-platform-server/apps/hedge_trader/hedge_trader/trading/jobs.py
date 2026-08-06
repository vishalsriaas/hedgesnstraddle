import frappe


def minute_heartbeat():
	"""Cheap scheduler heartbeat for health visibility.

	The real market-data heartbeat should come from the external feed worker.
	"""
	frappe.cache().set_value("hedge_trader:frappe_scheduler_alive", 1, expires_in_sec=120)
	try:
		from hedge_trader.trading.commands import heartbeat

		heartbeat("frappe_scheduler", status="OK", summary="Scheduler heartbeat")
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Hedge Trader scheduler heartbeat failed")


def daily_housekeeping():
	"""Placeholder for daily reports, old-log cleanup, and reconciliation jobs."""
	frappe.logger("hedge_trader").info("Daily housekeeping placeholder executed")
