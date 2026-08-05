"""Provision the Straddle runtime's MariaDB tables from a Frappe site context."""

from __future__ import annotations

import os


def provision() -> None:
	import frappe

	os.environ["MARIADB_HOST"] = str(frappe.conf.get("db_host") or "127.0.0.1")
	os.environ["MARIADB_PORT"] = str(frappe.conf.get("db_port") or 3306)
	os.environ["MARIADB_DATABASE"] = str(frappe.conf.db_name)
	os.environ["MARIADB_USER"] = str(frappe.conf.get("db_user") or frappe.conf.db_name)
	os.environ["MARIADB_PASSWORD"] = str(frappe.conf.db_password)

	from straddle_bot.mariadb_compat import ensure_schema

	ensure_schema()
