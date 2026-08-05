"""Dedicated runtime worker entrypoint.

This module is intentionally separate from Frappe request workers and scheduler
jobs. It can be supervised by bench, systemd, or any process manager.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

log = logging.getLogger("hedge_trader.worker")


def _connect_frappe(site: str | None):
	if not site:
		return None

	import frappe

	sites_path = Path.cwd() / "sites"
	if sites_path.exists():
		# Frappe's logger resolves the site log path relative to the process
		# working directory, even when init receives an absolute sites_path.
		# Run from the sites directory so supervised workers use the same
		# layout as bench commands.
		os.chdir(sites_path)
		frappe.init(site=site, sites_path=".")
	else:
		frappe.init(site=site)
	frappe.connect()
	return frappe


def _disconnect_frappe(frappe_module):
	if frappe_module is not None:
		frappe_module.destroy()


def _default_embedded_engine_path() -> Path:
	return Path(__file__).resolve().parents[1] / "runtime" / "legacy_engine"


def _configure_site_runtime_defaults(frappe_module) -> None:
	if frappe_module is None:
		raise RuntimeError("--site is required: MariaDB is the only supported runtime database")

	runtime_dir = Path(frappe_module.get_site_path("private", "files", "hedge_trader"))
	log_dir = runtime_dir / "logs"
	backup_dir = runtime_dir / "backups"
	for folder in (runtime_dir, log_dir, backup_dir):
		folder.mkdir(parents=True, exist_ok=True)

	os.environ.setdefault("LOG_DIR", str(log_dir))
	os.environ.setdefault("BACKUP_DIR", str(backup_dir))
	os.environ.setdefault("HEDGE_FRAPPE_ENABLED", "1")
	os.environ.setdefault("MARIADB_HOST", str(frappe_module.conf.get("db_host") or "127.0.0.1"))
	os.environ.setdefault("MARIADB_PORT", str(frappe_module.conf.get("db_port") or 3306))
	os.environ.setdefault("MARIADB_DATABASE", str(frappe_module.conf.db_name))
	os.environ.setdefault("MARIADB_USER", str(frappe_module.conf.get("db_user") or frappe_module.conf.db_name))
	os.environ.setdefault("MARIADB_PASSWORD", str(frappe_module.conf.db_password))
	from hedge_trader.mariadb_compat import ensure_schema
	ensure_schema()

	try:
		settings = frappe_module.get_single("Hedge Trader Settings")
	except Exception as exc:
		log.warning("Could not load Hedge Trader Settings: %s", exc)
		return {"engine_enabled": True, "global_pause": False}

	if settings.get("redis_url"):
		os.environ.setdefault("REDIS_URL", settings.redis_url)

	return {
		"engine_enabled": bool(settings.get("engine_enabled", True)),
		"global_pause": bool(settings.get("global_pause", False)),
	}


async def _monitor_commands(worker_id: str, target: str | None, poll_seconds: int):
	import json
	import urllib.request
	from hedge_trader.trading.commands import claim_pending_commands, complete_command, heartbeat

	while True:
		pending = claim_pending_commands(worker_id=worker_id, target=target, limit=20)
		for row in pending:
			command = str(row.get("command") or "").upper()
			try:
				if command not in {"FORCE_CLOSE", "SQUARE_OFF", "EMERGENCY_SQUARE_OFF"}:
					raise RuntimeError(f"Command {command} is not implemented by the Hedge runtime")
				targets = ["bull", "bear"] if row.get("target") in (None, "", "all") else [row["target"]]
				results = []
				for trader in targets:
					url = (
						f"http://127.0.0.1:{os.environ.get('HEDGE_RUNTIME_PORT', '8100')}"
						f"/api/trader/{trader}/force-close"
					)
					def _post():
						request = urllib.request.Request(url, data=b"{}", method="POST",
							headers={"Content-Type": "application/json"})
						with urllib.request.urlopen(request, timeout=30) as response:
							return json.loads(response.read())
					results.append(await asyncio.to_thread(_post))
				complete_command(row["name"], "Completed", "Runtime acknowledged square-off", results)
			except Exception as exc:
				complete_command(row["name"], "Failed", str(exc))
		heartbeat(
			component="frappe_command_worker",
			status="OK",
			worker_id=worker_id,
			summary=f"{len(pending)} command(s) processed",
			snapshot={"target": target or "all", "pending": pending},
		)
		if pending:
			log.info("Pending command(s): %s", ", ".join(row["name"] for row in pending))
		await asyncio.sleep(max(1, poll_seconds))


def _prepare_legacy_import_path(engine_path: str) -> Path:
	resolved = Path(engine_path).expanduser().resolve()
	if not (resolved / "backend").exists():
		raise SystemExit(f"Legacy engine path must contain a backend folder: {resolved}")
	sys.path.insert(0, str(resolved))
	return resolved


async def _run_legacy_engine(engine_path: str, worker_id: str):
	"""Launch the legacy backend inside this worker process."""
	resolved = _prepare_legacy_import_path(engine_path)

	from backend.main import shutdown, startup
	from hedge_trader.trading.commands import heartbeat

	await startup()
	log.info("Legacy hedge engine started from %s", resolved)
	try:
		while True:
			heartbeat(
				component="legacy_engine",
				status="OK",
				worker_id=worker_id,
				summary=f"Running from {resolved}",
			)
			await asyncio.sleep(30)
	finally:
		await shutdown()


def _run_legacy_api(engine_path: str, host: str, port: int, worker_id: str) -> None:
	"""Serve the legacy FastAPI panel/API from the embedded backend."""
	resolved = _prepare_legacy_import_path(engine_path)

	from hedge_trader.trading.commands import heartbeat
	import uvicorn

	os.environ.setdefault("HOST", host)
	os.environ.setdefault("PORT", str(port))
	heartbeat(
		component="legacy_api",
		status="OK",
		worker_id=worker_id,
		mode=os.environ.get("HEDGE_RUNTIME_MODE", "Paper"),
		summary=f"Serving legacy API from {resolved} on {host}:{port}",
	)
	uvicorn.run("backend.main:app", host=host, port=port, log_level=os.environ.get("LOG_LEVEL", "info").lower())


async def _run_disabled(worker_id: str, poll_seconds: int, summary: str) -> None:
	from hedge_trader.trading.commands import heartbeat
	while True:
		heartbeat(
			component="frappe_command_worker",
			status="Paused",
			worker_id=worker_id,
			summary=summary,
		)
		await asyncio.sleep(max(5, poll_seconds))


async def _amain(args, runtime_config: dict[str, bool]):
	stop_event = asyncio.Event()

	def _stop(*_):
		stop_event.set()

	if hasattr(signal, "SIGTERM"):
		signal.signal(signal.SIGTERM, _stop)
	signal.signal(signal.SIGINT, _stop)

	enabled = runtime_config.get("engine_enabled", True)
	paused = runtime_config.get("global_pause", False)

	if not enabled or paused:
		summary = "Engine disabled in Hedge Trader Settings" if not enabled else "Global pause active in settings"
		task = asyncio.create_task(_run_disabled(args.worker_id, args.poll_seconds, summary))
	else:
		if args.legacy_engine_path:
			engine_path = args.legacy_engine_path
		elif args.embedded_legacy_engine:
			engine_path = str(_default_embedded_engine_path())
		else:
			engine_path = None

		if engine_path:
			task = asyncio.create_task(_run_legacy_engine(engine_path, args.worker_id))
		else:
			task = asyncio.create_task(_monitor_commands(args.worker_id, args.target, args.poll_seconds))

	await stop_event.wait()
	task.cancel()
	try:
		await task
	except asyncio.CancelledError:
		pass


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Hedge Trader dedicated runtime worker")
	parser.add_argument("--site", help="Frappe site name to connect before running")
	parser.add_argument("--worker-id", default="frappe-worker-1", help="Worker identifier shown in Frappe")
	parser.add_argument("--target", help="Command target to monitor, for example BullishExecutor_Paper")
	parser.add_argument("--poll-seconds", type=int, default=2, help="Command monitor heartbeat interval")
	parser.add_argument(
		"--legacy-engine-path",
		help="Path to a legacy engine folder containing backend/. Overrides the embedded runtime.",
	)
	parser.add_argument(
		"--embedded-legacy-engine",
		action="store_true",
		help="Start the legacy engine copied inside this Frappe app package.",
	)
	parser.add_argument(
		"--serve-legacy-api",
		action="store_true",
		help="Serve the embedded legacy FastAPI panel/API. This starts the engine through FastAPI startup.",
	)
	parser.add_argument("--legacy-api-host", default="0.0.0.0", help="Legacy API bind host")
	parser.add_argument("--legacy-api-port", type=int, default=8100, help="Legacy API bind port")
	return parser


def main(argv: list[str] | None = None):
	logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
	args = build_parser().parse_args(argv)
	frappe_module = _connect_frappe(args.site)
	try:
		runtime_config = _configure_site_runtime_defaults(frappe_module)
		if args.serve_legacy_api:
			engine_path = args.legacy_engine_path or str(_default_embedded_engine_path())
			_run_legacy_api(engine_path, args.legacy_api_host, args.legacy_api_port, args.worker_id)
		else:
			asyncio.run(_amain(args, runtime_config))
	finally:
		_disconnect_frappe(frappe_module)


if __name__ == "__main__":
	main()
