"""
Hedge Platform - Backend Entry Point.

Agents:
  - Spot + Futures + Options WebSocket feeds
  - AnalystAgent     : daily line computation (configurable IST time)
  - ManagerAgent     : risk gate for position size
  - BullishExecutor  : paper - long futures + ITM PUT hedge
  - BearishExecutor  : paper - short futures + ITM CALL hedge

Run:
  python -m backend.main
"""

import asyncio
import json
import logging
import os
import sys
import signal
from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# ── Load .env FIRST (before anything else) ──────────────────────────────────
try:
    from dotenv import load_dotenv
    _env_file = Path(__file__).parent.parent / ".env"
    if _env_file.exists():
        load_dotenv(_env_file)
        print(f"[main] Loaded .env from {_env_file}")
    else:
        print("[main] No .env file found — using system environment variables.")
except ImportError:
    print("[main] python-dotenv not installed — skipping .env load.")

# ── Timezone: set IST before any time operations ────────────────────────────
if not os.environ.get("TZ"):
    os.environ["TZ"] = "Asia/Kolkata"
try:
    import time as _time
    _time.tzset()
except AttributeError:
    pass  # Windows — TZ env var is enough for most libs

# ── Fix: python -m does NOT add backend.main to sys.modules ─────────────────
if "backend.main" not in sys.modules:
    sys.modules["backend.main"] = sys.modules[__name__]

# ── UTF-8 console on Windows ─────────────────────────────────────────────────
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import uvicorn


# ═══════════════════════════════════════════════════════════════════════════
# LOGGING SETUP — console + daily rotating file
# ═══════════════════════════════════════════════════════════════════════════

def _setup_logging():
    log_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s [%(name)-22s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handlers = [logging.StreamHandler(sys.stdout)]

    # Daily rotating log file
    log_dir = Path(os.environ.get("LOG_DIR", Path(__file__).parent.parent / "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "hedge_trader.log"
    file_handler = TimedRotatingFileHandler(
        filename=str(log_file),
        when="midnight",
        interval=1,
        backupCount=30,          # keep 30 days
        encoding="utf-8",
        utc=False,
    )
    file_handler.suffix = "%Y-%m-%d"
    handlers.append(file_handler)

    for h in handlers:
        h.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(log_level)
    for h in root.handlers[:]:
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)

    return log_dir

log_dir = _setup_logging()
log = logging.getLogger("main")


# ═══════════════════════════════════════════════════════════════════════════
# AGENT SINGLETONS
# ═══════════════════════════════════════════════════════════════════════════

from backend.message_bus import bus
from backend.config import cfg
from backend.state_store import store
from backend.utils import ist_now, ist_now_str

from backend.agents.analyst          import analyst
from backend.agents.manager          import manager
from backend.agents.bullish_executor import BullishExecutor
from backend.agents.bearish_executor import BearishExecutor
from backend.execution.paper_engine  import PaperEngine

_bull_paper = PaperEngine(); _bull_paper._state_key = "paper_engine_state_bull"
_bear_paper = PaperEngine(); _bear_paper._state_key = "paper_engine_state_bear"

bullish  = BullishExecutor(is_paper=True, force_window=False, paper_engine=_bull_paper)
bearish  = BearishExecutor(is_paper=True, force_window=False, paper_engine=_bear_paper)


# ═══════════════════════════════════════════════════════════════════════════
# BACKGROUND TASKS
# ═══════════════════════════════════════════════════════════════════════════

async def _squareoff_broadcaster(executor, h_key: str, m_key: str):
    from backend.message_bus import SQUAREOFF_START
    name = executor.name
    last_published_key = ""
    while True:
        try:
            now_ist = ist_now()
            sq_h    = int(getattr(cfg, h_key) or 0)
            sq_m    = int(getattr(cfg, m_key) or 0)
            sq_time = now_ist.replace(hour=sq_h, minute=sq_m, second=0, microsecond=0)
            publish_key = f"{name}:{sq_time.strftime('%Y-%m-%d')}:{sq_h:02d}:{sq_m:02d}"
            if now_ist >= sq_time and publish_key != last_published_key:
                last_published_key = publish_key
                log.info(f"[{name}] Broadcasting SQUAREOFF_START at {sq_h:02d}:{sq_m:02d} IST")
                await bus.publish(SQUAREOFF_START, {"ts_ist": ist_now_str(), "executor": name}, source="main")
            else:
                next_sq = sq_time if now_ist < sq_time else sq_time + timedelta(days=1)
                wait_sec = max((next_sq - now_ist).total_seconds(), 0)
                log.debug(f"[{name}] Squareoff watch: {wait_sec/3600:.2f}h until {next_sq.strftime('%H:%M')} IST")
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"[{name}] Squareoff broadcaster error: {e} — retrying in 60s")
            await asyncio.sleep(60)


async def _state_persister():
    """Persist executor states every 5s for crash recovery."""
    while True:
        await asyncio.sleep(5)
        for ex in (bullish, bearish):
            try:
                await store.set(f"{ex.name}_state", ex._serialize())
            except Exception as e:
                log.error(f"State persist failed for {ex.name}: {e}")


async def _nightly_reconciliation():
    """Independent ledger-vs-KV self-audit every night at 00:10 IST."""
    from backend import telegram_alert as tg
    while True:
        now = ist_now()
        target = now.replace(hour=0, minute=10, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep(max((target - now).total_seconds(), 1))
        issues = await store.reconcile_paper_state()
        if issues:
            payload = json.dumps(issues, default=str)[:3500]
            log.error(f"[reconciliation] PAPER STATE MISMATCH: {payload}")
            tg.send(
                "🚨 <b>PAPER STATE RECONCILIATION FAILED</b>\n"
                f"<pre>{payload}</pre>"
            )
        else:
            log.info("[reconciliation] paper_trades and kv_state match")


async def _health_monitor():
    """
    Every 30 min: heartbeat log + Telegram if feeds dead.
    Also auto-restarts feeds that have been silent for >5 min.
    """
    from backend.data import futures_feed, spot_feed, options_feed
    from backend import telegram_alert as tg
    import time as _time

    _feed_alert_sent = {"spot": False, "futures": False, "options": False}
    await asyncio.sleep(300)   # first check after 5 min (give feeds time to connect)

    while True:
        try:
            now_str = ist_now_str()
            now_ts  = _time.time()

            # ── Feed staleness check + auto-restart ───────────────────────
            def _feed_age(feed_mod, key):
                st = getattr(feed_mod, "_state", {}) if hasattr(feed_mod, "_state") else {}
                ts = float(st.get("bid_ts") or st.get("ts") or 0)
                return now_ts - ts if ts else 9999

            spot_age    = _feed_age(spot_feed, "spot")
            fut_age     = float(getattr(futures_feed, "_state", {}).get("bid_ts") or 0)
            fut_age     = now_ts - fut_age if fut_age else 9999

            for name, age, feed_mod, alert_key in [
                ("Spot",    spot_age, spot_feed,    "spot"),
                ("Futures", fut_age,  futures_feed,  "futures"),
            ]:
                if age > 300:   # >5 min silent → alert + restart
                    if not _feed_alert_sent[alert_key]:
                        log.error(f"[watchdog] {name} feed DEAD for {age:.0f}s — restarting")
                        tg.send(
                            f"🔴 <b>Feed Dead — Auto-Restart</b>\n"
                            f"{name} feed silent for {int(age//60)}m {int(age%60)}s\n"
                            f"Attempting reconnect...\nTime: {now_str}"
                        )
                        _feed_alert_sent[alert_key] = True
                    try:
                        await feed_mod.stop()
                        await asyncio.sleep(2)
                        await feed_mod.start()
                        log.info(f"[watchdog] {name} feed restarted.")
                    except Exception as re:
                        log.error(f"[watchdog] {name} restart failed: {re}")
                else:
                    if _feed_alert_sent[alert_key]:
                        tg.send(f"✅ <b>{name} Feed Recovered</b>\nTime: {now_str}")
                    _feed_alert_sent[alert_key] = False

            # ── Heartbeat every 30 min ────────────────────────────────────
            spot_price = float(getattr(spot_feed, "_state", {}).get("last_price") or 0)
            states = {
                "bull": bullish.state.value  if hasattr(bullish, "state")  else "?",
                "bear": bearish.state.value  if hasattr(bearish, "state")  else "?",
            }
            log.info(
                f"[heartbeat] {now_str} | BTC=${spot_price:,.0f} | "
                f"bull={states['bull']} bear={states['bear']}"
            )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"[heartbeat] Monitor error: {e}")
        await asyncio.sleep(300)   # check every 5 min


# ═══════════════════════════════════════════════════════════════════════════
# STARTUP / SHUTDOWN
# ═══════════════════════════════════════════════════════════════════════════

async def startup():
    from backend import telegram_alert as tg

    log.info("=" * 64)
    log.info("  HEDGE TRADER  -  Bull/Bear Platform")
    log.info("  Bull | Bear - each $100k independent balance")
    log.info(f"  Started: {ist_now_str()}")
    log.info(f"  Log directory: {log_dir}")
    log.info("=" * 64)

    # 1. State store
    await store.connect()
    log.info(f"MariaDB database: {store._db_path}")

    # 2. Restore persisted user config
    user_cfg = store.get("user_config")
    if user_cfg:
        for k, v in user_cfg.items():
            if hasattr(cfg, k):
                try:
                    setattr(cfg, k, type(getattr(cfg, k))(v))
                except Exception:
                    setattr(cfg, k, v)
        log.info(f"User config restored: {len(user_cfg)} key(s)")

    # 2. Restore configs from Frappe if available, else use default fallback hardcoded values
    loaded_from_frappe = False
    try:
        from backend.frappe_bridge import bridge
        frappe = bridge._ready()
        if frappe:
            configs = frappe.get_all("Hedge Strategy Config", fields=["*"])
            for doc in configs:
                side = "bull" if doc.direction == "Bullish" else "bear" if doc.direction == "Bearish" else None
                if not side:
                    continue
                setattr(cfg, f"{side}_force_close_h", int(doc.force_close_h if doc.force_close_h is not None else 12))
                setattr(cfg, f"{side}_force_close_m", int(doc.force_close_m if doc.force_close_m is not None else 0))
                setattr(cfg, f"{side}_trade_start_h", int(doc.trade_start_h if doc.trade_start_h is not None else 5))
                setattr(cfg, f"{side}_trade_start_m", int(doc.trade_start_m if doc.trade_start_m is not None else 0))
                setattr(cfg, f"{side}_trade_end_h", int(doc.trade_end_h if doc.trade_end_h is not None else 7))
                setattr(cfg, f"{side}_trade_end_m", int(doc.trade_end_m if doc.trade_end_m is not None else 30))
                setattr(cfg, f"{side}_max_premium", float(doc.max_premium if doc.max_premium is not None else 220.0))
                setattr(cfg, f"{side}_max_time_value", float(doc.max_time_value if doc.max_time_value is not None else 219.0))
                setattr(cfg, f"{side}_contract_qty", float(doc.contract_qty if doc.contract_qty is not None else 10.0))
                setattr(cfg, f"{side}_session_pnl_target", 600.0)
                setattr(cfg, f"{side}_partial_profit_ratio", float(doc.partial_profit_ratio if doc.partial_profit_ratio is not None else 1.10))
                setattr(cfg, f"{side}_partial_tp_multiplier", float(doc.partial_tp_multiplier if doc.partial_tp_multiplier is not None else 1.10))
                if doc.rebuy_mode:
                    setattr(cfg, f"{side}_rebuy_mode", doc.rebuy_mode)
                
                # Also set first/second trader defaults
                setattr(cfg, f"{side}_first_trader_max_premium", float(doc.max_premium if doc.max_premium is not None else 220.0))
                setattr(cfg, f"{side}_first_trader_max_time_value", float(doc.max_time_value if doc.max_time_value is not None else 219.0))
                setattr(cfg, f"{side}_second_trader_max_premium", float(doc.max_premium) - 30.0 if doc.max_premium is not None else 190.0)
                setattr(cfg, f"{side}_second_trader_max_time_value", float(doc.max_time_value) - 30.0 if doc.max_time_value is not None else 189.0)
                for role in ("first", "second"):
                    setattr(cfg, f"{side}_{role}_contract_qty", float(doc.contract_qty if doc.contract_qty is not None else 10.0))
            loaded_from_frappe = True
            log.info("Successfully loaded strategy configuration values from Frappe.")
    except Exception as exc:
        log.warning(f"Could not load Hedge Strategy Configs from Frappe: {exc}")

    if not loaded_from_frappe:
        # Fallback to legacy hardcoded defaults
        cfg.bull_force_close_h = 12
        cfg.bull_force_close_m = 0
        cfg.bear_force_close_h = 12
        cfg.bear_force_close_m = 0
        cfg.bull_trade_start_h = 5
        cfg.bull_trade_start_m = 0
        cfg.bull_trade_end_h = 7
        cfg.bull_trade_end_m = 30
        cfg.bear_trade_start_h = 5
        cfg.bear_trade_start_m = 0
        cfg.bear_trade_end_h = 7
        cfg.bear_trade_end_m = 30
        cfg.bull_max_premium = 220.0
        cfg.bear_max_premium = 220.0
        cfg.bull_max_time_value = 219.0
        cfg.bear_max_time_value = 219.0
        cfg.bull_contract_qty = 10.0
        cfg.bear_contract_qty = 10.0
        for side in ("bull", "bear"):
            setattr(cfg, f"{side}_first_trader_max_premium", 220.0)
            setattr(cfg, f"{side}_first_trader_max_time_value", 219.0)
            setattr(cfg, f"{side}_second_trader_max_premium", 190.0)
            setattr(cfg, f"{side}_second_trader_max_time_value", 189.0)
            for role in ("first", "second"):
                setattr(cfg, f"{side}_{role}_contract_qty", 10.0)

    # 3. Redis (optional)
    redis_url = os.environ.get("REDIS_URL", "")
    if redis_url:
        await bus.connect_redis(redis_url)

    # 4. Data feeds
    from backend.data import spot_feed, futures_feed, options_feed
    await futures_feed.fetch_historical_candles(500, "5m")
    await futures_feed.fetch_historical_candles(500, "15m")
    await futures_feed.fetch_historical_candles(300, "1h")
    await futures_feed.fetch_historical_candles(200, "4h")
    await spot_feed.start()
    await futures_feed.start()
    await options_feed.start()
    log.info("WebSocket data feeds started.")

    # 5. Agents
    await analyst.start()
    await manager.start()
    await bullish.start()
    await bearish.start()
    log.info("Both directional traders started.")

    # 6. Background tasks - wrapped in auto-restart guard so crashes don't silently kill them
    def _guarded(make_coro, name: str):
        """Wrap a coroutine factory: if the task crashes, log and restart it after 10s."""
        async def _wrapper():
            while True:
                try:
                    await make_coro()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.error(f"[guarded:{name}] crashed: {e} — restarting in 10s")
                    await asyncio.sleep(10)
        return asyncio.create_task(_wrapper(), name=name)

    _guarded(lambda: _squareoff_broadcaster(bullish, "bull_force_close_h", "bull_force_close_m"), "squareoff_bull")
    _guarded(lambda: _squareoff_broadcaster(bearish, "bear_force_close_h", "bear_force_close_m"), "squareoff_bear")
    _guarded(lambda: _state_persister(),    "state_persister")
    _guarded(lambda: _nightly_reconciliation(), "nightly_reconciliation")
    _guarded(lambda: _health_monitor(),     "health_monitor")

    log.info("System fully online.")
    log.info(f"  Bullish : {bullish.name}  (paper, 24/7)")
    log.info(f"  Bearish : {bearish.name}  (paper, 24/7)")
    log.info("=" * 64)

    # ── Full health check on startup ────────────────────────────────────────
    await asyncio.sleep(3)   # let feeds settle
    health_lines = []
    all_ok = True

    # DB
    db_ok = bool(store._db_path)
    health_lines.append(f"{'✅' if db_ok else '❌'} DB: {'MariaDB ready' if db_ok else 'NOT CONNECTED'}")
    if not db_ok: all_ok = False

    # PG

    # Price feed
    from backend.data.spot_feed import get_state as _ss
    spot_bid = float(_ss().get("bid") or 0)
    feed_ok  = spot_bid > 0
    health_lines.append(f"{'✅' if feed_ok else '❌'} BTC Feed: {'$' + f'{spot_bid:,.0f}' if feed_ok else 'NO DATA'}")
    if not feed_ok: all_ok = False

    # Options
    from backend.data.options_feed import get_chain as _gc
    opts = len(_gc())
    opts_ok = opts >= 10
    health_lines.append(f"{'✅' if opts_ok else '❌'} Options: {opts} strikes {'ready' if opts_ok else '— LOW'}")

    # Env vars
    missing_env = [k for k in ["BINANCE_API_KEY", "BINANCE_API_SECRET"] if not os.environ.get(k)]
    if missing_env:
        health_lines.append(f"⚠️ Missing env: {', '.join(missing_env)} (paper mode OK)")
    else:
        health_lines.append("✅ Env vars: all set")

    # Next session
    from backend.utils import ist_now as _isn
    from backend.config import cfg as _cfg
    from datetime import timedelta as _td
    _n = _isn()
    _exp_h = int(getattr(_cfg, "session_expiry_h", 13))
    _exp_m = int(getattr(_cfg, "session_expiry_m", 30))
    _expiry = _n.replace(hour=_exp_h, minute=_exp_m, second=0, microsecond=0)
    if _n >= _expiry: _expiry += _td(days=1)
    _mins = int((_expiry - _n).total_seconds() / 60)
    health_lines.append(f"📅 Next expiry: {_expiry.strftime('%d/%m %H:%M IST')} (in {_mins//60}h {_mins%60}m)")

    status_icon = "✅" if all_ok else "⚠️"
    tg.send(
        f"{status_icon} <b>Hedge Trader ONLINE</b>\n"
        f"Time: {ist_now_str()}\n\n"
        + "\n".join(health_lines) +
        f"\n\n<b>Traders:</b>\n"
        f"  Bull: {bullish.name}\n"
        f"  Bear: {bearish.name}"
    )


async def shutdown():
    from backend import telegram_alert as tg
    log.info("Shutting down gracefully...")
    tg.send(f"🔴 <b>Hedge Trader OFFLINE</b>\nTime: {ist_now_str()}")
    from backend.data import spot_feed, futures_feed, options_feed
    try:
        await spot_feed.stop()
        await futures_feed.stop()
        await options_feed.stop()
        await analyst.stop()
        await bullish.stop()
        await bearish.stop()
        # Final state save
        for ex in (bullish, bearish):
            try:
                await store.set(f"{ex.name}_state", ex._serialize())
            except Exception:
                pass
        await store.close()
    except Exception as e:
        log.error(f"Shutdown error: {e}")
    log.info("Shutdown complete.")


# ═══════════════════════════════════════════════════════════════════════════
# GLOBAL EXCEPTION HANDLER — catch all uncaught async exceptions
# ═══════════════════════════════════════════════════════════════════════════

def _handle_exception(loop, context):
    msg = context.get("exception", context["message"])
    log.error(f"[asyncio] Uncaught exception: {msg}", exc_info=context.get("exception"))
    from backend import telegram_alert as tg
    tg.send(
        f"🚨 <b>Hedge Trader ERROR</b>\n"
        f"{str(msg)[:400]}\n"
        f"Time: {ist_now_str()}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# FASTAPI APP
# ═══════════════════════════════════════════════════════════════════════════

from backend.api.gateway import app


@app.on_event("startup")
async def fastapi_startup():
    loop = asyncio.get_event_loop()
    loop.set_exception_handler(_handle_exception)
    await startup()


@app.on_event("shutdown")
async def fastapi_shutdown():
    await shutdown()


# ── Health check endpoint ──────────────────────────────────────────────────
@app.get("/health")
async def health_check():
    from backend.data import futures_feed
    price = getattr(futures_feed, "_last_price", 0)
    return {
        "status":   "ok",
        "ts_ist":   ist_now_str(),
        "btc_price": price,
        "traders": {
            "bull": bullish.state.value  if hasattr(bullish,  "state")  else "?",
            "bear": bearish.state.value  if hasattr(bearish,  "state")  else "?",
        },
    }


# ═══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))

    log.info(f"Starting uvicorn on {host}:{port}")

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        reload=False,
        access_log=True,
    )
