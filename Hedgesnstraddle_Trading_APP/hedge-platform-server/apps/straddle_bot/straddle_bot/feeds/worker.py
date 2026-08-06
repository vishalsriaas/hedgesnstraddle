"""Dedicated runtime worker entrypoint for the embedded straddle bot."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import os
import signal
from pathlib import Path
from typing import Any

log = logging.getLogger("straddle_bot.worker")


def _connect_frappe(site: str | None):
	if not site:
		return None

	import frappe

	sites_path = Path.cwd() / "sites"
	if sites_path.exists():
		frappe.init(site=site, sites_path=str(sites_path))
	else:
		frappe.init(site=site)
	frappe.connect()
	return frappe


def _disconnect_frappe(frappe_module) -> None:
	if frappe_module is not None:
		frappe_module.destroy()


def _setenv_default(name: str, value: Any) -> None:
	if value in (None, "") or os.environ.get(name):
		return
	os.environ[name] = str(value)


def _password(settings, fieldname: str) -> str | None:
	try:
		value = settings.get_password(fieldname, raise_exception=False)
	except TypeError:
		try:
			value = settings.get_password(fieldname)
		except Exception:
			value = None
	except Exception:
		value = None
	return value or None


def _time_value(value: Any) -> str | None:
	if value in (None, ""):
		return None
	if isinstance(value, dt.time):
		return value.strftime("%H:%M:%S")
	if isinstance(value, dt.timedelta):
		total_seconds = int(value.total_seconds())
		hours, remainder = divmod(total_seconds, 3600)
		minutes, seconds = divmod(remainder, 60)
		return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
	return str(value)


def _truthy(value: Any) -> bool:
	return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _configure_site_runtime_defaults(frappe_module) -> dict[str, Any]:
	if frappe_module is None:
		_setenv_default("DB_PATH", Path(__file__).resolve().parents[1] / "runtime" / "legacy_bot" / "straddle_paper.db")
		return {"bot_enabled": True, "runtime_mode": "Paper"}

	runtime_dir = Path(frappe_module.get_site_path("private", "files", "straddle_bot"))
	runtime_dir.mkdir(parents=True, exist_ok=True)

	settings = None
	try:
		settings = frappe_module.get_single("Straddle Bot Settings")
	except Exception as exc:
		log.warning("Could not load Straddle Bot Settings: %s", exc)

	db_path = settings.get("db_path") if settings else None
	_setenv_default("DB_PATH", db_path or runtime_dir / "straddle_paper.db")

	if not settings:
		return {"bot_enabled": True, "runtime_mode": "Paper"}

	runtime_mode = settings.get("runtime_mode") or "Paper"
	paper_enabled = _truthy(settings.get("paper_trading_enabled")) or runtime_mode != "Live"
	_setenv_default("STRADDLE_RUNTIME_MODE", runtime_mode)
	_setenv_default("STRADDLE_PAPER_TRADE", "1" if paper_enabled else "0")

	_setenv_default("BINANCE_API_KEY", _password(settings, "binance_api_key"))
	_setenv_default("BINANCE_API_SECRET", _password(settings, "binance_secret_key"))
	_setenv_default("BINANCE_SECRET_KEY", os.environ.get("BINANCE_API_SECRET"))
	_setenv_default("TELEGRAM_BOT_TOKEN", _password(settings, "telegram_token"))
	_setenv_default("TELEGRAM_TOKEN", os.environ.get("TELEGRAM_BOT_TOKEN"))
	_setenv_default("TELEGRAM_CHAT_ID", settings.get("telegram_chat_id"))

	_setenv_default("STRADDLE_WINDOW_START", _time_value(settings.get("entry_window_open")))
	_setenv_default("STRADDLE_WINDOW_END", _time_value(settings.get("entry_window_close")))
	_setenv_default("STRADDLE_SQUAREOFF_START", _time_value(settings.get("squareoff_start")))
	_setenv_default("STRADDLE_SQUAREOFF_END", _time_value(settings.get("squareoff_end")))
	_setenv_default("STRADDLE_FUTURES_ENTRY_CUTOFF", _time_value(settings.get("futures_entry_cutoff")))
	_setenv_default("STRADDLE_FUTURES_SQUAREOFF", _time_value(settings.get("futures_squareoff")))
	_setenv_default("STRADDLE_EXPIRY_TIME", _time_value(settings.get("expiry_time")))
	_setenv_default("STRADDLE_TRADE_QTY", settings.get("trade_qty"))
	_setenv_default("STRADDLE_MIN_EXPIRY_HOURS", settings.get("min_expiry_hours"))
	_setenv_default("STRADDLE_MIN_STRIKE_GAP", settings.get("min_strike_gap"))
	_setenv_default("STRADDLE_MAX_TOTAL_ASK", settings.get("max_total_ask"))
	_setenv_default("STRADDLE_MAX_PREMIUM_GAP", settings.get("max_premium_gap"))
	_setenv_default("STRADDLE_FUTURES_TP_MULTIPLIER", settings.get("futures_tp_multiplier"))
	_setenv_default("STRADDLE_SCAN_INTERVAL_SECONDS", settings.get("scan_interval_seconds"))
	_setenv_default("STRADDLE_RETRY_TIMEOUT_SECONDS", settings.get("retry_timeout_seconds"))
	_setenv_default("STRADDLE_FUTURES_LEVERAGE", settings.get("futures_leverage"))
	_setenv_default("STRADDLE_FUTURES_MAINTENANCE_MARGIN_RATE", settings.get("futures_maintenance_margin_rate"))
	_setenv_default("STRADDLE_PAPER_WALLET_USDT", settings.get("paper_wallet_usdt"))

	return {"bot_enabled": _truthy(settings.get("bot_enabled")), "runtime_mode": runtime_mode}


def _safe_heartbeat(**kwargs) -> None:
	try:
		from straddle_bot.trading.commands import heartbeat

		heartbeat(**kwargs)
	except Exception as exc:
		log.debug("Heartbeat skipped: %s", exc)


async def _monitor_commands(worker_id: str, target: str | None, poll_seconds: int) -> None:
	from straddle_bot.trading.commands import get_pending_commands

	while True:
		pending = get_pending_commands(target=target, limit=20)
		_safe_heartbeat(
			component="frappe_command_worker",
			status="OK",
			worker_id=worker_id,
			mode=os.environ.get("STRADDLE_RUNTIME_MODE", "Paper"),
			summary=f"{len(pending)} pending command(s)",
			detail={"target": target or "all", "pending": pending},
		)
		if pending:
			log.info("Pending command(s): %s", ", ".join(row["name"] for row in pending))
		await asyncio.sleep(max(1, poll_seconds))


async def _heartbeat_loop(worker_id: str, mode: str, status: str = "OK", summary: str = "Running") -> None:
	while True:
		sync_status = None
		try:
			import frappe
			sync_status = frappe.cache().get_value("straddle_bot:sqlite_sync")
		except Exception:
			pass
		_safe_heartbeat(
			component="legacy_straddle_bot",
			status=status,
			worker_id=worker_id,
			mode=mode,
			summary=summary,
			detail={"db_path": os.environ.get("DB_PATH"), "sqlite_sync": sync_status},
		)
		await asyncio.sleep(30)


async def _run_disabled(worker_id: str, poll_seconds: int) -> None:
	while True:
		_safe_heartbeat(
			component="legacy_straddle_bot",
			status="Paused",
			worker_id=worker_id,
			mode=os.environ.get("STRADDLE_RUNTIME_MODE", "Paper"),
			summary="Bot disabled in Straddle Bot Settings",
		)
		await asyncio.sleep(max(5, poll_seconds))


async def _run_legacy_bot(worker_id: str, runtime_mode: str) -> None:
	from straddle_bot.runtime.legacy_bot import straddle_trader
	from straddle_bot.trading.sqlite_sync import sync_loop

	heartbeat_task = asyncio.create_task(
		_heartbeat_loop(worker_id, runtime_mode, summary="Embedded straddle bot running")
	)
	sync_task = asyncio.create_task(sync_loop())
	try:
		await straddle_trader.main()
	finally:
		heartbeat_task.cancel()
		sync_task.cancel()
		await asyncio.gather(heartbeat_task, sync_task, return_exceptions=True)
		_safe_heartbeat(
			component="legacy_straddle_bot",
			status="Paused",
			worker_id=worker_id,
			mode=runtime_mode,
			summary="Embedded straddle bot stopped",
		)


def _run_dashboard(args) -> None:
	import uvicorn
	from straddle_bot.runtime.legacy_bot import dashboard

	dashboard._db_path = os.environ["DB_PATH"]
	dashboard._ensure_schema()
	_safe_heartbeat(
		component="legacy_straddle_dashboard",
		status="OK",
		worker_id=args.worker_id,
		mode=os.environ.get("STRADDLE_RUNTIME_MODE", "Paper"),
		summary=f"Dashboard listening on {args.dashboard_host}:{args.dashboard_port}",
		detail={"db_path": dashboard._db_path},
	)
	uvicorn.run(dashboard.app, host=args.dashboard_host, port=args.dashboard_port, log_level="warning")


async def _amain(args, runtime_config: dict[str, Any]) -> None:
	stop_event = asyncio.Event()

	def _stop(*_) -> None:
		stop_event.set()

	if hasattr(signal, "SIGTERM"):
		signal.signal(signal.SIGTERM, _stop)
	signal.signal(signal.SIGINT, _stop)

	if args.mode == "bot":
		enabled = runtime_config.get("bot_enabled", True)
		if not enabled and not _truthy(os.environ.get("STRADDLE_FORCE_START")):
			task = asyncio.create_task(_run_disabled(args.worker_id, args.poll_seconds))
		else:
			task = asyncio.create_task(_run_legacy_bot(args.worker_id, runtime_config.get("runtime_mode") or "Paper"))
	else:
		task = asyncio.create_task(_monitor_commands(args.worker_id, args.target, args.poll_seconds))

	await stop_event.wait()
	task.cancel()
	await asyncio.gather(task, return_exceptions=True)


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Straddle Bot dedicated runtime worker")
	parser.add_argument("--site", help="Frappe site name to connect before running")
	parser.add_argument("--worker-id", default="straddle-worker-1", help="Worker identifier shown in Frappe")
	parser.add_argument(
		"--mode",
		choices=["command-monitor", "bot", "dashboard"],
		default="command-monitor",
		help="Runtime process to start",
	)
	parser.add_argument("--target", help="Command target to monitor")
	parser.add_argument("--poll-seconds", type=int, default=2, help="Command monitor heartbeat interval")
	parser.add_argument("--dashboard-host", default="0.0.0.0", help="Dashboard bind host")
	parser.add_argument("--dashboard-port", type=int, default=8080, help="Dashboard bind port")
	return parser


def main(argv: list[str] | None = None) -> None:
	logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
	args = build_parser().parse_args(argv)
	frappe_module = _connect_frappe(args.site)
	try:
		runtime_config = _configure_site_runtime_defaults(frappe_module)
		if args.mode == "dashboard":
			_run_dashboard(args)
		else:
			asyncio.run(_amain(args, runtime_config))
	finally:
		_disconnect_frappe(frappe_module)


if __name__ == "__main__":
	main()
