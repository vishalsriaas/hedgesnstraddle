#!/usr/bin/env python3
"""
straddle_trader.py  -  BTC ITM Straddle Bot
==========================================
Strategy:
  1. Find nearest ITM CALL + nearest ITM PUT pair
  2. Buy both simultaneously (FOK) if all conditions pass
  3. Immediately place opposing LONG + SHORT futures LIMIT orders:
       LONG  limit = PUT_strike  âˆ' total_premium_paid
       SHORT limit = CALL_strike + total_premium_paid
  4. When each futures fills -> TP at fill +- (total_prem + TV_at_entry)
     Immediately place liq protection option at intrinsic value at liq price
  5. Protection is placed once on fill — no periodic polling
  6. Squareoff: 15m kline high/low determines if TP was hit during session
     Options closed at intrinsic value; futures exit at TP price or spot

Run:  python straddle_trader.py
Stop: Ctrl+C
"""

import asyncio
import concurrent.futures
import hashlib
import hmac
import json
import re
import signal
import os
import socket
from straddle_bot import mariadb_compat as dbapi
import sys
import time
from pathlib import Path
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

# Force UTF-8 on Windows consoles / log files so emoji in log() never crash
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ╔╗
# â•'                        CREDENTIALS                                        â•'
# ╚
def _env_text(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value not in (None, ""):
            return value
    return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on", "live"}


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    try:
        return int(float(value))
    except ValueError:
        return default


def _env_time(name: str, default: tuple[int, int]) -> tuple[int, int]:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    parts = value.replace(",", ":").split(":")
    if len(parts) < 2:
        return default
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return default


# Supplied by the Frappe worker or process environment. Do not hardcode secrets.
BINANCE_API_KEY    = _env_text("BINANCE_API_KEY")
BINANCE_SECRET_KEY = _env_text("BINANCE_API_SECRET", "BINANCE_SECRET_KEY")
TELEGRAM_TOKEN     = _env_text("TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID   = _env_text("TELEGRAM_CHAT_ID")


# ╔╗
# â•'                        TRADING SETTINGS                                   â•'
# â•'  All strategy parameters here  -  change only this block.                   â•'
# ╚

# â"€â"€ Entry Window (IST) â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
WINDOW_START    = (5, 30)
WINDOW_END      = (8, 0)

# â"€â"€ Squareoff Window (IST) â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
FUTURES_ENTRY_CUTOFF = (11, 0)
SQUAREOFF_START = (11, 0)
SQUAREOFF_HARD  = (12, 0)
FUTURES_SQUAREOFF = (12, 0)
EXPIRY_TIME     = _env_time("STRADDLE_EXPIRY_TIME", (13, 30))       # used for session/chain logic

# â"€â"€ Trade Size â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
TRADE_QTY       = 10.0       # BTC qty per leg

# â"€â"€ Entry Conditions â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
MIN_EXPIRY_HOURS   = _env_float("STRADDLE_MIN_EXPIRY_HOURS", 0.0)
MIN_STRIKE_GAP     = _env_float("STRADDLE_MIN_STRIKE_GAP", 0)
#                              guarantees minimum "locked" intrinsic between strikes
MAX_PREMIUM_GAP    = _env_float("STRADDLE_MAX_PREMIUM_GAP", 130)
MAX_TOTAL_MARK     = 400.0
OPTIONS_RECOVERY_PCT = 75.0
FUTURES_TP_MULTIPLIER = _env_float("STRADDLE_FUTURES_TP_MULTIPLIER", 2.0)
SCAN_INTERVAL      = _env_float("STRADDLE_SCAN_INTERVAL_SECONDS", 2.0)
RETRY_TIMEOUT      = _env_int("STRADDLE_RETRY_TIMEOUT_SECONDS", 60)

# â"€â"€ Futures Management â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# TP distance is computed from premium + time value, NOT a fixed constant.
# Formula (in _on_both_filled):  tp_dist = max(prem + (prem - gap), 50)
#   where gap = put_strike - call_strike,  prem = total_premium_paid
#   LONG  TP = long_entry  + tp_dist
#   SHORT TP = short_entry - tp_dist


# â"€â"€ Binance Endpoints â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
#  Paper Trading 
PAPER_TRADE        = _env_bool("STRADDLE_PAPER_TRADE", True)
_PTAG = "[PAPER] " if PAPER_TRADE else ""  # Telegram prefix
FUTURES_LEVERAGE   = _env_int("STRADDLE_FUTURES_LEVERAGE", 100)
FUTURES_MM_RATE    = _env_float("STRADDLE_FUTURES_MAINTENANCE_MARGIN_RATE", 0.004)
PAPER_WALLET_USDT  = _env_float("STRADDLE_PAPER_WALLET_USDT", 100_000.0)
DASHBOARD_PASSWORD = _env_text("STRADDLE_DASHBOARD_PASSWORD", default="admin1234")
DB_NAME            = os.environ.get("MARIADB_DATABASE", "")

FAPI = "https://fapi.binance.com"
EAPI = "https://eapi.binance.com"
IST  = timezone(timedelta(hours=5, minutes=30))


# ╔╗
# â•'                        TERMINAL LOGGING                                   â•'
# ╚
_R   = "\033[0m";  _B   = "\033[1m"
_GRN = "\033[92m"; _YLW = "\033[93m"; _RED = "\033[91m"
_CYN = "\033[96m"; _BLU = "\033[94m"; _MGT = "\033[95m"; _WHT = "\033[97m"
_LEVEL_CLR = {"INFO": _WHT, "WARN": _YLW, "ERROR": _RED,
              "OK":   _GRN, "TRADE": _CYN, "STATE": _BLU, "CRIT": _MGT}
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')
_IS_TTY  = sys.stdout.isatty()

def log(tag: str, msg: str, level: str = "INFO"):
    ts  = datetime.now(IST).strftime("%H:%M:%S")
    tc  = _CYN if tag == "SYS" else (_GRN if tag == "BOT" else _YLW)
    lc  = _LEVEL_CLR.get(level, _WHT)
    pfx = f"{_WHT}{ts}{_R}  {tc}{_B}[{tag:4s}]{_R}  {lc}{level:5s}{_R}  "
    if not _IS_TTY:
        pfx = _ANSI_RE.sub("", pfx)
        msg = _ANSI_RE.sub("", msg)
    lines = msg.split("\n")
    print(pfx + lines[0], flush=True)
    for line in lines[1:]:
        print(" " * 26 + line, flush=True)

def separator(title: str = ""):
    bar = (_CYN + "" * 72 + _R) if _IS_TTY else ("" * 72)
    print(f"\n{bar}")
    if title:
        if _IS_TTY:
            print(f"{_B}{_CYN}  {title}{_R}")
        else:
            print(f"  {title}")
        print(bar)
    print()


# ╔╗
# â•'                        TELEGRAM                                           â•'
# ╚
async def _tg(text: str):
    if not TELEGRAM_CHAT_ID:
        return
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        body = json.dumps({"chat_id": TELEGRAM_CHAT_ID,
                           "text": text, "parse_mode": "HTML"}).encode()
        def _send():
            req = urllib.request.Request(url, data=body,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=8)
        await asyncio.get_running_loop().run_in_executor(None, _send)
    except Exception as e:
        log("SYS", f"Telegram failed: {e}", "WARN")


# 
#  DATABASE  (MariaDB)
# 
_db: Optional[dbapi.Connection] = None
_active_bot = None
_control_paused = False

def _ts() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

def _db_init():
    global _db
    if not DB_NAME:
        raise RuntimeError("MARIADB_DATABASE is required; MariaDB fallback was removed")
    from straddle_bot.mariadb_compat import ensure_schema
    ensure_schema()
    _db = dbapi.connect(DB_NAME, check_same_thread=False)
    _db.row_factory = dbapi.Row

    # key, default_value, label, input_type, is_sensitive, section, sort_order
    defaults = [
        ("WINDOW_START",       f"{WINDOW_START[0]:02d}:{WINDOW_START[1]:02d}",
         "Entry Window Open",        "time",     0, "time",       1),
        ("WINDOW_END",         f"{WINDOW_END[0]:02d}:{WINDOW_END[1]:02d}",
         "Entry Window Close",       "time",     0, "time",       2),
        ("FUTURES_ENTRY_CUTOFF", f"{FUTURES_ENTRY_CUTOFF[0]:02d}:{FUTURES_ENTRY_CUTOFF[1]:02d}",
         "Futures Entry Cutoff",     "time",     0, "time",       3),
        ("SQ_START",           f"{SQUAREOFF_START[0]:02d}:{SQUAREOFF_START[1]:02d}",
         "Squareoff Start",          "time",     0, "time",       3),
        ("SQ_END",             f"{SQUAREOFF_HARD[0]:02d}:{SQUAREOFF_HARD[1]:02d}",
         "Squareoff End",            "time",     0, "time",       4),
        ("FUTURES_SQUAREOFF", f"{FUTURES_SQUAREOFF[0]:02d}:{FUTURES_SQUAREOFF[1]:02d}",
         "Futures Hard Squareoff",   "time",     0, "time",       5),
        ("MIN_EXPIRY_HOURS",   str(MIN_EXPIRY_HOURS),
         "Min Expiry (hours)",       "number",   0, "entry",      1),
        ("MIN_STRIKE_GAP",     str(MIN_STRIKE_GAP),
         "Min Strike Gap (USDT)",    "number",   0, "entry",      2),
        ("MAX_TOTAL_MARK",      str(MAX_TOTAL_MARK),
         "Max Combined Mark Premium (USDT)", "number", 0, "entry", 4),
        ("MAX_PREMIUM_GAP", str(MAX_PREMIUM_GAP),
         "Max Premium Gap (USDT)",   "number",   0, "entry",      5),
        ("FUTURES_TP_MULTIPLIER", str(FUTURES_TP_MULTIPLIER),
         "Futures TP Multiplier",    "number",   0, "exit",       2),
        ("TRADE_QTY",          str(TRADE_QTY),
         "Trade Qty (BTC)",          "number",   0, "sizing",     1),
        ("PAPER_WALLET_USDT",  str(PAPER_WALLET_USDT),
         "Paper Wallet (USDT)",      "number",   0, "sizing",     2),
        ("DASHBOARD_PASSWORD", DASHBOARD_PASSWORD,
         "Dashboard Password",       "password", 1, "security",   1),
        ("_config_dirty",      "0",
         "Config Dirty Flag",        "text",     1, "system",     99),
        ("PAPER_WALLET_BALANCE", str(PAPER_WALLET_USDT),
         "Paper Wallet Balance",     "number",   1, "system",     98),
        ("LAST_TRADED_EXPIRY",  "",
         "Last Traded Expiry",       "text",     1, "system",     97),
    ]
    for key, val, label, inp, sens, section, sort_order in defaults:
        _db.execute(
            "INSERT OR IGNORE INTO config "
            "(key,value,label,input_type,is_sensitive,section,sort_order) "
            "VALUES (?,?,?,?,?,?,?)",
            (key, val, label, inp, sens, section, sort_order)
        )
        # Always refresh metadata (label, section, sort_order) without touching user's saved value
        _db.execute(
            "UPDATE config SET label=?, section=?, sort_order=? WHERE key=?",
            (label, section, sort_order, key)
        )
    _db.execute(
        "DELETE FROM config WHERE key IN "
        "('MAX_ASK_MARK_PCT','MAX_ASK_MARK_RETRY','MIN_ASK_PER_LEG',"
        "'MAX_TOTAL_ASK','OPTIONS_RECOVERY_PCT')"
    )
    # Fixed strategy capital. Migrate the previous untouched $10k default
    # without overwriting a wallet that already contains trading PnL.
    _db.execute("UPDATE config SET value='100000' WHERE key='PAPER_WALLET_USDT'")
    old_wallet = _db.execute(
        "SELECT value FROM config WHERE key='PAPER_WALLET_BALANCE'"
    ).fetchone()
    if old_wallet and abs(float(old_wallet[0] or 0) - 10_000.0) < 0.01:
        _db.execute(
            "UPDATE config SET value='100000' WHERE key='PAPER_WALLET_BALANCE'"
        )
        _db.execute(
            "INSERT INTO wallet_ledger (ts,session_id,type,amount,balance_after,note) "
            "VALUES (?,0,'CREDIT',90000,100000,'Wallet resized from 10k to 100k')",
            (_ts(),),
        )
    # Defensive expiry reconciliation: an offline bot must not resurrect an
    # already-expired ACTIVE session as a running position.
    now_ist_text = datetime.now(IST).strftime("%Y-%m-%d %H:%M")
    expired_ids = [
        int(row[0]) for row in _db.execute(
            """SELECT id FROM sessions
               WHERE state='ACTIVE' AND expiry_dt<>'' AND expiry_dt<?""",
            (now_ist_text,),
        ).fetchall()
    ]
    for session_id in expired_ids:
        _db.execute(
            """UPDATE sessions SET state='EXPIRED_UNRECONCILED',
                       sq_type='CONTRACT_EXPIRED_OFFLINE',end_dt=?
               WHERE id=? AND state='ACTIVE'""",
            (_ts(), session_id),
        )
        _db.execute(
            """UPDATE orders SET status='EXPIRED',
                       cancel_reason='CONTRACT_EXPIRED_OFFLINE',updated_at=?
               WHERE session_id=? AND status='NEW'""",
            (_ts(), session_id),
        )
        _db.execute(
            "INSERT INTO events(session_id,ts,event_type,detail) VALUES (?,?,?,?)",
            (session_id, _ts(), "SESSION_EXPIRED_UNRECONCILED",
             "Startup expiry reconciliation; no historical mark fabricated"),
        )
    _db.commit()


def _db_load_config():
    """Read all trading parameters from config table and override module globals."""
    global TRADE_QTY, MAX_TOTAL_MARK, MIN_STRIKE_GAP, MAX_PREMIUM_GAP
    global OPTIONS_RECOVERY_PCT, FUTURES_TP_MULTIPLIER
    global WINDOW_START, WINDOW_END, SQUAREOFF_START, SQUAREOFF_HARD
    global FUTURES_ENTRY_CUTOFF, FUTURES_SQUAREOFF
    global PAPER_WALLET_USDT, DASHBOARD_PASSWORD, MIN_EXPIRY_HOURS
    global _paper_wallet_balance
    if not _db:
        return
    try:
        rows = {r["key"]: r["value"]
                for r in _db.execute("SELECT key, value FROM config").fetchall()}
        def _f(k):
            try: return float(rows[k]) if k in rows and rows[k] not in ("", None) else None
            except (ValueError, TypeError): return None
        def _t(k):
            try:
                if k not in rows or not rows[k]: return None
                parts = str(rows[k]).split(":")
                return int(parts[0]), int(parts[1])
            except Exception: return None
        if _f("TRADE_QTY")        is not None: TRADE_QTY        = _f("TRADE_QTY")
        if _f("MAX_TOTAL_MARK")    is not None: MAX_TOTAL_MARK    = _f("MAX_TOTAL_MARK")
        if _f("MIN_STRIKE_GAP")   is not None: MIN_STRIKE_GAP   = _f("MIN_STRIKE_GAP")
        if _f("MIN_EXPIRY_HOURS") is not None: MIN_EXPIRY_HOURS = _f("MIN_EXPIRY_HOURS")
        if _f("MAX_PREMIUM_GAP") is not None: MAX_PREMIUM_GAP = _f("MAX_PREMIUM_GAP")
        if _f("OPTIONS_RECOVERY_PCT") is not None: OPTIONS_RECOVERY_PCT = _f("OPTIONS_RECOVERY_PCT")
        if _f("FUTURES_TP_MULTIPLIER") is not None: FUTURES_TP_MULTIPLIER = _f("FUTURES_TP_MULTIPLIER")
        if _f("PAPER_WALLET_USDT") is not None: PAPER_WALLET_USDT = _f("PAPER_WALLET_USDT")
        if "DASHBOARD_PASSWORD" in rows: DASHBOARD_PASSWORD = rows["DASHBOARD_PASSWORD"]
        if _t("WINDOW_START")     is not None: WINDOW_START     = _t("WINDOW_START")
        if _t("WINDOW_END")       is not None: WINDOW_END       = _t("WINDOW_END")
        if _t("SQ_START")         is not None: SQUAREOFF_START  = _t("SQ_START")
        if _t("SQ_END")           is not None: SQUAREOFF_HARD   = _t("SQ_END")
        if _t("FUTURES_ENTRY_CUTOFF") is not None: FUTURES_ENTRY_CUTOFF = _t("FUTURES_ENTRY_CUTOFF")
        if _t("FUTURES_SQUAREOFF") is not None: FUTURES_SQUAREOFF = _t("FUTURES_SQUAREOFF")
        # Load running paper wallet balance (persisted across sessions)
        _wb = rows.get("PAPER_WALLET_BALANCE")
        if _wb not in (None, ""):
            try:
                _paper_wallet_balance = float(_wb)
            except ValueError:
                pass
        # Seed wallet_ledger with RESET entry if this is a fresh DB (no ledger rows)
        ledger_count = _db.execute("SELECT COUNT(*) FROM wallet_ledger").fetchone()[0]
        if ledger_count == 0:
            _db.execute(
                "INSERT INTO wallet_ledger (ts,session_id,type,amount,balance_after,note) "
                "VALUES (?,0,'RESET',?,?,?)",
                (_ts(), round(_paper_wallet_balance, 4), round(_paper_wallet_balance, 4),
                 "Initial wallet balance")
            )
            _db.commit()
        log("SYS", f"Config loaded from DB  wallet={_paper_wallet_balance:.2f}", "OK")
    except Exception as e:
        log("SYS", f"Config load error (using hardcoded defaults): {e}", "WARN")

def _db_get_config(key: str, default: Optional[str] = None) -> Optional[str]:
    """Read a single value from the config table."""
    if not _db:
        return default
    try:
        row = _db.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return row[0] if row and row[0] is not None else default
    except Exception:
        return default

def _db_set_config(key: str, value: str):
    """Write a single value to the config table (upsert)."""
    if not _db:
        return
    try:
        _db.execute("INSERT OR REPLACE INTO config (key,value) VALUES (?,?)", (key, value))
        _db.commit()
    except Exception as e:
        log("SYS", f"_db_set_config({key}): {e}", "WARN")

def _db_session_create(expiry_sym: str, expiry_dt: str) -> int:
    if not _db:
        return 0
    cur = _db.execute(
        "INSERT INTO sessions (date,expiry_sym,expiry_dt,state,start_dt) VALUES (?,?,?,?,?)",
        (datetime.now(IST).strftime("%Y-%m-%d"), expiry_sym, expiry_dt, "ACTIVE", _ts())
    )
    _db.commit()
    sid = cur.lastrowid
    try:
        from straddle_bot.trading.ingest import upsert_session
        upsert_session({
            "legacy_id": sid,
            "session_id": f"STR-SESSION-{sid}",
            "session_date": datetime.now(IST).strftime("%Y-%m-%d"),
            "expiry_sym": expiry_sym,
            "expiry_dt": expiry_dt,
            "state": "ACTIVE",
            "start_dt": _ts(),
        })
    except Exception:
        pass
    return sid

def _db_session_update(session_id: int, **kwargs):
    if not _db or not session_id:
        return
    cols = ", ".join(f"{k}=?" for k in kwargs)
    _db.execute(f"UPDATE sessions SET {cols} WHERE id=?", list(kwargs.values()) + [session_id])
    _db.commit()
    try:
        from straddle_bot.trading.ingest import upsert_session
        upsert_session({
            "legacy_id": session_id,
            "session_id": f"STR-SESSION-{session_id}",
            **kwargs
        })
    except Exception:
        pass

def _db_order_insert(session_id: int, paper_order_id: int, symbol: str, asset_type: str,
                     leg_label: str, side: str, order_type: str, qty: float,
                     limit_price: float = 0, fill_price: float = 0,
                     status: str = "NEW", filled_at: str = None,
                     cancel_reason: str = None) -> int:
    if not _db:
        return 0
    cur = _db.execute(
        "INSERT INTO orders (session_id,paper_order_id,symbol,asset_type,leg_label,"
        "side,order_type,qty,limit_price,fill_price,status,placed_at,filled_at,"
        "cancel_reason,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, paper_order_id, symbol, asset_type, leg_label, side, order_type,
         qty, limit_price, fill_price, status, _ts(), filled_at, cancel_reason, _ts())
    )
    _db.commit()
    oid = cur.lastrowid
    try:
        from straddle_bot.trading.ingest import record_order
        record_order({
            "legacy_id": oid,
            "session": f"STR-SESSION-{session_id}" if session_id else None,
            "paper_order_id": str(paper_order_id),
            "symbol": symbol,
            "asset_type": asset_type,
            "leg_label": leg_label,
            "side": side,
            "order_type": order_type,
            "qty": qty,
            "limit_price": limit_price,
            "fill_price": fill_price,
            "status": status,
            "placed_at": _ts(),
            "filled_at": filled_at,
            "cancel_reason": cancel_reason,
        })
    except Exception:
        pass
    return oid


def _db_order_cancel(session_id: int, leg_label: str, cancel_reason: str):
    """Mark all NEW orders for a leg as CANCELLED."""
    if not _db or not session_id:
        return
    _db.execute(
        "UPDATE orders SET status='CANCELLED', cancel_reason=?, updated_at=? "
        "WHERE session_id=? AND leg_label=? AND status='NEW'",
        (cancel_reason, _ts(), session_id, leg_label)
    )
    _db.commit()

def _db_snapshot_insert(session_id: int, btc_mark: float, call_mark: float, put_mark: float,
                        call_upnl: float, put_upnl: float, long_upnl: float, short_upnl: float,
                        long_liq: float, short_liq: float, margin_used: float):
    if not _db or not session_id:
        return
    total = call_upnl + put_upnl + long_upnl + short_upnl
    _db.execute(
        "INSERT INTO pnl_snapshots (session_id,ts,btc_mark,call_mark,put_mark,"
        "call_upnl,put_upnl,long_upnl,short_upnl,total_upnl,long_liq,short_liq,margin_used) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, _ts(), btc_mark, call_mark, put_mark,
         call_upnl, put_upnl, long_upnl, short_upnl, total,
         long_liq, short_liq, margin_used)
    )
    # Keep last 2000 rows per session (~16h at 30s interval — covers full overnight session)
    _db.commit()


def _db_fill_insert(session_id: int, instrument: str, side: str,
                   qty: float, mark_at_order: float, fill_price: float,
                   order_id: str = "", note: str = ""):
    """Record a fill against its fair mark-price accounting baseline."""
    if not _db or not session_id:
        return
    slippage = round(fill_price - mark_at_order, 4) if mark_at_order else 0.0
    try:
        _db.execute(
            "INSERT INTO fills (session_id,ts,instrument,side,qty,"
            "ask_at_order,fill_price,slippage,order_id,note) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (session_id, _ts(), instrument, side, qty,
             mark_at_order, fill_price, slippage, str(order_id), note)
        )
        _db.commit()
    except Exception:
        pass


def _db_atomic_future_entry_fill(session_id: int, leg: str, qty: float,
                                 fill_price: float, tp_price: float,
                                 liq_price: float, margin_used: float):
    """Persist order fill, position state, fill audit and event atomically."""
    if not _db or not session_id:
        raise RuntimeError("DB unavailable for atomic futures fill")
    leg = leg.upper()
    if leg not in ("LONG", "SHORT"):
        raise ValueError(f"Unsupported futures leg: {leg}")
    now = _ts()
    entry_col = "long_entry" if leg == "LONG" else "short_entry"
    qty_col = "long_qty" if leg == "LONG" else "short_qty"
    tp_col = "long_tp_px" if leg == "LONG" else "short_tp_px"
    liq_col = "long_liq_px" if leg == "LONG" else "short_liq_px"
    opposite = "SHORT" if leg == "LONG" else "LONG"
    side = "BUY" if leg == "LONG" else "SELL"
    with _db:
        cur = _db.execute(
            f"""UPDATE sessions SET {entry_col}=?,{qty_col}=?,{tp_col}=?,
                       {liq_col}=?,margin_used=?
                WHERE id=? AND state='ACTIVE'""",
            (fill_price, qty, tp_price, liq_price, margin_used, session_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"Session {session_id} is not ACTIVE")
        _db.execute(
            """UPDATE orders SET status='FILLED',fill_price=?,filled_at=?,updated_at=?
               WHERE session_id=? AND leg_label=? AND status='NEW'""",
            (fill_price, now, now, session_id, f"{leg}_ENTRY"),
        )
        _db.execute(
            """UPDATE orders SET status='CANCELLED',
                       cancel_reason='OPPOSITE_FILLED',updated_at=?
               WHERE session_id=? AND leg_label IN (?,?) AND status='NEW'""",
            (now, session_id, f"{opposite}_ENTRY", f"{opposite}_TP"),
        )
        _db.execute(
            """INSERT INTO fills(session_id,ts,instrument,side,qty,ask_at_order,
                                 fill_price,slippage,order_id,note)
               SELECT ?,?,?,?,?,?,?,0,COALESCE(CAST(paper_order_id AS TEXT),''),
                      'atomic_future_entry'
               FROM orders WHERE session_id=? AND leg_label=? LIMIT 1""",
            (session_id, now, f"{leg}_FUT", side, qty, fill_price,
             fill_price, session_id, f"{leg}_ENTRY"),
        )
        _db.execute(
            "INSERT INTO events(session_id,ts,event_type,detail) VALUES (?,?,?,?)",
            (session_id, now, f"{leg}_FILLED",
             f"entry={fill_price:.2f} tp={tp_price:.2f} atomic=1"),
        )


def _db_atomic_future_tp_fill(session_id: int, leg: str, qty: float,
                              exit_price: float, pnl: float):
    """Commit TP order, fill audit, exit basis and event together."""
    if not _db or not session_id:
        raise RuntimeError("DB unavailable for atomic futures TP")
    leg = leg.upper()
    exit_col = "sq_long_exit" if leg == "LONG" else "sq_short_exit"
    side = "SELL" if leg == "LONG" else "BUY"
    now = _ts()
    with _db:
        cur = _db.execute(
            f"UPDATE sessions SET {exit_col}=? WHERE id=? AND state='ACTIVE'",
            (exit_price, session_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"Session {session_id} is not ACTIVE")
        _db.execute(
            """UPDATE orders SET status='FILLED',fill_price=?,filled_at=?,updated_at=?
               WHERE session_id=? AND leg_label=? AND status='NEW'""",
            (exit_price, now, now, session_id, f"{leg}_TP"),
        )
        _db.execute(
            """INSERT INTO fills(session_id,ts,instrument,side,qty,ask_at_order,
                                 fill_price,slippage,order_id,note)
               SELECT ?,?,?,?,?,?,?,0,COALESCE(CAST(paper_order_id AS TEXT),''),
                      'atomic_future_tp'
               FROM orders WHERE session_id=? AND leg_label=? LIMIT 1""",
            (session_id, now, f"{leg}_FUT", side, qty, exit_price,
             exit_price, session_id, f"{leg}_TP"),
        )
        _db.execute(
            "INSERT INTO events(session_id,ts,event_type,detail) VALUES (?,?,?,?)",
            (session_id, now, f"{leg}_TP_HIT",
             f"exit={exit_price:.2f} pnl={pnl:.4f} atomic=1"),
        )


def _db_reconcile_records() -> list:
    """Independent orders/fills/session/wallet consistency audit."""
    if not _db:
        return [{"type": "db_unavailable"}]
    issues = []
    active = _db.execute(
        "SELECT * FROM sessions WHERE state='ACTIVE' ORDER BY id"
    ).fetchall()
    for session in active:
        sid = int(session["id"])
        for leg, entry_col, qty_col in (
            ("LONG_ENTRY", "long_entry", "long_qty"),
            ("SHORT_ENTRY", "short_entry", "short_qty"),
        ):
            order = _db.execute(
                """SELECT fill_price,qty FROM orders
                   WHERE session_id=? AND leg_label=? AND status='FILLED'
                   ORDER BY id DESC LIMIT 1""",
                (sid, leg),
            ).fetchone()
            if order and (
                abs(float(session[entry_col] or 0) - float(order["fill_price"] or 0)) > 0.01
                or abs(float(session[qty_col] or 0) - float(order["qty"] or 0)) > 1e-8
            ):
                issues.append({
                    "type": "position_order_mismatch", "session_id": sid,
                    "leg": leg, "session_entry": session[entry_col],
                    "session_qty": session[qty_col],
                    "order_fill": order["fill_price"], "order_qty": order["qty"],
                })
        missing_fills = _db.execute(
            """SELECT o.leg_label FROM orders o
               WHERE o.session_id=? AND o.status='FILLED'
                 AND o.leg_label IN ('LONG_ENTRY','SHORT_ENTRY','LONG_TP','SHORT_TP')
                 AND NOT EXISTS (
                   SELECT 1 FROM fills f WHERE f.session_id=o.session_id
                     AND f.order_id=CAST(o.paper_order_id AS TEXT)
                 )""",
            (sid,),
        ).fetchall()
        if missing_fills:
            issues.append({
                "type": "missing_fill_audit", "session_id": sid,
                "legs": [row[0] for row in missing_fills],
            })
    ledger = _db.execute(
        "SELECT balance_after FROM wallet_ledger ORDER BY id DESC LIMIT 1"
    ).fetchone()
    configured = _db.execute(
        "SELECT value FROM config WHERE key='PAPER_WALLET_BALANCE'"
    ).fetchone()
    if ledger and configured and abs(float(ledger[0]) - float(configured[0])) > 0.01:
        issues.append({
            "type": "wallet_mismatch",
            "ledger": float(ledger[0]), "config": float(configured[0]),
        })
    return issues

def _db_event_insert(session_id: int, event_type: str, detail: str = ""):
    if not _db:
        return
    _db.execute(
        "INSERT INTO events (session_id,ts,event_type,detail) VALUES (?,?,?,?)",
        (session_id, _ts(), event_type, detail)
    )
    _db.commit()


def _db_wallet_ledger(session_id: int, tx_type: str, amount: float,
                      balance_after: float, note: str = ""):
    """Record a signed wallet movement. amount < 0 = debit, amount > 0 = credit."""
    if not _db:
        return
    _db.execute(
        "INSERT INTO wallet_ledger (ts,session_id,type,amount,balance_after,note) "
        "VALUES (?,?,?,?,?,?)",
        (_ts(), session_id, tx_type, round(amount, 4), round(balance_after, 4), note)
    )
    _db.commit()
    try:
        from straddle_bot.trading.ingest import record_wallet_ledger_entry
        record_wallet_ledger_entry({
            "session": f"STR-SESSION-{session_id}" if session_id else None,
            "entry_type": tx_type,
            "amount": round(amount, 4),
            "balance_after": round(balance_after, 4),
            "note": note,
            "ts": _ts(),
        })
    except Exception:
        pass


# ╔╗
# â•'                        BINANCE REST  (HMAC-SHA256)                        â•'
# ╚
def _sign(params: dict) -> str:
    qs  = urllib.parse.urlencode(params)
    sig = hmac.new(BINANCE_SECRET_KEY.encode(), qs.encode(), hashlib.sha256).hexdigest()
    return qs + "&signature=" + sig

_HDR = {"X-MBX-APIKEY": BINANCE_API_KEY,
        "Content-Type": "application/x-www-form-urlencoded"}

def _parse_418_ban(err_str: str) -> float:
    """Return Unix seconds when the Binance EAPI IP ban expires (from the 418 error body)."""
    m = re.search(r'banned until (\d+)', err_str)
    if m:
        ts = int(m.group(1))
        return ts / 1000.0 if ts > 1e12 else float(ts)  # >1e12 = milliseconds -> convert
    return time.time() + 120   # default 2-min backoff if timestamp not parseable

def _is_rate_limited(err: Exception) -> bool:
    s = str(err).lower()
    return "418" in s or "banned" in s

async def _post(base: str, path: str, params: dict) -> dict:
    params["timestamp"] = int(time.time() * 1000)
    params.setdefault("recvWindow", 5000)
    body = _sign(params).encode()
    def _do():
        import urllib.error
        req = urllib.request.Request(f"{base}{path}", data=body,
                                     headers=_HDR, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:    eb = e.read().decode()
            except: eb = "(unreadable)"
            raise RuntimeError(f"HTTP {e.code} {e.reason}  -  {eb}") from e
    return await asyncio.get_running_loop().run_in_executor(None, _do)

async def _delete(base: str, path: str, params: dict) -> dict:
    params["timestamp"] = int(time.time() * 1000)
    params.setdefault("recvWindow", 5000)
    qs = _sign(params)
    def _do():
        import urllib.error
        req = urllib.request.Request(f"{base}{path}?{qs}",
                                     headers={"X-MBX-APIKEY": BINANCE_API_KEY},
                                     method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:    eb = e.read().decode()
            except: eb = "(unreadable)"
            raise RuntimeError(f"HTTP {e.code}  -  {eb}") from e
    return await asyncio.get_running_loop().run_in_executor(None, _do)

async def _get(base: str, path: str, params: dict = None) -> Any:
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000)
    p.setdefault("recvWindow", 5000)
    qs = _sign(p)
    def _do():
        import urllib.error
        req = urllib.request.Request(f"{base}{path}?{qs}",
                                     headers={"X-MBX-APIKEY": BINANCE_API_KEY})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:    eb = e.read().decode()
            except: eb = "(unreadable)"
            raise RuntimeError(f"HTTP {e.code}  -  {eb}") from e
    return await asyncio.get_running_loop().run_in_executor(None, _do)


#
#  PAPER TRADING STATE
#
_paper_orders:    Dict[int, dict] = {}
_paper_oid_seq:   int             = 1000
_last_valid_mark: float           = 0.0   # last non-zero BTC mark price
_last_valid_mark_ts: float        = 0.0   # epoch time when _last_valid_mark was last set
_last_valid_opt:  Dict[str, float] = {}   # sym  last non-zero option mark
_last_valid_opt_ts: Dict[str, float] = {} # exact sym -> mark fetch epoch
_paper_wallet_balance: float      = PAPER_WALLET_USDT  # tracks deductions during session

def _paper_next_oid() -> int:
    global _paper_oid_seq
    _paper_oid_seq += 1
    return _paper_oid_seq

def _calc_liq_price_cross(entry: float, qty: float, side: str,
                          wallet: float = None) -> float:
    # Cross-margin 100x liq price. Returns 0 if no meaningful liq risk.
    if entry <= 0 or qty <= 0:
        return 0.0
    wb  = wallet if wallet is not None else _paper_wallet_balance
    mm  = FUTURES_MM_RATE
    if side == "LONG":
        denom = qty * (1.0 - mm)
        liq   = (entry * qty - wb) / denom if denom else 0.0
    else:
        denom = qty * (1.0 + mm)
        liq   = (wb + entry * qty) / denom if denom else 0.0
    return round(max(0.0, liq), 1)

def _opt_upnl(fill: float, mark: float, qty: float) -> float:
    # Options unrealized PnL: (mark - fill) * qty
    if fill <= 0 or mark <= 0 or qty <= 0:
        return 0.0
    return round((mark - fill) * qty, 4)

def _fut_upnl(entry: float, mark: float, qty: float, side: str) -> float:
    # Futures unrealized PnL: LONG = (mark-entry)*qty, SHORT = (entry-mark)*qty
    if entry <= 0 or mark <= 0 or qty <= 0:
        return 0.0
    pnl = (mark - entry) * qty if side == "LONG" else (entry - mark) * qty
    return round(pnl, 4)

# ╔╗
# â•'                        ORDER HELPERS                                      â•'
# ╚
def _opt_tick(price: float) -> int:
    """Round to nearest 5 USDT tick  -  Binance EAPI requirement for ALL premium levels."""
    return max(5, int(round(price / 5)) * 5)

async def option_order(sym: str, side: str, qty: float,
                       price: float = 0, order_type: str = "LIMIT",
                       tif: str = "FOK") -> dict:
    if PAPER_TRADE:
        oid = _paper_next_oid()
        ticked = _opt_tick(price) if price else 0
        status, fill_px = "NEW", 0.0
        if order_type == "LIMIT" and tif == "FOK":
            data   = _chain.get(sym, {})
            mark = float(data.get("mark", 0.0) or 0.0)
            if mark > 0:
                status, fill_px = "FILLED", mark
            else:
                status = "EXPIRED"
        elif order_type == "MARKET":
            data = _chain.get(sym, {})
            fill_px = float(data.get("mark") or 0)
            status = "FILLED" if fill_px > 0 else "EXPIRED"
        _paper_orders[oid] = {
            "orderId": oid, "symbol": sym, "side": side,
            "type": order_type, "tif": tif, "qty": qty,
            "price": ticked, "status": status, "fill_price": fill_px,
            "asset": "option", "pos_side": None, "placed_at": time.time(),
        }
        return {"orderId": oid, "status": status,
                "avgPrice": str(fill_px), "executedQty": str(qty if status == "FILLED" else 0)}
    p: dict = {"symbol": sym, "side": side, "type": order_type,
               "quantity": f"{qty:.2f}"}
    if order_type == "LIMIT":
        ticked = _opt_tick(price)
        p["price"]       = str(ticked) if price >= 10 else f"{ticked:.2f}"
        p["timeInForce"] = tif
    return await _post(EAPI, "/eapi/v1/order", p)

async def option_cancel(sym: str, order_id: int) -> dict:
    if PAPER_TRADE:
        if order_id in _paper_orders:
            _paper_orders[order_id]["status"] = "CANCELLED"
        return {}
    return await _delete(EAPI, "/eapi/v1/order",
                         {"symbol": sym, "orderId": order_id})

async def option_query(sym: str, order_id: int) -> dict:
    if PAPER_TRADE:
        o = _paper_orders.get(order_id, {})
        return {"status": o.get("status", "NEW"),
                "avgPrice": str(o.get("fill_price", 0)),
                "executedQty": str(o.get("qty", 0))}
    return await _get(EAPI, "/eapi/v1/order", {"symbol": sym, "orderId": order_id})

async def futures_limit(side: str, qty: float, price: float,
                        pos_side: str = "LONG") -> dict:
    if PAPER_TRADE:
        oid = _paper_next_oid()
        _paper_orders[oid] = {
            "orderId": oid, "symbol": "BTCUSDT", "side": side,
            "type": "LIMIT", "qty": qty, "price": round(price, 1),
            "status": "NEW", "fill_price": 0.0,
            "asset": "future", "pos_side": pos_side, "placed_at": time.time(),
        }
        return {"orderId": oid, "status": "NEW"}
    p = {"symbol": "BTCUSDT", "side": side, "type": "LIMIT",
         "quantity": round(qty, 3), "price": round(price, 1),
         "timeInForce": "GTC", "positionSide": pos_side}
    return await _post(FAPI, "/fapi/v1/order", p)

async def futures_market(side: str, qty: float, pos_side: str = "LONG") -> dict:
    if PAPER_TRADE:
        mark = _price() or _last_valid_mark or 0
        oid  = _paper_next_oid()
        _paper_orders[oid] = {
            "orderId": oid, "symbol": "BTCUSDT", "side": side,
            "type": "MARKET", "qty": qty, "price": mark,
            "status": "FILLED", "fill_price": mark,
            "asset": "future", "pos_side": pos_side, "placed_at": time.time(),
        }
        return {"orderId": oid, "status": "FILLED",
                "avgPrice": str(mark), "executedQty": str(qty)}
    p = {"symbol": "BTCUSDT", "side": side, "type": "MARKET",
         "quantity": round(qty, 3), "positionSide": pos_side}
    return await _post(FAPI, "/fapi/v1/order", p)

async def futures_cancel(order_id: int) -> dict:
    if PAPER_TRADE:
        if order_id in _paper_orders:
            _paper_orders[order_id]["status"] = "CANCELLED"
        return {}
    return await _delete(FAPI, "/fapi/v1/order",
                         {"symbol": "BTCUSDT", "orderId": order_id})

async def futures_query(order_id: int) -> dict:
    if PAPER_TRADE:
        o = _paper_orders.get(order_id, {})
        return {"status": o.get("status", "NEW"),
                "avgPrice": str(o.get("fill_price", 0)),
                "executedQty": str(o.get("qty", 0))}
    return await _get(FAPI, "/fapi/v1/order", {"symbol": "BTCUSDT", "orderId": order_id})


# ╔╗
# â•'                        SHARED PRICE STATE                                 â•'
# ╚
_fut_px: dict            = {"bid": 0.0, "ask": 0.0, "last": 0.0, "mark": 0.0}
_fut_px_update_ms: float = 0.0          # epoch-ms of last futures price update
_chain:  Dict[str, dict] = {}           # sym -> {bid, ask, bid_qty, ask_qty, mark, intrinsic, *_ms}
_chain_ms: Dict[str, Dict[str, int]] = {}  # sym -> {field -> epoch_ms}  LKG timestamps
_all_strikes: List[float] = []
_sym_meta:    Dict[str, dict] = {}      # sym -> {symbol, strike, side, expiry_ms}
_expiry_6d    = ""
_expiry_ms    = 0
_ws_last_msg: Dict[str, float] = {"fut": time.time(), "opt": time.time()}
_last_chain_refresh = 0.0
_ws_opt_fail_count  = 0   # consecutive options WS failures; resets on success
_rest_banned_until  = 0.0  # Unix time: stop all EAPI REST calls while time() < this
_rest_last_ok:  float = 0.0   # epoch time of last successful REST ticker fetch
_rest_fail_streak: int = 0    # consecutive REST failures (resets on success)
_ws_mark_time: Dict[str, float] = {}  # sym -> last time mark price arrived from WS ticker

# ── LKG helpers ──────────────────────────────────────────────────────────────
_LKG_FIELDS = ("bid", "ask", "bid_qty", "ask_qty", "mark")

def _chain_set(sym: str, field: str, value: float):
    """Update chain field only when value > 0 (Last-Known-Good pattern)."""
    if value <= 0:
        return
    if sym not in _chain:
        _chain[sym] = {}
    _chain[sym][field] = round(value, 2)
    if sym not in _chain_ms:
        _chain_ms[sym] = {}
    _chain_ms[sym][field] = int(time.time() * 1000)

def _chain_get(sym: str, field: str, max_age_ms: int = 15_000) -> Optional[float]:
    """Return chain field value if fresh, None if stale or missing."""
    val = _chain.get(sym, {}).get(field)
    if not val:
        return None
    age = int(time.time() * 1000) - _chain_ms.get(sym, {}).get(field, 0)
    return val if age < max_age_ms else None

def _feed_age_s(feed: str) -> float:
    """Seconds since the last successful futures/options API refresh."""
    return time.time() - _ws_last_msg.get(feed, 0)

def _price() -> float:
    mark = float(_fut_px.get("mark") or 0.0)
    age_ms = time.time() * 1000 - _fut_px_update_ms
    return mark if mark > 0 and 0 <= age_ms <= 15_000 else 0.0

def _price_safe() -> float:
    """_price() with time-bounded _last_valid_mark fallback (max 30s stale)."""
    p = _price()
    if p:
        return p
    if time.time() - _last_valid_mark_ts < 30 and _last_valid_mark > 0:
        return _last_valid_mark
    return 0.0

def _exact_option_mark(sym: str, max_age_s: float = 15.0) -> float:
    """Fresh mark for the exact persisted option contract; never another expiry."""
    if not sym:
        return 0.0
    fresh = _chain_get(sym, "mark", int(max_age_s * 1000))
    if fresh:
        return float(fresh)
    mark = float(_last_valid_opt.get(sym, 0.0) or 0.0)
    ts = float(_last_valid_opt_ts.get(sym, 0.0) or 0.0)
    return mark if mark > 0 and ts > 0 and time.time() - ts <= max_age_s else 0.0

def _sym(strike: float, side: str) -> str:
    return f"BTC-{_expiry_6d}-{int(strike)}-{side}"

def _intr(strike: float, spot: float, side: str) -> float:
    return max(spot - strike, 0.0) if side == "C" else max(strike - spot, 0.0)

def _to_min(h: int, m: int) -> int:
    return h * 60 + m

def _in_entry_window(now_min: int) -> bool:
    """True when current time is inside the entry window. Handles overnight windows correctly."""
    start = _to_min(*WINDOW_START)
    end   = _to_min(*WINDOW_END)
    if start <= end:                         # same-day  e.g. 09:00 -> 13:00
        return start <= now_min < end
    else:                                    # overnight e.g. 23:00 -> 05:00
        return now_min >= start or now_min < end

def _is_expiry_day() -> bool:
    """True only on the calendar day the current options expiry falls on."""
    if not _expiry_ms:
        return False
    return datetime.fromtimestamp(_expiry_ms / 1000, tz=IST).date() == datetime.now(IST).date()

def _expiry_iso() -> str:
    """ISO date string (YYYY-MM-DD) of the current expiry  -  used as session key."""
    if not _expiry_ms:
        return ""
    return datetime.fromtimestamp(_expiry_ms / 1000, tz=IST).date().isoformat()


# ╔╗
# â•'                        OPTIONS CHAIN                                      â•'
# ╚
async def _fetch_chain():
    """Load/refresh full options chain from exchangeInfo. Preserves existing price data."""
    global _all_strikes, _sym_meta, _expiry_6d, _expiry_ms, _chain, _last_chain_refresh
    _last_chain_refresh = time.time()
    old_strikes = set(_all_strikes)
    try:
        def _do():
            import urllib.error
            req = urllib.request.Request(f"{EAPI}/eapi/v1/exchangeInfo",
                                         headers={"User-Agent": "StraddleBot/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                try:    eb = e.read().decode()
                except: eb = ""
                raise RuntimeError(f"HTTP {e.code}  -  {eb}") from e
        data    = await asyncio.get_running_loop().run_in_executor(None, _do)
        now_ms  = int(time.time() * 1000)
        now_ist = datetime.now(IST)
        btc     = [s for s in data.get("optionSymbols", [])
                   if s.get("underlying") == "BTCUSDT"
                   and s.get("status") == "TRADING"
                   and s.get("expiryDate", 0) > now_ms]
        if not btc:
            raise ValueError("No active BTC option symbols")
        by_exp = {}
        for s in btc:
            by_exp.setdefault(s["expiryDate"], []).append(s)
        today_ts = next((ts for ts in sorted(by_exp)
                         if datetime.fromtimestamp(ts/1000, tz=IST).date() == now_ist.date()),
                        None)
        ts_use   = today_ts or sorted(by_exp.keys())[0]
        raw      = by_exp[ts_use]
        exp_dt   = datetime.fromtimestamp(ts_use/1000, tz=IST)
        _expiry_6d   = exp_dt.strftime("%y%m%d")
        _expiry_ms   = ts_use
        strikes      = sorted(set(float(s["strikePrice"]) for s in raw))
        _all_strikes = strikes
        meta = {}
        for s in raw:
            sc = "C" if s.get("side", "CALL") == "CALL" else "P"
            meta[s["symbol"]] = {"symbol": s["symbol"],
                                 "strike": float(s["strikePrice"]),
                                 "side":   sc,
                                 "expiry_ms": ts_use,
                                 "min_qty":  float(s.get("minQty", 0.01))}
        # Build new chain  -  preserve existing price data for known symbols
        new_chain: Dict[str, dict] = {}
        for k, v in meta.items():
            ex = _chain.get(k, {})
            new_chain[k] = {
                "symbol":    k,
                "strike":    v["strike"],
                "side":      v["side"],
                "bid":       ex.get("bid", 0.0),
                "ask":       ex.get("ask", 0.0),
                "bid_qty":   ex.get("bid_qty", 0.0),
                "ask_qty":   ex.get("ask_qty", 0.0),
                "mark":      ex.get("mark", 0.0),
                "intrinsic": ex.get("intrinsic", 0.0),
            }
        _sym_meta = meta
        _chain    = new_chain
        new_syms  = sorted(set(strikes) - old_strikes)
        if new_syms and old_strikes:
            log("SYS", f"New strikes: +{len(new_syms)} -> {[int(s) for s in new_syms]}", "WARN")
        log("SYS", f"Chain loaded: {exp_dt.strftime('%d %b %Y %H:%M IST')} | "
                   f"{len(strikes)} strikes", "OK")
    except Exception as e:
        global _rest_banned_until
        err_s = str(e)
        if "429" in err_s or "Too Many Requests" in err_s:
            _rest_banned_until = time.time() + 60
            log("SYS", "exchangeInfo HTTP 429  -  rate limited. Backing off 60s.", "WARN")
        elif _is_rate_limited(e):
            _rest_banned_until = _parse_418_ban(err_s)
            remain = max(0, int(_rest_banned_until - time.time()))
            log("SYS", f"exchangeInfo HTTP 418  -  EAPI REST paused for {remain}s", "WARN")
        else:
            log("SYS", f"exchangeInfo failed: {e}", "WARN")


async def _chain_refresh_loop():
    """Re-fetch chain on startup and every 10 minutes.
    Fetch-first pattern ensures _expiry_6d is set before the entry window opens.
    Interval reduced from 15m so session boundary transitions are caught faster."""
    await asyncio.sleep(30)   # brief startup delay — WS feeds need time to connect first
    while True:
        await _fetch_chain()
        await asyncio.sleep(10 * 60)


# ╔╗
# â•'                        ITM PAIR LOGIC                                     â•'
# ╚
def _find_itm_pair(spot: float) -> Optional[Tuple[dict, dict]]:
    """Return CALL and PUT at the single strike nearest to spot."""
    if not _all_strikes or not _expiry_6d:
        return None
    strike = min(_all_strikes, key=lambda value: (abs(value - spot), value))
    call_sym = _sym(strike, "C")
    put_sym  = _sym(strike, "P")
    call_opt = _chain.get(call_sym)
    put_opt  = _chain.get(put_sym)
    if call_opt is None or put_opt is None:
        return None
    # Return shallow copies so we don't mutate shared state
    return dict(call_opt), dict(put_opt)


def _check_conditions(call_opt: dict, put_opt: dict) -> Tuple[bool, str]:
    """Validate the setup using option mark prices only."""
    now_ms = int(time.time() * 1000)
    if _expiry_ms:
        hours_left = (_expiry_ms - now_ms) / (3600 * 1000)
        if hours_left < MIN_EXPIRY_HOURS:
            return False, f"expiry too soon ({hours_left:.1f}h left)"

    marks: Dict[str, float] = {}
    for leg, sym in (("CALL", call_opt["symbol"]), ("PUT", put_opt["symbol"])):
        mark = _chain_get(sym, "mark", max_age_ms=20_000)
        if mark is None:
            return False, f"{leg} mark stale/unavailable"
        if mark <= 0:
            return False, f"{leg} mark is zero"
        marks[leg] = float(mark)

    total_mark = marks["CALL"] + marks["PUT"]
    if total_mark > MAX_TOTAL_MARK:
        return False, f"total mark {total_mark:.0f} > {MAX_TOTAL_MARK}"
    premium_gap = abs(marks["CALL"] - marks["PUT"])
    if premium_gap > MAX_PREMIUM_GAP:
        return False, f"premium gap {premium_gap:.0f} > {MAX_PREMIUM_GAP}"
    return True, ""


def _conditions_detail(call_opt: dict, put_opt: dict) -> str:
    """Mark-only entry conditions shown every scan cycle."""
    now_ms    = int(time.time() * 1000)
    c_mark    = call_opt.get("mark")    or 0.0
    p_mark    = put_opt.get("mark")     or 0.0
    total     = c_mark + p_mark
    gap       = put_opt["strike"] - call_opt["strike"]
    h_left    = (_expiry_ms - now_ms) / 3_600_000 if _expiry_ms else 0.0

    def ok(cond): return "OK" if cond else "NO"

    return (
        f"  {call_opt['symbol']}  <->  {put_opt['symbol']}\n"
        f"  expiry    {h_left:.1f}h              need >= {MIN_EXPIRY_HOURS:.0f}h       {ok(h_left >= MIN_EXPIRY_HOURS)}\n"
        f"  gap       {gap:.0f} USDT           need >= {MIN_STRIKE_GAP}      {ok(gap >= MIN_STRIKE_GAP)}\n"
        f"  CALL mark {c_mark:.0f}               need > 0             {ok(c_mark > 0)}\n"
        f"  PUT  mark {p_mark:.0f}               need > 0             {ok(p_mark > 0)}\n"
        f"  prem gap  {abs(c_mark-p_mark):.0f}               need <= {MAX_PREMIUM_GAP}   {ok(abs(c_mark-p_mark) <= MAX_PREMIUM_GAP)}\n"
        f"  total     {total:.0f}               need <= {MAX_TOTAL_MARK}   {ok(total <= MAX_TOTAL_MARK)}"
    )


# ╔╗
# â•'                        WEBSOCKET FEEDS                                    â•'
# ╚
async def _fut_ws():
    """Perpetual futures: mark price + book ticker -> _fut_px."""
    import websockets
    url = ("wss://fstream.binance.com/stream?streams="
           "btcusdt@bookTicker/btcusdt@markPrice@1s")
    _was_down = False
    while True:
        _ws_last_msg["fut"] = time.time()   # reset on every (re)connect so feed age stays fresh
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=15, ssl=True) as ws:
                if _was_down:
                    log("SYS", "Futures WS: reconnected", "OK")
                    asyncio.create_task(_tg("✅ <b>Futures WS reconnected</b>\nPrice feed restored."))
                    _was_down = False
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=6.0)
                    except asyncio.TimeoutError:
                        raise ConnectionError("Futures WS: no message in 6s — forcing reconnect")
                    global _fut_px_update_ms
                    _ws_last_msg["fut"] = time.time()
                    _fut_px_update_ms   = time.time() * 1000
                    msg = json.loads(raw)
                    st  = msg.get("stream", "")
                    d   = msg.get("data", msg)
                    if "bookTicker" in st:
                        bid = float(d.get("b") or 0)
                        ask = float(d.get("a") or 0)
                        if bid > 0: _fut_px["bid"] = bid
                        if ask > 0: _fut_px["ask"] = ask
                    elif "markPrice" in st:
                        mp = float(d.get("p") or 0)
                        if mp > 0:
                            _fut_px["mark"] = mp
                            globals()["_last_valid_mark"]    = mp
                            globals()["_last_valid_mark_ts"] = time.time()
                        else:
                            log("SYS", "BTC mark=0 from WS — keeping last valid", "WARN")
        except Exception as e:
            if not _was_down:
                log("SYS", f"Futures WS: {e}  -  retry 3s", "WARN")
                asyncio.create_task(_tg(
                    f"⚠ <b>Futures WS disconnected</b>\n"
                    f"Spot price feed lost  -  retrying...\n"
                    f"{type(e).__name__}: {e}"
                ))
            _was_down = True
            await asyncio.sleep(3)



async def _fapi_mark_fallback():
    """Primary futures REST API feed. Mark price is the sole fair price."""
    while True:
        await asyncio.sleep(1)
        try:
            data = await _get(FAPI, "/fapi/v1/premiumIndex", {"symbol": "BTCUSDT"})
            mark = float(data.get("markPrice", 0))
            if mark > 0:
                _fut_px["mark"]                       = mark
                globals()["_last_valid_mark"]          = mark
                globals()["_last_valid_mark_ts"]       = time.time()
                globals()["_fut_px_update_ms"]         = time.time() * 1000
                _ws_last_msg["fut"]                    = time.time()
        except Exception as _e:
            log("SYS", f"Futures mark API error: {_e}", "WARN")

async def _opt_ws():
    """
    Options WS via nbstream.binance.com/eoptions  -  per ITM CALL+PUT pair subscribes to:
      {sym}@depth10@100ms   -  best bid/ask price + qty at 100ms
      {sym}@ticker           -  mark price (mp field) at real-time
    Resubscribes live (no reconnect) when spot moves and ITM strikes shift.
    """
    import websockets
    global _chain, _ws_opt_fail_count, _ws_mark_time

    while not _all_strikes or not _expiry_6d:
        await asyncio.sleep(1)

    def _itm_streams() -> Tuple[List[str], List[str]]:
        spot = _price()
        if not spot:
            return [], []
        pair = _find_itm_pair(spot)
        if not pair:
            return [], []
        syms    = [pair[0]["symbol"], pair[1]["symbol"]]
        streams = []
        for s in syms:
            streams.append(f"{s.lower()}@depth10@100ms")
            streams.append(f"{s.lower()}@ticker")
            streams.append(f"{s.lower()}@markPrice")
        return streams, syms

    backoff = 2
    _req_id = 1
    while True:
        streams, syms = _itm_streams()
        while not streams:
            await asyncio.sleep(2)
            streams, syms = _itm_streams()

        url = f"wss://nbstream.binance.com/eoptions/stream?streams={'/'.join(streams)}"
        try:
            async with websockets.connect(url, ping_interval=None, ping_timeout=None, ssl=True) as ws:
                _was_reconnect      = _ws_opt_fail_count > 0
                _ws_opt_fail_count  = 0
                backoff = 2
                current_streams = set(streams)
                log("SYS",
                    f"Options WS connected | {syms[0]}  {syms[1]}  (depth+mark 100ms)", "OK")
                if _was_reconnect:
                    asyncio.create_task(_tg(
                        f"✅ <b>Options WS reconnected</b>\n"
                        f"Bid/ask/mark feed restored.\n"
                        f"{syms[0]}  |  {syms[1]}"
                    ))

                async for raw in ws:
                    _ws_last_msg["opt"] = time.time()
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    # Subscription responses have no "stream" key  -  check for errors
                    if "stream" not in msg:
                        if msg.get("error"):
                            log("SYS", f"Options WS subscription error: {msg['error']}", "WARN")
                        continue

                    # Live resubscribe when ITM strikes shift with spot
                    new_streams, _ = _itm_streams()
                    if new_streams and set(new_streams) != current_streams:
                        to_unsub = list(current_streams - set(new_streams))
                        to_sub   = [s for s in new_streams if s not in current_streams]
                        if to_unsub:
                            await ws.send(json.dumps(
                                {"method": "UNSUBSCRIBE", "params": to_unsub, "id": _req_id}))
                            _req_id += 1
                        if to_sub:
                            await ws.send(json.dumps(
                                {"method": "SUBSCRIBE", "params": to_sub, "id": _req_id}))
                            _req_id += 1
                        current_streams = set(new_streams)
                        log("SYS", f"Options WS: strikes shifted -> {new_streams}", "INFO")

                    # Parse depth10 / ticker / markPrice messages
                    stream = msg.get("stream", "")
                    data   = msg.get("data", {})
                    sym    = stream.split("@")[0].upper()

                    if "@depth10" in stream:
                        asks = data.get("asks", [])
                        bids = data.get("bids", [])
                        if sym not in _chain:
                            _chain[sym] = {}
                        # LKG: only update if value > 0
                        _chain_set(sym, "ask",     float(asks[0][0]) if asks else 0.0)
                        _chain_set(sym, "ask_qty", float(asks[0][1]) if asks else 0.0)
                        _chain_set(sym, "bid",     float(bids[0][0]) if bids else 0.0)
                        ask_val = _chain.get(sym, {}).get("ask", 0)
                        if ask_val > 0:
                            _last_valid_opt[sym] = ask_val
                            _last_valid_opt_ts[sym] = time.time()
                    elif "@markPrice" in stream or "@ticker" in stream:
                        mp = float(data.get("mp") or data.get("markPrice") or 0)
                        if mp > 0:
                            if sym not in _chain:
                                _chain[sym] = {}
                            _chain_set(sym, "mark", mp)
                            _last_valid_opt[sym] = mp
                            _last_valid_opt_ts[sym] = time.time()

        except Exception as e:
            _ws_opt_fail_count += 1
            log("SYS", f"Options WS: {e}  -  retry {backoff}s", "WARN")
            if _ws_opt_fail_count == 1:
                asyncio.create_task(_tg(
                    f"⚠ <b>Options WS disconnected</b>\n"
                    f"Bid/ask/mark feed lost. Reconnecting in {backoff}s..."
                ))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def _ticker_loop():
    """Primary options REST API feed: mark prices only, one request per cycle."""
    global _rest_last_ok, _rest_fail_streak, _rest_banned_until
    loop = asyncio.get_running_loop()

    def _marks_api():
        import urllib.error
        req = urllib.request.Request(
            f"{EAPI}/eapi/v1/mark",
            headers={"User-Agent": "StraddleBot/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}") from exc

    while True:
        await asyncio.sleep(5)
        if time.time() < _rest_banned_until:
            continue
        try:
            marks = await loop.run_in_executor(None, _marks_api)
            if not isinstance(marks, list):
                raise RuntimeError("invalid mark API response")
            updated = 0
            for item in marks:
                sym = item.get("symbol", "")
                mark = float(item.get("markPrice") or 0)
                if mark > 0:
                    # Cache every exact BTC option symbol so restored open legs
                    # continue receiving their own expiry mark after chain rollover.
                    if sym.startswith("BTC-"):
                        _last_valid_opt[sym] = mark
                        _last_valid_opt_ts[sym] = time.time()
                    if sym in _chain:
                        _chain_set(sym, "mark", mark)
                        updated += 1
            if updated == 0:
                raise RuntimeError("mark API returned no active-chain prices")
            now = time.time()
            _rest_last_ok = now
            _rest_fail_streak = 0
            _ws_last_msg["opt"] = now
        except Exception as exc:
            _rest_fail_streak += 1
            if "429" in str(exc) or "418" in str(exc):
                _rest_banned_until = time.time() + 60
                log("SYS", "Options mark API rate limited - retrying after 60s", "WARN")
            elif _rest_fail_streak == 1 or _rest_fail_streak % 12 == 0:
                log("SYS", f"Options mark API error: {exc}", "WARN")


async def _paper_fill_monitor():
    while True:
        await asyncio.sleep(0.2)
        mark = _price()
        if mark <= 0:
            continue
        for oid, o in list(_paper_orders.items()):
            if o.get("status") != "NEW":
                continue
            asset = o.get("asset")
            if asset == "future":
                side  = o.get("side")
                lpx   = o.get("price", 0)
                fills = (side == "BUY"  and mark <= lpx) or \
                        (side == "SELL" and mark >= lpx)
                if fills:
                    _paper_orders[oid]["status"]     = "FILLED"
                    _paper_orders[oid]["fill_price"] = lpx
                    log("BOT",
                        f"[PAPER] Futures LIMIT filled: {side} {o.get('pos_side')} @ {lpx:.0f}",
                        "OK")
            elif asset == "option" and o.get("side") == "SELL":
                sym = o.get("symbol", "")
                option_mark = _exact_option_mark(sym)
                lpx = o.get("price", 0)
                if option_mark > 0 and option_mark >= lpx:
                    _paper_orders[oid]["status"]     = "FILLED"
                    _paper_orders[oid]["fill_price"] = lpx
                    log("BOT",
                        f"[PAPER] Option SELL LIMIT filled: {sym} @ {lpx:.0f}", "OK")


# ╔╗
# â•'                        TASK SUPERVISOR                                    â•'
# ╚
async def _supervised(fn, name: str, delay: float = 3.0):
    fail_count = 0
    while True:
        try:
            await fn()
            fail_count = 0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            fail_count += 1
            log("SYS", f"[{name}] crashed (#{fail_count}): {e!r}  -  restart in {delay:.0f}s", "ERROR")
            if fail_count in (1, 5, 20):
                asyncio.create_task(_tg(
                    f"⚠ <b>{name} error (#{fail_count})</b>\n"
                    f"{type(e).__name__}: {e}\n"
                    f"Restarting in {delay:.0f}s..."
                ))
            await asyncio.sleep(delay)


def _fire(coro) -> asyncio.Task:
    """Schedule a coroutine as a background task; log any uncaught exception."""
    async def _wrapper():
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log("SYS", f"Background task error: {e!r}", "ERROR")
    return asyncio.create_task(_wrapper())


# ╔╗
# â•'                        STRADDLE BOT  (state machine)                      â•'
# ╚
class StraddleBot:
    """
    State machine for one ITM straddle position.

    States
    "€"€"€"€"€"€
    SLEEP       : outside window or session already used
    SCANNING    : polling for entry conditions every SCAN_INTERVAL seconds
    EXECUTING   : FOK orders in flight (handled by coroutine, tick skips)
    MANAGING    : position open; futures limits placed; monitoring fills + TP
    DONE        : squareoff complete, all positions settled for this session
    """

    def __init__(self):
        self.state     = "SLEEP"
        self.sess_date = ""
        self.entry_ts  = ""

        # â"€â"€ Options â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
        self.call_sym   = ""      # e.g. BTC-260703-62000-C
        self.call_fill  = 0.0     # fill price (USDT/BTC)
        self.call_qty   = 0.0
        self.call_open  = False   # still holding (not closed via protection)

        self.put_sym    = ""
        self.put_fill   = 0.0
        self.put_qty    = 0.0
        self.put_open   = False

        self.total_prem = 0.0     # call_fill + put_fill

        # â"€â"€ Futures limit orders â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
        self.long_limit_px  = 0.0
        self.short_limit_px = 0.0
        self.long_oid        = 0
        self.short_oid       = 0
        self.long_filled     = False
        self.short_filled    = False
        self.long_entry      = 0.0   # actual fill price
        self.short_entry     = 0.0
        self.long_qty        = 0.0
        self.short_qty       = 0.0

        # â"€â"€ Futures TP orders â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
        self.long_tp_oid     = 0
        self.short_tp_oid    = 0
        self.long_tp_px      = 0.0
        self.short_tp_px     = 0.0
        self.long_tp_done    = False
        self.short_tp_done   = False

        # â"€â"€ Squareoff tracking â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
        self._sq_done       = False  # soft squareoff fired (SQ_START)
        self._hard_sq_fired = False  # hard squareoff fired (SQ_HARD)
        self._entry_orders_cancelled = False
        self._squareoff_reason = ""

        # â"€â"€ Execution guard â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
        self._executing      = False

        #  DB / paper tracking
        self._session_id    = 0     # MariaDB sessions.id for this run
        self._call_oid      = 0     # paper order IDs saved from last FOK attempt
        self._put_oid       = 0
        self._last_snap_ts  = 0.0   # last pnl_snapshot write time
        self.call_sq_exit   = 0.0   # squareoff exit prices (for records)
        self.put_sq_exit    = 0.0
        self.long_sq_exit   = 0.0
        self.short_sq_exit  = 0.0

        # â"€â"€ Throttle timestamps â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
        self.entry_ts_ms     = 0      # epoch-ms when futures limits were placed

        # â"€â"€ Running session high/low (sub-second accuracy, updated every 0.5s) â"€â"€â"€
        self.session_high    = 0.0
        self.session_low     = float("inf")

        self._last_scan_ts   = 0.0
        self._last_poll_ts   = 0.0
        self._last_log_ts    = 0.0
        self._last_reconciliation_date = ""

    # 
    #  MAIN LOOP
    # 
    async def run(self):
        while True:
            await asyncio.sleep(0.5)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log("BOT", f"Tick error: {e!r}", "ERROR")
            if self.state == "DONE":
                log("SYS", "Session settled — resetting for next window in 10s", "OK")
                await asyncio.sleep(10)   # brief pause so in-flight Telegram messages finish
                await self._reset_for_next_session()

    async def _reset_for_next_session(self):
        """
        Called after DONE. Resets all session state so the bot loops back to SLEEP
        and is ready to trade the next day without a process restart.
        """
        global _paper_orders
        prev_sess_date = self.sess_date   # remember which expiry was just traded

        # Full state reset — clears all positions, order IDs, PnL tracking
        self.__init__()

        # Preserve sess_date so already_traded stays True for rest of today
        # (also persisted in LAST_TRADED_EXPIRY config, so survives restart too).
        self.sess_date = prev_sess_date

        # Clear stale paper engine orders from finished session.
        # Wallet is NOT reset — it carries cumulative PnL across sessions.
        _paper_orders.clear()

        log("SYS", f"Bot reset → SLEEP  (traded={prev_sess_date}, waiting for next window)", "OK")
        await _tg(
            f"{_PTAG}🔄 <b>Bot reset → SLEEP</b>\n"
            f"Session settled. Waiting for next trading window."
        )

    async def _tick(self):
        if _control_paused and self.state in ("SLEEP", "SCANNING"):
            return
        now = datetime.now(IST)
        h, m = now.hour, now.minute

        # Run once nightly. It reads the immutable order/fill/ledger trail and
        # alerts without silently modifying accounting records.
        today = now.strftime("%Y-%m-%d")
        if (h, m) >= (0, 10) and self._last_reconciliation_date != today:
            self._last_reconciliation_date = today
            issues = _db_reconcile_records()
            if issues:
                payload = json.dumps(issues, default=str)[:3500]
                log("AUDIT", f"RECONCILIATION FAILED: {payload}", "ERROR")
                asyncio.create_task(_tg(
                    f"🚨 <b>STRADDLE DB RECONCILIATION FAILED</b>\n<pre>{payload}</pre>"
                ))
            else:
                log("AUDIT", "Orders, fills, sessions and wallet match", "OK")

        # Hot-reload config whenever dashboard marks it dirty — runs in ANY state
        if _db:
            try:
                row = _db.execute("SELECT value FROM config WHERE key='_config_dirty'").fetchone()
                if row and str(row[0]) == "1":
                    _db_load_config()
                    _db.execute("UPDATE config SET value='0' WHERE key='_config_dirty'")
                    _db.commit()
                    log("SYS", "Config hot-reloaded from DB (settings saved via dashboard)", "OK")
            except Exception:
                pass

        if self.state == "SLEEP":
            await self._tick_sleep(h, m)
        elif self.state == "SCANNING":
            await self._tick_scan(h, m)
        elif self.state == "EXECUTING":
            pass   # coroutine running; do nothing until it completes
        elif self.state == "MANAGING":
            await self._tick_manage(h, m)
        # DONE: nothing to do

        # Write live bot state + diagnostics every tick (single-row UPSERT, O(1))
        if _db:
            try:
                detail_json = json.dumps(self._build_status_detail())
                _db.execute(
                    "INSERT OR REPLACE INTO bot_status (id,ts,state,btc_mark,session_id,detail) "
                    "VALUES (1,?,?,?,?,?)",
                    (_ts(), self.state, _price_safe(), self._session_id, detail_json)
                )
                _db.commit()
            except Exception:
                pass

        # Periodic status log
        if self.state not in ("SLEEP", "DONE") and time.time() - self._last_log_ts > 30:
            self._last_log_ts = time.time()
            self._log_status()

    # 
    #  SLEEP
    # 
    async def _tick_sleep(self, h: int, m: int):
        if _has_open_positions:
            return   # blocked: existing positions detected at startup
        now_min = _to_min(h, m)
        if not _in_entry_window(now_min):
            return
        _last_traded = self.sess_date or _db_get_config("LAST_TRADED_EXPIRY") or ""
        if _last_traded == _expiry_iso():
            return   # already traded this expiry (persisted across restarts)
        _fut_stale = _feed_age_s("fut") > 10
        _opt_stale = _feed_age_s("opt") > 30 and (time.time() - _rest_last_ok) > 30
        if not _all_strikes or not _price() or _fut_stale or _opt_stale:
            return   # data not ready

        # Activate only when within 24h of the next expiry.
        # Session = 13:31 day N → 13:30 day N+1.  Window can span both dates.
        # "Same-day expiry" means < 24h remaining — NOT a calendar-date comparison.
        if _expiry_ms:
            hours_to_expiry = (_expiry_ms - int(time.time() * 1000)) / 3_600_000
            if hours_to_expiry < 0 or hours_to_expiry >= 24:
                return   # more than 24h to expiry — stay SLEEP
        self.state = "SCANNING"
        log("BOT", "Window open -> SCANNING", "STATE")
        _db_event_insert(0, "STATE_SCANNING", f"expiry={_expiry_iso()} spot={round(_price() or 0,0):.0f}")
        start_m   = _to_min(*WINDOW_START)
        end_m     = _to_min(*WINDOW_END)
        overnight = start_m > end_m
        if not overnight:
            mins_left = end_m - now_min
        else:
            mins_left = ((24*60 - now_min) + end_m) if now_min >= start_m else (end_m - now_min)
        asyncio.create_task(_tg(
            f" <b>Entry Window OPEN</b>\n"
            f"Scanning for ITM straddle setup...\n"
            f"Window closes in {_fmt_hm(mins_left)}\n"
            f"Squareoff starts {SQUAREOFF_START[0]:02d}:{SQUAREOFF_START[1]:02d} IST"
        ))

    # 
    #  SCANNING
    # 
    async def _tick_scan(self, h: int, m: int):
        now_min = _to_min(h, m)

        # Window closed before we entered
        if not _in_entry_window(now_min):
            self.state = "SLEEP"
            log("BOT", "Window closed -> SLEEP", "STATE")
            asyncio.create_task(_tg(
                f" <b>Entry Window CLOSED</b>\n"
                f"No valid setup found during window.\n"
                f"Bot sleeping until next session."
            ))
            return

        # Squareoff time reached without entry  -  only relevant on expiry day
        if _is_expiry_day() and now_min >= _to_min(*SQUAREOFF_START):
            self.state = "SLEEP"
            log("BOT", "Squareoff time reached without entry -> SLEEP", "STATE")
            asyncio.create_task(_tg(
                f" <b>Squareoff time  -  no position taken</b>\n"
                f"Window passed without a valid setup.\n"
                f"Bot sleeping."
            ))
            return

        if time.time() - self._last_scan_ts < SCAN_INTERVAL:
            return
        self._last_scan_ts = time.time()

        spot = _price()
        if not spot:
            return

        pair = _find_itm_pair(spot)
        if pair is None:
            return

        call_opt, put_opt = pair
        ok, reason = _check_conditions(call_opt, put_opt)
        if not ok:
            if time.time() - self._last_log_ts > 10:
                self._last_log_ts = time.time()
                detail = _conditions_detail(call_opt, put_opt)
                log("BOT", f"Waiting: {reason}\n{detail}", "INFO")
            return

        # All conditions pass -> execute
        if self._executing or self.state != "SCANNING":
            return   # safety: never double-execute
        log("BOT",
            f"✅ All conditions met\n"
            f"   CALL {call_opt['symbol']} mark={call_opt['mark']:.0f}\n"
            f"   PUT  {put_opt['symbol']} mark={put_opt['mark']:.0f}\n"
            f"   Total mark={call_opt['mark'] + put_opt['mark']:.0f}  "
            f"gap={put_opt['strike'] - call_opt['strike']:.0f}", "OK")
        self.state    = "EXECUTING"
        self._executing = True
        _fire(self._execute_straddle(call_opt, put_opt))

    # 
    #  EXECUTE  -  simultaneous FOK orders
    # 
    async def _execute_straddle(self, call_opt: dict, put_opt: dict):
        self._executing = True
        try:
            call_mark_ticked = _opt_tick(call_opt["mark"])
            put_mark_ticked  = _opt_tick(put_opt["mark"])

            log("BOT", f"Firing mark FOK: CALL@{call_mark_ticked}  PUT@{put_mark_ticked}", "TRADE")

            # Fire both FOK simultaneously
            results = await asyncio.gather(
                option_order(call_opt["symbol"], "BUY", TRADE_QTY,
                             call_mark_ticked, "LIMIT", "FOK"),
                option_order(put_opt["symbol"],  "BUY", TRADE_QTY,
                             put_mark_ticked,  "LIMIT", "FOK"),
                return_exceptions=True
            )
            call_res, put_res = results

            await asyncio.sleep(1.5)   # brief pause for fills to register

            # â"€â"€ Query fill status â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
            call_filled, call_oid = False, 0
            put_filled,  put_oid  = False, 0

            if not isinstance(call_res, Exception):
                call_oid = int(call_res.get("orderId", 0))
            else:
                log("BOT", f"CALL FOK placement error: {call_res}", "ERROR")

            if not isinstance(put_res, Exception):
                put_oid = int(put_res.get("orderId", 0))
            else:
                log("BOT", f"PUT FOK placement error: {put_res}", "ERROR")

            self._call_oid = call_oid   # save for _on_both_filled / _handle_partial_only
            self._put_oid  = put_oid

            if call_oid:
                try:
                    q = {}
                    for _att in range(3):
                        q = await option_query(call_opt["symbol"], call_oid)
                        if q.get("status") in ("FILLED", "EXPIRED", "CANCELLED"):
                            break
                        await asyncio.sleep(1.0)
                    if q.get("status") == "FILLED":
                        call_filled     = True
                        self.call_sym   = call_opt["symbol"]
                        self.call_fill  = float(call_opt["mark"])
                        self.call_qty   = float(q.get("executedQty") or TRADE_QTY)
                        self.call_open  = True
                    else:
                        # FOK not filled — record with session_id=0 (no session yet)
                        _db_order_insert(0, call_oid, call_opt["symbol"], "option",
                                         "CALL_ENTRY", "BUY", "FOK", TRADE_QTY,
                                         call_mark_ticked, 0, "EXPIRED")
                except Exception as e:
                    log("BOT", f"CALL order query: {e}", "WARN")

            if put_oid:
                try:
                    q = {}
                    for _att in range(3):
                        q = await option_query(put_opt["symbol"], put_oid)
                        if q.get("status") in ("FILLED", "EXPIRED", "CANCELLED"):
                            break
                        await asyncio.sleep(1.0)
                    if q.get("status") == "FILLED":
                        put_filled    = True
                        self.put_sym  = put_opt["symbol"]
                        self.put_fill = float(put_opt["mark"])
                        self.put_qty  = float(q.get("executedQty") or TRADE_QTY)
                        self.put_open = True
                    else:
                        _db_order_insert(0, put_oid, put_opt["symbol"], "option",
                                         "PUT_ENTRY", "BUY", "FOK", TRADE_QTY,
                                         put_mark_ticked, 0, "EXPIRED")
                except Exception as e:
                    log("BOT", f"PUT order query: {e}", "WARN")

            log("BOT",
                f"FOK result: CALL={'FILLED' if call_filled else 'FAILED'}  "
                f"PUT={'FILLED' if put_filled else 'FAILED'}", "TRADE")

            if call_filled and put_filled:
                self.total_prem = self.call_fill + self.put_fill
                await self._on_both_filled()

            elif not call_filled and not put_filled:
                # Both failed  -  back to scanning immediately
                log("BOT", "Both FOK failed -> back to SCANNING", "WARN")
                self.state = "SCANNING"
                self._last_scan_ts = 0   # allow immediate re-scan

            elif call_filled and not put_filled:
                log("BOT", "CALL filled / PUT failed -> retrying PUT", "WARN")
                put_ok = await self._retry_single(put_opt["symbol"], "PUT")
                if put_ok:
                    self.total_prem = self.call_fill + self.put_fill
                    await self._on_both_filled()
                else:
                    await self._handle_partial_only("CALL")

            else:
                log("BOT", "PUT filled / CALL failed -> retrying CALL", "WARN")
                call_ok = await self._retry_single(call_opt["symbol"], "CALL")
                if call_ok:
                    self.total_prem = self.call_fill + self.put_fill
                    await self._on_both_filled()
                else:
                    await self._handle_partial_only("PUT")

        except Exception as e:
            log("BOT", f"Execute straddle error: {e!r}", "ERROR")
            await _tg(f" <b>Execute error</b>\n{e}\nCheck terminal  -  returning to SCANNING")
            self.state = "SCANNING"
        finally:
            self._executing = False

    async def _retry_single(self, sym: str, leg: str) -> bool:
        """
        Retry buying a single failed leg for up to RETRY_TIMEOUT seconds.
        Returns True if filled, False if timed out.
        """
        # Safety guard: confirm we don't already hold the position before retrying
        try:
            existing = await _get(EAPI, "/eapi/v1/position")
            for p in (existing if isinstance(existing, list) else []):
                if p.get("symbol") == sym and float(p.get("quantity") or 0) >= TRADE_QTY:
                    fill_px = float(_chain.get(sym, {}).get("mark") or 0)
                    log("BOT", f"Retry {leg}: already holding {sym} @ {fill_px:.0f}  -  skip retry", "WARN")
                    if leg == "CALL":
                        self.call_sym  = sym; self.call_fill = fill_px
                        self.call_qty  = TRADE_QTY; self.call_open = True
                    else:
                        self.put_sym   = sym; self.put_fill  = fill_px
                        self.put_qty   = TRADE_QTY; self.put_open  = True
                    return True
        except Exception as e:
            log("BOT", f"Retry {leg}: position safety check failed: {e}", "WARN")

        deadline = time.time() + RETRY_TIMEOUT
        attempt  = 0
        while time.time() < deadline:
            opt = _chain.get(sym, {})
            mark = opt.get("mark") or 0.0

            if mark > 0:
                ticked = _opt_tick(mark)
                attempt += 1
                log("BOT", f"Retry {leg} #{attempt} FOK at mark {ticked}", "TRADE")
                if True:
                    try:
                        res = await option_order(sym, "BUY", TRADE_QTY,
                                                 ticked, "LIMIT", "FOK")
                        oid = int(res.get("orderId", 0))
                        await asyncio.sleep(1.5)
                        if oid:
                            q = await option_query(sym, oid)
                            if q.get("status") == "FILLED":
                                fill_px = float(mark)
                                if leg == "CALL":
                                    self.call_sym  = sym
                                    self.call_fill = fill_px
                                    self.call_qty  = float(q.get("executedQty") or TRADE_QTY)
                                    self.call_open = True
                                else:
                                    self.put_sym   = sym
                                    self.put_fill  = fill_px
                                    self.put_qty   = float(q.get("executedQty") or TRADE_QTY)
                                    self.put_open  = True
                                log("BOT", f"✅ Retry {leg} FILLED @ {fill_px:.0f}", "OK")
                                return True
                            log("BOT", f"Retry {leg} FOK not filled", "WARN")
                    except Exception as e:
                        log("BOT", f"Retry {leg} order error: {e}", "WARN")
            await asyncio.sleep(2)

        log("BOT", f"Retry {leg} timed out after {RETRY_TIMEOUT}s", "WARN")
        return False

    async def _handle_partial_only(self, filled_leg: str):
        """One leg filled, other could not be retried. Alert and manage single option."""
        msg = (f"⚠ <b>Partial straddle  -  only {filled_leg} filled</b>\n"
               f"Retry timed out. Managing single option until squareoff.\n"
               f"No futures setup (no valid entry levels without both legs).")
        log("BOT", f"Partial entry: only {filled_leg} open  -  no futures", "CRIT")
        await _tg(msg)

        self.total_prem = (self.call_fill or 0.0) + (self.put_fill or 0.0)
        self.sess_date  = _expiry_iso()
        self.entry_ts   = datetime.now(IST).strftime("%H:%M:%S")

        # Create a session so all downstream DB writes (snapshots, events) have a valid ID
        exp_sym = _expiry_6d
        exp_dt  = (datetime.fromtimestamp(_expiry_ms / 1000, tz=IST).strftime("%Y-%m-%d %H:%M")
                   if _expiry_ms else "")
        self._session_id = _db_session_create(exp_sym, exp_dt)

        if filled_leg == "CALL" and self.call_sym:
            _db_session_update(self._session_id,
                call_sym=self.call_sym, call_fill=self.call_fill,
                call_qty=self.call_qty, total_premium=self.total_prem)
            _db_order_insert(self._session_id, self._call_oid, self.call_sym, "option",
                             "CALL_ENTRY", "BUY", "FOK", self.call_qty,
                             0, self.call_fill, "FILLED", _ts())
        elif filled_leg == "PUT" and self.put_sym:
            _db_session_update(self._session_id,
                put_sym=self.put_sym, put_fill=self.put_fill,
                put_qty=self.put_qty, total_premium=self.total_prem)
            _db_order_insert(self._session_id, self._put_oid, self.put_sym, "option",
                             "PUT_ENTRY", "BUY", "FOK", self.put_qty,
                             0, self.put_fill, "FILLED", _ts())

        _db_event_insert(self._session_id, "PARTIAL_STRADDLE",
                         f"Only {filled_leg} filled. No futures. prem={self.total_prem:.0f}")
        self.state = "MANAGING"

    # 
    #  BOTH FILLED  -  set up futures limits
    # 
    async def _on_both_filled(self):
        global _paper_wallet_balance
        call_strike = float(self.call_sym.split("-")[2])
        put_strike  = float(self.put_sym.split("-")[2])
        tp          = self.total_prem

        long_px  = round(put_strike  - tp, 1)   # LONG limit: PUT_strike âˆ' total_prem
        short_px = round(call_strike + tp, 1)   # SHORT limit: CALL_strike + total_prem

        self.long_limit_px  = long_px
        self.short_limit_px = short_px
        # Revised strategy uses one common option strike as the centre.
        long_px = round(call_strike - tp, 1)
        short_px = round(call_strike + tp, 1)
        self.long_limit_px = long_px
        self.short_limit_px = short_px
        self.long_qty       = TRADE_QTY
        self.short_qty      = TRADE_QTY

        # Deduct options premium from paper wallet (cumulative — not reset per session)
        if PAPER_TRADE:
            prem_cost = tp * self.call_qty
            wallet_before_deduction = _paper_wallet_balance
            _paper_wallet_balance = max(0.0, _paper_wallet_balance - prem_cost)
            self._wallet_before = wallet_before_deduction
            log("BOT", f"Paper wallet: -{prem_cost:.2f} (options prem)  bal={_paper_wallet_balance:.2f}", "INFO")

        log("BOT",
            f"Straddle open:\n"
            f"   CALL {self.call_sym} @ {self.call_fill:.0f}\n"
            f"   PUT  {self.put_sym}  @ {self.put_fill:.0f}\n"
            f"   Total premium = {tp:.0f} USDT\n"
            f"   LONG  LIMIT @ {long_px:.0f}  (= {put_strike:.0f} âˆ' {tp:.0f})\n"
            f"   SHORT LIMIT @ {short_px:.0f}  (= {call_strike:.0f} + {tp:.0f})", "TRADE")

        # TP distance = prem + TV; TV = prem - gap; floor at 50 pts to prevent degenerate case
        _cs      = float(self.call_sym.split("-")[2])
        _ps      = float(self.put_sym.split("-")[2])
        _tp_dist = max(self.total_prem + (self.total_prem - (_ps - _cs)), 50.0)
        long_tp_px  = round(long_px  + _tp_dist, 1)
        short_tp_px = round(short_px - _tp_dist, 1)
        _tp_dist = self.total_prem * FUTURES_TP_MULTIPLIER
        long_tp_px = round(long_px + _tp_dist, 1)
        short_tp_px = round(short_px - _tp_dist, 1)

        # Place LONG entry + TP together
        try:
            r = await futures_limit("BUY", TRADE_QTY, long_px, "LONG")
            self.long_oid = int(r.get("orderId", 0))
            log("BOT", f"LONG entry LIMIT @ {long_px:.0f}  oid={self.long_oid}", "OK")
        except Exception as e:
            log("BOT", f"LONG entry LIMIT failed: {e}", "ERROR")
            await _tg(f"⚠ LONG entry LIMIT failed:\n{e}")

        try:
            r = {"orderId": 0}
            self.long_tp_oid = int(r.get("orderId", 0))
            self.long_tp_px  = long_tp_px
            log("BOT", f"LONG TP pre-placed @ {long_tp_px:.0f}  oid={self.long_tp_oid}", "OK")
        except Exception as e:
            log("BOT", f"LONG TP pre-placement failed: {e}", "ERROR")

        # Place SHORT entry + TP together
        try:
            r = await futures_limit("SELL", TRADE_QTY, short_px, "SHORT")
            self.short_oid = int(r.get("orderId", 0))
            log("BOT", f"SHORT entry LIMIT @ {short_px:.0f}  oid={self.short_oid}", "OK")
        except Exception as e:
            log("BOT", f"SHORT entry LIMIT failed: {e}", "ERROR")
            await _tg(f"⚠ SHORT entry LIMIT failed:\n{e}")

        try:
            r = {"orderId": 0}
            self.short_tp_oid = int(r.get("orderId", 0))
            self.short_tp_px  = short_tp_px
            log("BOT", f"SHORT TP pre-placed @ {short_tp_px:.0f}  oid={self.short_tp_oid}", "OK")
        except Exception as e:
            log("BOT", f"SHORT TP pre-placement failed: {e}", "ERROR")

        self.sess_date   = _expiry_iso()
        self.entry_ts    = datetime.now(IST).strftime("%H:%M:%S")
        self.entry_ts_ms = int(time.time() * 1000)
        self.state       = "MANAGING"

        #  DB: atomic write — session + all orders in one BEGIN/COMMIT
        if _db:
            try:
                exp_sym = _expiry_6d
                exp_dt  = (datetime.fromtimestamp(_expiry_ms / 1000, tz=IST).strftime("%Y-%m-%d %H:%M")
                           if _expiry_ms else "")
                now_s = _ts()
                def _oi(oid, sym, at, ll, sd, ot, qty, lx=0, fx=0, st="NEW", fat=None):
                    _db.execute(
                        "INSERT INTO orders "
                        "(session_id,paper_order_id,symbol,asset_type,leg_label,"
                        "side,order_type,qty,limit_price,fill_price,status,"
                        "placed_at,filled_at,cancel_reason,updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (self._session_id, oid, sym, at, ll, sd, ot, qty,
                         lx, fx, st, now_s, fat, None, now_s)
                    )
                _wb = getattr(self, "_wallet_before", _paper_wallet_balance)
                with _db:
                    cur = _db.execute(
                        "INSERT INTO sessions "
                        "(date,expiry_sym,expiry_dt,state,start_dt,entry_ts_ms,"
                        "call_sym,put_sym,call_fill,put_fill,call_qty,put_qty,total_premium,"
                        "entry_btc,tp_dist,wallet_before) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (_expiry_iso(), exp_sym, exp_dt,  # expiry date — matches already_traded guard
                         "ACTIVE", now_s, self.entry_ts_ms,
                         self.call_sym, self.put_sym,
                         self.call_fill, self.put_fill,
                         self.call_qty, self.put_qty, self.total_prem,
                         round(_price() or 0, 1), round(_tp_dist, 2), round(_wb, 2))
                    )
                    self._session_id = cur.lastrowid
                    _oi(self._call_oid, self.call_sym, "option", "CALL_ENTRY",
                        "BUY", "FOK", self.call_qty, 0, self.call_fill, "FILLED", now_s)
                    _oi(self._put_oid,  self.put_sym,  "option", "PUT_ENTRY",
                        "BUY", "FOK", self.put_qty,  0, self.put_fill,  "FILLED", now_s)
                    for instrument, oid, qty, fill_px in (
                        ("CALL_OPT", self._call_oid, self.call_qty, self.call_fill),
                        ("PUT_OPT", self._put_oid, self.put_qty, self.put_fill),
                    ):
                        _db.execute(
                            """INSERT INTO fills
                               (session_id,ts,instrument,side,qty,ask_at_order,
                                fill_price,slippage,order_id,note)
                               VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            (self._session_id, now_s, instrument, "BUY", qty,
                             fill_px, fill_px, 0.0, str(oid),
                             "atomic_option_entry"),
                        )
                    # Re-link any failed FOK attempts for these symbols to this session
                    _db.execute(
                        "UPDATE orders SET session_id=? WHERE session_id=0"
                        " AND symbol IN (?,?) AND leg_label IN ('CALL_ENTRY','PUT_ENTRY')",
                        (self._session_id, self.call_sym, self.put_sym)
                    )
                    if self.long_oid:
                        _oi(self.long_oid, "BTCUSDT", "future", "LONG_ENTRY",
                            "BUY", "LIMIT", TRADE_QTY, long_px)
                    if self.long_tp_oid:
                        _oi(self.long_tp_oid, "BTCUSDT", "future", "LONG_TP",
                            "SELL", "LIMIT", TRADE_QTY, long_tp_px)
                    if self.short_oid:
                        _oi(self.short_oid, "BTCUSDT", "future", "SHORT_ENTRY",
                            "SELL", "LIMIT", TRADE_QTY, short_px)
                    if self.short_tp_oid:
                        _oi(self.short_tp_oid, "BTCUSDT", "future", "SHORT_TP",
                            "BUY", "LIMIT", TRADE_QTY, short_tp_px)
                    _db.execute(
                        "INSERT INTO events (session_id,ts,event_type,detail) "
                        "VALUES (?,?,?,?)",
                        (self._session_id, now_s, "STRADDLE_ENTERED",
                         f"CALL@{self.call_fill:.0f} PUT@{self.put_fill:.0f}"
                         f" prem={self.total_prem:.0f} tp_dist={_tp_dist:.0f} btc={round(_price() or 0,0):.0f}")
                    )
                    # Wallet debit, balance snapshot and session are one commit.
                    _db.execute(
                        "INSERT INTO wallet_ledger (ts,session_id,type,amount,balance_after,note) "
                        "VALUES (?,?,?,?,?,?)",
                        (now_s, self._session_id, "PREMIUM_DEBIT",
                         -round(self.total_prem * self.call_qty, 4),
                         round(_paper_wallet_balance, 4),
                         f"CALL@{self.call_fill:.0f} PUT@{self.put_fill:.0f} qty={self.call_qty}")
                    )
                    _db.execute(
                        """INSERT OR REPLACE INTO config(key,value)
                           VALUES ('PAPER_WALLET_BALANCE',?)""",
                        (str(round(_paper_wallet_balance, 2)),),
                    )
            except Exception as _dbe:
                log("BOT", f"Session DB write CRITICAL — in-memory OK, session_id=0: {_dbe!r}", "CRIT")
                if PAPER_TRADE:
                    _paper_wallet_balance = getattr(
                        self, "_wallet_before", _paper_wallet_balance
                    )
                self._session_id = 0

        await _tg(
            f"{_PTAG} <b>Straddle ENTERED</b>  [{self.entry_ts} IST]\n"
            f"─────────────────────────────────────────────────────────────────────\n"
            f" CALL : {self.call_sym}  @ <b>{self.call_fill:.0f}</b>\n"
            f" PUT  : {self.put_sym}  @ <b>{self.put_fill:.0f}</b>\n"
            f" Premium : <b>{tp:.0f}</b> USDT\n\n"
            f"📊 Futures:\n"
            f"  LONG  entry @ <b>{long_px:.0f}</b>  TP @ <b>{long_tp_px:.0f}</b>\n"
            f"  SHORT entry @ <b>{short_px:.0f}</b>  TP @ <b>{short_tp_px:.0f}</b>"
        )

    # 
    #  MANAGING
    # 
    async def _tick_manage(self, h: int, m: int):
        now_min = _to_min(h, m)

        # Always process exchange fills before applying time-based decisions.
        if time.time() - self._last_poll_ts >= 2.0:
            self._last_poll_ts = time.time()
            await self._poll_futures_fills()

        futures_open = self.long_filled or self.short_filled

        # A futures TP completes the strategy immediately; options are closed too.
        if self.long_tp_done or self.short_tp_done:
            self._squareoff_reason = "FUTURES_TP"
            await self._do_squareoff()
            return

        # Neither breakout fired by 10:00: cancel both futures entries.
        if (not futures_open
                and now_min >= _to_min(*FUTURES_ENTRY_CUTOFF)
                and not self._entry_orders_cancelled):
            await self._cancel_all_pending_orders()
            self._entry_orders_cancelled = True
            _db_event_insert(self._session_id, "FUTURES_ENTRY_EXPIRED", "No futures fill by cutoff")

        # If no futures entry filled by 11:00, wait for a mark-only 75% recovery
        # during 11:00-12:00 and square off immediately when it is available.
        if not futures_open and now_min >= _to_min(*SQUAREOFF_START):
            call_mark = _exact_option_mark(self.call_sym)
            put_mark = _exact_option_mark(self.put_sym)
            combined_mark = call_mark + put_mark
            recovery_level = self.total_prem * (OPTIONS_RECOVERY_PCT / 100.0)
            if call_mark > 0 and put_mark > 0 and combined_mark >= recovery_level:
                self._squareoff_reason = "OPTIONS_75_MARK_RECOVERY"
                await self._do_squareoff()
                return

        # Universal 12:00 hard squareoff, whether futures filled or not.
        if now_min >= _to_min(*SQUAREOFF_HARD):
            self._squareoff_reason = "UNIVERSAL_HARD_1200"
            await self._do_squareoff()
            return

        spot = _price_safe()
        if spot:
            if spot > self.session_high:
                self.session_high = spot
            if spot < self.session_low:
                self.session_low = spot
        if time.time() - self._last_snap_ts >= 30.0:
            self._last_snap_ts = time.time()
            self._write_pnl_snapshot()
        return

        # Determine if squareoff should fire (handles evening window + midnight crossing)
        _sq_h, _sq_m = SQUAREOFF_START
        _past_sq = False
        if self.entry_ts_ms > 0:
            # Primary: squareoff at SQ_TIME on the entry day; if SQ_TIME ≤ entry time, push to next day
            _entry_dt = datetime.fromtimestamp(self.entry_ts_ms / 1000, tz=IST)
            _sq_dt    = _entry_dt.replace(hour=_sq_h, minute=_sq_m, second=0, microsecond=0)
            if _sq_dt <= _entry_dt:
                _sq_dt += timedelta(days=1)
            _past_sq = datetime.now(IST) >= _sq_dt
        elif self._session_id == 0:
            # Orphaned session (DB write failed at entry): squareoff whenever outside entry window
            _past_sq = not _in_entry_window(now_min)
        else:
            # Fallback: same-day sq_time AND expiry approaching within 24h
            _hours_left = (_expiry_ms - int(time.time() * 1000)) / 3_600_000 if _expiry_ms else 25
            _past_sq = now_min >= _to_min(_sq_h, _sq_m) and 0 < _hours_left < 24

        # SQUAREOFF_HARD — absolute deadline: reset _sq_done and re-fire if still MANAGING
        _hq_h, _hq_m = SQUAREOFF_HARD
        if self.entry_ts_ms > 0:
            _hard_dt = _entry_dt.replace(hour=_hq_h, minute=_hq_m, second=0, microsecond=0)
            if _hard_dt <= _entry_dt:
                _hard_dt += timedelta(days=1)
            _past_hard = datetime.now(IST) >= _hard_dt
        elif self._session_id == 0:
            _past_hard = not _in_entry_window(now_min)
        else:
            _past_hard = _to_min(h, m) >= _to_min(_hq_h, _hq_m)

        if _past_hard and not self._hard_sq_fired:
            self._hard_sq_fired = True
            self._sq_done = False   # allow squareoff to run even if soft already fired
            log("BOT", "SQUAREOFF_HARD deadline — forcing squareoff now", "CRIT")
            asyncio.create_task(self._do_squareoff())
            return

        if _past_sq:
            if not self._sq_done:
                log("BOT", "Squareoff time -> running _do_squareoff", "STATE")
                asyncio.create_task(self._do_squareoff())
            return

        # Update running session high/low every tick (0.5s resolution)
        spot = _price() or _last_valid_mark
        if spot:
            if spot > self.session_high: self.session_high = spot
            if spot < self.session_low:  self.session_low  = spot

        # Poll futures order fills every 5s
        if time.time() - self._last_poll_ts >= 5.0:
            self._last_poll_ts = time.time()
            await self._poll_futures_fills()

        # Write PnL snapshot to DB every 30s
        if time.time() - self._last_snap_ts >= 30.0:
            self._last_snap_ts = time.time()
            self._write_pnl_snapshot()

    def _write_pnl_snapshot(self):
        """Compute current MTM PnL for all legs and persist to pnl_snapshots table."""
        if not _db or not self._session_id:
            return
        spot   = _price()
        c_mark = _exact_option_mark(self.call_sym)
        p_mark = _exact_option_mark(self.put_sym)
        # Never append a zero/stale snapshot over the last valid accounting row.
        # Every still-open leg must have its own fresh exact-symbol mark.
        if (self.call_open and self.call_fill > 0 and c_mark <= 0) or \
           (self.put_open and self.put_fill > 0 and p_mark <= 0) or \
           ((self.long_filled or self.short_filled) and spot <= 0):
            return
        c_upnl = _opt_upnl(self.call_fill,  c_mark, self.call_qty)
        p_upnl = _opt_upnl(self.put_fill,   p_mark, self.put_qty)
        l_upnl = _fut_upnl(self.long_entry,  spot, self.long_qty,  "LONG")  if self.long_filled  else 0.0
        s_upnl = _fut_upnl(self.short_entry, spot, self.short_qty, "SHORT") if self.short_filled else 0.0
        l_liq  = _calc_liq_price_cross(self.long_entry,  self.long_qty,  "LONG")  if self.long_filled  else 0.0
        s_liq  = _calc_liq_price_cross(self.short_entry, self.short_qty, "SHORT") if self.short_filled else 0.0
        margin = ((self.long_entry  * self.long_qty  if self.long_filled  else 0)
                + (self.short_entry * self.short_qty if self.short_filled else 0)) / FUTURES_LEVERAGE
        _db_snapshot_insert(self._session_id, spot, c_mark, p_mark,
                            c_upnl, p_upnl, l_upnl, s_upnl, l_liq, s_liq, margin)

    async def _poll_futures_fills(self):
        """Poll entry fills and TP fills. TPs are pre-placed at entry time."""
        # â"€â"€ LONG entry â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
        if self.long_oid and not self.long_filled:
            try:
                q = await futures_query(self.long_oid)
                if q.get("status") == "FILLED":
                    self.long_filled = True
                    self.long_entry  = float(_price() or self.long_limit_px)
                    liq    = _calc_liq_price_cross(self.long_entry, self.long_qty, "LONG")
                    margin = (self.long_entry * self.long_qty) / FUTURES_LEVERAGE
                    _db_atomic_future_entry_fill(
                        self._session_id, "LONG", self.long_qty,
                        self.long_entry, self.long_tp_px, liq, margin
                    )
                    if self.short_oid and not self.short_filled:
                        await futures_cancel(self.short_oid)
                    if self.short_tp_oid and not self.short_tp_done:
                        await futures_cancel(self.short_tp_oid)
                    self._entry_orders_cancelled = True
                    if not self.long_tp_oid:
                        tp_order = await futures_limit("SELL", self.long_qty, self.long_tp_px, "LONG")
                        self.long_tp_oid = int(tp_order.get("orderId", 0))
                        _db_order_insert(
                            self._session_id, self.long_tp_oid, "BTCUSDT", "future",
                            "LONG_TP", "SELL", "LIMIT", self.long_qty, self.long_tp_px
                        )
                    log("BOT", f"LONG FILLED @ {self.long_entry:.0f}  TP already @ {self.long_tp_px:.0f}", "TRADE")
                    asyncio.create_task(_tg(
                        f"⚡ <b>LONG filled</b> @ <b>{self.long_entry:.0f}</b>\n"
                        f"TP already in market @ <b>{self.long_tp_px:.0f}</b>"
                    ))
            except Exception as e:
                log("BOT", f"LONG entry query: {e}", "WARN")

        # â"€â"€ SHORT entry â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
        if self.short_oid and not self.short_filled:
            try:
                q = await futures_query(self.short_oid)
                if q.get("status") == "FILLED":
                    self.short_filled = True
                    self.short_entry  = float(_price() or self.short_limit_px)
                    liq       = _calc_liq_price_cross(self.short_entry, self.short_qty, "SHORT")
                    margin_tot = ((self.long_entry * self.long_qty if self.long_filled else 0)
                                  + self.short_entry * self.short_qty) / FUTURES_LEVERAGE
                    _db_atomic_future_entry_fill(
                        self._session_id, "SHORT", self.short_qty,
                        self.short_entry, self.short_tp_px, liq, margin_tot
                    )
                    if self.long_oid and not self.long_filled:
                        await futures_cancel(self.long_oid)
                    if self.long_tp_oid and not self.long_tp_done:
                        await futures_cancel(self.long_tp_oid)
                    self._entry_orders_cancelled = True
                    if not self.short_tp_oid:
                        tp_order = await futures_limit("BUY", self.short_qty, self.short_tp_px, "SHORT")
                        self.short_tp_oid = int(tp_order.get("orderId", 0))
                        _db_order_insert(
                            self._session_id, self.short_tp_oid, "BTCUSDT", "future",
                            "SHORT_TP", "BUY", "LIMIT", self.short_qty, self.short_tp_px
                        )
                    log("BOT", f"SHORT FILLED @ {self.short_entry:.0f}  TP already @ {self.short_tp_px:.0f}", "TRADE")
                    asyncio.create_task(_tg(
                        f"⚡ <b>SHORT filled</b> @ <b>{self.short_entry:.0f}</b>\n"
                        f"TP already in market @ <b>{self.short_tp_px:.0f}</b>"
                    ))
            except Exception as e:
                log("BOT", f"SHORT entry query: {e}", "WARN")

        # â"€â"€ TP fills â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
        if self.long_tp_oid and self.long_filled and not self.long_tp_done:
            try:
                q = await futures_query(self.long_tp_oid)
                if q.get("status") == "FILLED":
                    self.long_tp_done = True
                    self.long_sq_exit = float(_price() or self.long_tp_px)
                    pnl = (self.long_sq_exit - self.long_entry) * self.long_qty
                    log("BOT", f"LONG TP HIT | exit mark={self.long_sq_exit:.0f}  PnL={pnl:+.2f}", "OK")
                    _db_atomic_future_tp_fill(
                        self._session_id, "LONG", self.long_qty,
                        self.long_sq_exit, pnl
                    )
                    asyncio.create_task(_tg(
                        f"✅ <b>LONG TP hit</b> @ {self.long_tp_px:.0f}\n"
                        f"Futures PnL: +{pnl:.2f} USDT"))
            except Exception as e:
                log("BOT", f"LONG TP query: {e}", "WARN")

        if self.short_tp_oid and self.short_filled and not self.short_tp_done:
            try:
                q = await futures_query(self.short_tp_oid)
                if q.get("status") == "FILLED":
                    self.short_tp_done = True
                    self.short_sq_exit = float(_price() or self.short_tp_px)
                    pnl = (self.short_entry - self.short_sq_exit) * self.short_qty
                    log("BOT", f"SHORT TP HIT | exit mark={self.short_sq_exit:.0f}  PnL={pnl:+.2f}", "OK")
                    _db_atomic_future_tp_fill(
                        self._session_id, "SHORT", self.short_qty,
                        self.short_sq_exit, pnl
                    )
                    asyncio.create_task(_tg(
                        f"✅ <b>SHORT TP hit</b> @ {self.short_tp_px:.0f}\n"
                        f"Futures PnL: +{pnl:.2f} USDT"))
            except Exception as e:
                log("BOT", f"SHORT TP query: {e}", "WARN")

    #
    #  CANCEL ALL MANAGING-PHASE PENDING ORDERS
    #
    async def _cancel_all_pending_orders(self):
        """
        Cancel every open order placed during the MANAGING phase before squareoff.
        Covers: unfilled futures limits, futures TP orders, liq protection options.
        Does NOT cancel option SQ orders (those are the close orders we want to fill).
        """
        log("BOT", "Cancelling all pending MANAGING orders before squareoff...", "INFO")

        # â"€â"€ Futures limit orders (if not yet filled) â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
        if self.long_oid and not self.long_filled:
            try:
                await futures_cancel(self.long_oid)
                log("BOT", f"Cancelled LONG LIMIT oid={self.long_oid}", "INFO")
                _db_order_cancel(self._session_id, "LONG_ENTRY", "SQ_PREP")
            except Exception as e:
                log("BOT", f"Cancel LONG LIMIT: {e}", "WARN")

        if self.short_oid and not self.short_filled:
            try:
                await futures_cancel(self.short_oid)
                log("BOT", f"Cancelled SHORT LIMIT oid={self.short_oid}", "INFO")
                _db_order_cancel(self._session_id, "SHORT_ENTRY", "SQ_PREP")
            except Exception as e:
                log("BOT", f"Cancel SHORT LIMIT: {e}", "WARN")

        # â"€â"€ Futures TP orders (cancel so market close isn't blocked) â"€â"€â"€â"€â"€â"€â"€â"€â"€
        if self.long_tp_oid and not self.long_tp_done:
            try:
                await futures_cancel(self.long_tp_oid)
                log("BOT", f"Cancelled LONG TP oid={self.long_tp_oid}", "INFO")
                _db_order_cancel(self._session_id, "LONG_TP", "SQ_PREP")
            except Exception as e:
                log("BOT", f"Cancel LONG TP: {e}", "WARN")

        if self.short_tp_oid and not self.short_tp_done:
            try:
                await futures_cancel(self.short_tp_oid)
                log("BOT", f"Cancelled SHORT TP oid={self.short_tp_oid}", "INFO")
                _db_order_cancel(self._session_id, "SHORT_TP", "SQ_PREP")
            except Exception as e:
                log("BOT", f"Cancel SHORT TP: {e}", "WARN")

        log("BOT", "All pending orders cancelled", "OK")

    #
    #  SQUAREOFF  —  15m kline high/low determines TP hit; options at intrinsic
    #
    async def _fetch_spot_at_time(self, ts_ms: int) -> float:
        """
        Fetch BTC perpetual close price from the 1m kline that contains ts_ms.
        Used when squareoff fires late (connection break) to price options at
        the scheduled squareoff time rather than the (delayed) current time.
        Returns 0.0 on failure.
        """
        try:
            rows = await _get(FAPI, "/fapi/v1/klines", {
                "symbol":    "BTCUSDT",
                "interval":  "1m",
                "startTime": ts_ms - 60_000,
                "endTime":   ts_ms + 90_000,
                "limit":     3,
            })
            if not rows:
                return 0.0
            # Last kline whose open time is at or before ts_ms
            valid = [r for r in rows if int(r[0]) <= ts_ms]
            row   = valid[-1] if valid else rows[0]
            return float(row[4])   # close price of that 1m candle
        except Exception as e:
            log("BOT", f"_fetch_spot_at_time failed: {e}", "WARN")
            return 0.0

    async def _fetch_klines_high_low(self, start_ms: int, end_ms: int) -> tuple:
        """
        Fetch 15m BTCUSDT perpetual klines for the session window.
        Returns (session_high, session_low). Falls back to (0, 0) on error.
        """
        try:
            rows = await _get(FAPI, "/fapi/v1/klines", {
                "symbol":    "BTCUSDT",
                "interval":  "15m",
                "startTime": start_ms,
                "endTime":   end_ms,
                "limit":     200,   # 200 × 15 min = 50 h — covers any session
            })
            if not rows:
                return 0.0, 0.0
            highs = [float(r[2]) for r in rows]
            lows  = [float(r[3]) for r in rows]
            return max(highs), min(lows)
        except Exception as e:
            log("BOT", f"klines fetch failed: {e}", "WARN")
            return 0.0, 0.0

    async def _do_squareoff(self):
        if self._sq_done:
            return

        # Spot: live WS first, immediate REST if stale, LKG if fresh (< 120s), else retry
        spot = _price()
        if not spot:
            try:
                def _sq_premium():
                    req = urllib.request.Request(
                        f"{FAPI}/fapi/v1/premiumIndex?symbol=BTCUSDT",
                        headers={"User-Agent": "StraddleBot/1.0"})
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return json.loads(r.read())
                _pm  = await asyncio.get_running_loop().run_in_executor(None, _sq_premium)
                _mp  = float(_pm.get("markPrice") or 0)
                if _mp > 0:
                    _fut_px["mark"]              = _mp
                    globals()["_last_valid_mark"]    = _mp
                    globals()["_last_valid_mark_ts"] = time.time()
                    globals()["_fut_px_update_ms"]   = time.time() * 1000
                    _ws_last_msg["fut"]           = time.time()
                    spot = _mp
                    log("BOT", f"Squareoff: BTC via REST (WS down): {spot:.0f}", "WARN")
            except Exception as _re:
                log("BOT", f"Squareoff: REST fetch failed: {_re}", "WARN")
        if not spot:
            _mk_age_s = (time.time() * 1000 - _fut_px_update_ms) / 1000
            if _mk_age_s < 120 and _last_valid_mark > 0:
                spot = _last_valid_mark
                log("BOT", f"Squareoff: using LKG mark (age={_mk_age_s:.0f}s): {spot:.0f}", "WARN")
        if not spot:
            log("BOT", "Squareoff: no reliable spot — will retry next tick", "WARN")
            return

        # Never substitute bid, ask, or intrinsic for option accounting.
        for sym, is_open in ((self.call_sym, self.call_open), (self.put_sym, self.put_open)):
            if is_open and sym:
                option_mark = _exact_option_mark(sym)
                if option_mark <= 0:
                    log("BOT", f"Squareoff: {sym} mark unavailable; retrying", "WARN")
                    return

        self._sq_done = True

        # Compute scheduled squareoff time so we can detect a late/delayed squareoff
        # (e.g. bot was disconnected during the squareoff window and reconnected after).
        # Reference time: SQUAREOFF_HARD if hard-deadline fired, else SQUAREOFF_START.
        _ref_ts_ms:   int   = 0   # epoch-ms of the scheduled squareoff moment
        _sq_delay_s:  float = 0.0
        if self.entry_ts_ms > 0:
            _edt = datetime.fromtimestamp(self.entry_ts_ms / 1000, tz=IST)
            _rh, _rm = SQUAREOFF_HARD if self._hard_sq_fired else SQUAREOFF_START
            _rdt = _edt.replace(hour=_rh, minute=_rm, second=0, microsecond=0)
            if _rdt <= _edt:
                _rdt += timedelta(days=1)
            _ref_ts_ms  = int(_rdt.timestamp() * 1000)
            _sq_delay_s = (datetime.now(IST) - _rdt).total_seconds()

        log("BOT", f"Squareoff started  delay={_sq_delay_s:.0f}s", "STATE")

        try:
            # Step 1: Cancel all pending orders (entry limits + pre-placed TPs)
            await self._cancel_all_pending_orders()

            # Step 2: One final fill poll — catches fills that landed in the
            # gap between last 5s poll and squareoff (edge case, live mode)
            await self._poll_futures_fills()

            # Close real/paper positions. Accounting below uses current marks.
            if self.long_filled and not self.long_tp_done:
                await futures_market("SELL", self.long_qty, "LONG")
            if self.short_filled and not self.short_tp_done:
                await futures_market("BUY", self.short_qty, "SHORT")
            if self.call_open and self.call_sym:
                await option_order(self.call_sym, "SELL", self.call_qty, order_type="MARKET")
            if self.put_open and self.put_sym:
                await option_order(self.put_sym, "SELL", self.put_qty, order_type="MARKET")

            # Step 3: Determine session high/low for TP check
            # Primary: in-memory running values (0.5s resolution, built during MANAGING)
            high = self.session_high
            low  = self.session_low if self.session_low < float("inf") else 0.0

            # Fallback: klines (covers restarts where in-memory was reset)
            if not high:
                now_ms   = int(time.time() * 1000)
                start_ms = self.entry_ts_ms if self.entry_ts_ms > 0 else now_ms - 86_400_000
                kline_high, kline_low = await self._fetch_klines_high_low(start_ms, now_ms)
                if kline_high:
                    high = kline_high
                    low  = kline_low
                    log("BOT", f"In-memory high/low empty — klines used: high={high:.0f} low={low:.0f}", "WARN")

            # Last resort: pnl_snapshots (if both in-memory and klines fail)
            if not high and _db and self._session_id:
                try:
                    row = _db.execute(
                        "SELECT MAX(btc_mark), MIN(btc_mark) FROM pnl_snapshots"
                        " WHERE session_id=? AND btc_mark > 0",
                        (self._session_id,)
                    ).fetchone()
                    if row:
                        high = float(row[0] or 0)
                        low  = float(row[1] or 0)
                    log("BOT", f"Klines also failed — snapshots used: high={high:.0f} low={low:.0f}", "WARN")
                except Exception as e:
                    log("BOT", f"All high/low sources failed: {e}", "ERROR")

            log("BOT", f"Session high={high:.0f}  low={low:.0f}  spot={spot:.0f}", "INFO")

            # Step 4: LONG leg exit — was TP hit during session?
            if self.long_filled:
                if self.long_tp_done and self.long_sq_exit > 0:
                    log("BOT", f"LONG TP exit retained at mark={self.long_sq_exit:.0f}", "OK")
                elif self.long_tp_px > 0 and high >= self.long_tp_px:
                    self.long_tp_done = True
                    self.long_sq_exit = spot
                    log("BOT", f"LONG TP confirmed; accounting exit mark={spot:.0f}", "OK")
                else:
                    self.long_sq_exit = spot
                    log("BOT", f"LONG timeout exit @ spot={spot:.0f}", "INFO")

            # Step 5: SHORT leg exit
            if self.short_filled:
                if self.short_tp_done and self.short_sq_exit > 0:
                    log("BOT", f"SHORT TP exit retained at mark={self.short_sq_exit:.0f}", "OK")
                elif self.short_tp_px > 0 and low > 0 and low <= self.short_tp_px:
                    self.short_tp_done = True
                    self.short_sq_exit = spot
                    log("BOT", f"SHORT TP confirmed; accounting exit mark={spot:.0f}", "OK")
                else:
                    self.short_sq_exit = spot
                    log("BOT", f"SHORT timeout exit @ spot={spot:.0f}", "INFO")

            # Step 6: Options exit pricing.
            # Primary  : live mark price from _chain (if > 0  →  data aa raha hai)
            # Fallback : intrinsic at current spot   (if mark = 0  →  data nahi aa raha)
            call_strike = float(self.call_sym.split("-")[2]) if self.call_sym else 0.0
            put_strike  = float(self.put_sym.split("-")[2])  if self.put_sym  else 0.0

            for sym, strike, side, _open_attr, _exit_attr in [
                (self.call_sym, call_strike, "C", "call_open", "call_sq_exit"),
                (self.put_sym,  put_strike,  "P", "put_open",  "put_sq_exit"),
            ]:
                if not getattr(self, _open_attr) or not strike:
                    continue
                leg = "CALL" if side == "C" else "PUT"

                mark = _exact_option_mark(sym or "")
                if mark > 0:
                    # Live mark price available — use it
                    setattr(self, _exit_attr, mark)
                    setattr(self, _open_attr, False)
                    log("BOT", f"{leg} closed [mark={mark:.0f}]  delay={_sq_delay_s:.0f}s", "TRADE")
                else:
                    # Mark unavailable — fallback to intrinsic at current spot
                    raise RuntimeError(f"{leg} mark unavailable; refusing non-mark accounting")

            # Step 6.5: Record close orders in orders table (Binance-style — every order logged)
            if _db and self._session_id:
                _sq_ts = _ts()
                try:
                    with _db:
                        # Options exits: always insert — no pre-placed option close order exists
                        if self.call_fill > 0 and self.call_sq_exit > 0:
                            _db.execute(
                                "INSERT INTO orders "
                                "(session_id,paper_order_id,symbol,asset_type,leg_label,"
                                "side,order_type,qty,limit_price,fill_price,status,"
                                "placed_at,filled_at,cancel_reason,updated_at) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (self._session_id, 0, self.call_sym, "option", "CALL_EXIT",
                                 "SELL", "MARKET", self.call_qty, 0, self.call_sq_exit,
                                 "FILLED", _sq_ts, _sq_ts, None, _sq_ts))
                        if self.put_fill > 0 and self.put_sq_exit > 0:
                            _db.execute(
                                "INSERT INTO orders "
                                "(session_id,paper_order_id,symbol,asset_type,leg_label,"
                                "side,order_type,qty,limit_price,fill_price,status,"
                                "placed_at,filled_at,cancel_reason,updated_at) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (self._session_id, 0, self.put_sym, "option", "PUT_EXIT",
                                 "SELL", "MARKET", self.put_qty, 0, self.put_sq_exit,
                                 "FILLED", _sq_ts, _sq_ts, None, _sq_ts))
                        # Futures LONG exit
                        if self.long_filled:
                            _lt = _db.execute(
                                "SELECT status FROM orders"
                                " WHERE session_id=? AND leg_label='LONG_TP'",
                                (self._session_id,)).fetchone()
                            if _lt and _lt[0] == "FILLED":
                                pass  # TP filled during MANAGING — already recorded
                            elif self.long_tp_done and _lt and _lt[0] == "CANCELLED":
                                # TP price passed through session high but was SQ_PREP cancelled
                                # Flip back to FILLED so the order book shows the correct outcome
                                _db.execute(
                                    "UPDATE orders SET status='FILLED',fill_price=?,"
                                    "filled_at=?,cancel_reason=NULL,updated_at=?"
                                    " WHERE session_id=? AND leg_label='LONG_TP'",
                                    (self.long_sq_exit, _sq_ts, _sq_ts, self._session_id))
                            else:
                                # No TP hit — market close at squareoff
                                _db.execute(
                                    "INSERT INTO orders "
                                    "(session_id,paper_order_id,symbol,asset_type,leg_label,"
                                    "side,order_type,qty,limit_price,fill_price,status,"
                                    "placed_at,filled_at,cancel_reason,updated_at) "
                                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                    (self._session_id, 0, "BTCUSDT", "future", "LONG_CLOSE",
                                     "SELL", "MARKET", self.long_qty, 0, self.long_sq_exit,
                                     "FILLED", _sq_ts, _sq_ts, None, _sq_ts))
                        # Futures SHORT exit
                        if self.short_filled:
                            _st = _db.execute(
                                "SELECT status FROM orders"
                                " WHERE session_id=? AND leg_label='SHORT_TP'",
                                (self._session_id,)).fetchone()
                            if _st and _st[0] == "FILLED":
                                pass  # TP filled during MANAGING — already recorded
                            elif self.short_tp_done and _st and _st[0] == "CANCELLED":
                                # TP price passed through session low but was SQ_PREP cancelled
                                _db.execute(
                                    "UPDATE orders SET status='FILLED',fill_price=?,"
                                    "filled_at=?,cancel_reason=NULL,updated_at=?"
                                    " WHERE session_id=? AND leg_label='SHORT_TP'",
                                    (self.short_sq_exit, _sq_ts, _sq_ts, self._session_id))
                            else:
                                # No TP hit — market close at squareoff
                                _db.execute(
                                    "INSERT INTO orders "
                                    "(session_id,paper_order_id,symbol,asset_type,leg_label,"
                                    "side,order_type,qty,limit_price,fill_price,status,"
                                    "placed_at,filled_at,cancel_reason,updated_at) "
                                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                    (self._session_id, 0, "BTCUSDT", "future", "SHORT_CLOSE",
                                     "BUY", "MARKET", self.short_qty, 0, self.short_sq_exit,
                                     "FILLED", _sq_ts, _sq_ts, None, _sq_ts))
                except Exception as _soe:
                    log("BOT", f"Squareoff order records error: {_soe}", "ERROR")

            # Step 7: Final PnL
            opts_pnl = ((self.call_sq_exit - self.call_fill) * self.call_qty
                      + (self.put_sq_exit  - self.put_fill)  * self.put_qty)
            fut_pnl  = 0.0
            if self.long_filled:
                fut_pnl += (self.long_sq_exit   - self.long_entry)   * self.long_qty
            if self.short_filled:
                fut_pnl += (self.short_entry    - self.short_sq_exit) * self.short_qty
            net_pnl = round(opts_pnl + fut_pnl, 4)

            sq_type = ("TP_HIT"     if self.long_tp_done and self.short_tp_done else
                       "PARTIAL_TP" if self.long_tp_done or  self.short_tp_done else
                       "TIMEOUT")

            # Mark remaining NEW orders as cancelled
            if _db and self._session_id:
                try:
                    _db.execute(
                        "UPDATE orders SET status='CANCELLED', cancel_reason='SESSION_END', updated_at=?"
                        " WHERE session_id=? AND status='NEW'",
                        (_ts(), self._session_id))
                    _db.commit()
                except Exception:
                    pass

            # Update paper wallet: add options proceeds + futures PnL (cumulative)
            if PAPER_TRADE:
                global _paper_wallet_balance
                opts_proceeds = (self.call_sq_exit * self.call_qty
                               + self.put_sq_exit  * self.put_qty)
                sq_now = _ts()
                # Ledger: OPTIONS_CREDIT
                _db_wallet_ledger(self._session_id, "OPTIONS_CREDIT",
                                  round(opts_proceeds, 4),
                                  round(_paper_wallet_balance + opts_proceeds, 4),
                                  f"CALL@{self.call_sq_exit:.0f} PUT@{self.put_sq_exit:.0f} qty={self.call_qty}")
                _paper_wallet_balance = round(_paper_wallet_balance + opts_proceeds, 2)
                # Ledger: FUTURES_PNL (can be positive or negative)
                _db_wallet_ledger(self._session_id, "FUTURES_PNL",
                                  round(fut_pnl, 4),
                                  round(_paper_wallet_balance + fut_pnl, 4),
                                  f"sq_type={sq_type} long_sq={self.long_sq_exit:.0f} short_sq={self.short_sq_exit:.0f}")
                _paper_wallet_balance = round(_paper_wallet_balance + fut_pnl, 2)
                _db_set_config("PAPER_WALLET_BALANCE", str(_paper_wallet_balance))
                log("BOT", f"Paper wallet after squareoff: {_paper_wallet_balance:.2f}", "INFO")

            _db_session_update(self._session_id,
                sq_call_exit=self.call_sq_exit, sq_put_exit=self.put_sq_exit,
                sq_long_exit=self.long_sq_exit, sq_short_exit=self.short_sq_exit,
                options_pnl=round(opts_pnl, 4), futures_pnl=round(fut_pnl, 4),
                net_pnl=net_pnl, sq_type=sq_type, end_dt=_ts(), state="DONE",
                wallet_after=round(_paper_wallet_balance, 2))
            _db_event_insert(self._session_id, "SESSION_CLOSED",
                             f"sq={sq_type} opts={opts_pnl:.2f} fut={fut_pnl:.2f} net={net_pnl:.2f} wallet={_paper_wallet_balance:.2f}")

            # Persist: this expiry is done — survives process restart
            _db_set_config("LAST_TRADED_EXPIRY", _expiry_iso())

            log("BOT", f"Squareoff complete  sq_type={sq_type}  net={net_pnl:+.2f}", "STATE")
            await self._send_summary(sq_type, opts_pnl, fut_pnl, net_pnl)

        except Exception as e:
            log("BOT", f"_do_squareoff error: {e}", "ERROR")
            asyncio.create_task(_tg(f"⚠ Squareoff error — session may need manual review:\n{e}"))
        finally:
            self.state = "DONE"
            asyncio.create_task(_fetch_chain())   # immediately load next expiry chain
            log("SYS", "Chain refresh triggered post-squareoff — next expiry loading", "OK")

    #
    #  STATUS DETAIL (written to DB every tick for dashboard diagnostics)
    #
    def _build_status_detail(self) -> dict:
        now     = datetime.now(IST)
        now_min = _to_min(now.hour, now.minute)
        spot    = _price()
        win_open    = _in_entry_window(now_min)
        _fut_stale  = _feed_age_s("fut") > 10
        _opt_stale  = _feed_age_s("opt") > 30 and (time.time() - _rest_last_ok) > 30
        data_ready  = bool(_all_strikes and spot and not _fut_stale and not _opt_stale)

        exp_date_str   = ""
        hours_to_expiry = -1.0
        within_24h      = False
        if _expiry_ms:
            exp_date_str    = datetime.fromtimestamp(_expiry_ms / 1000, tz=IST).strftime("%Y-%m-%d %H:%M")
            hours_to_expiry = (_expiry_ms - int(time.time() * 1000)) / 3_600_000
            within_24h      = 0 < hours_to_expiry < 24

        d: dict = {
            "window_open":      win_open,
            "within_24h":       within_24h,
            "hours_to_expiry":  round(hours_to_expiry, 1) if hours_to_expiry >= 0 else None,
            "data_ready":       data_ready,
            "spot":           round(spot, 1) if spot else 0,
            "window_start":   f"{WINDOW_START[0]:02d}:{WINDOW_START[1]:02d}",
            "window_end":     f"{WINDOW_END[0]:02d}:{WINDOW_END[1]:02d}",
            "sq_start":       f"{SQUAREOFF_START[0]:02d}:{SQUAREOFF_START[1]:02d}",
            "sq_hard":        f"{SQUAREOFF_HARD[0]:02d}:{SQUAREOFF_HARD[1]:02d}",
            "fut_entry_cutoff": f"{FUTURES_ENTRY_CUTOFF[0]:02d}:{FUTURES_ENTRY_CUTOFF[1]:02d}",
            "fut_sq_hard":    f"{FUTURES_SQUAREOFF[0]:02d}:{FUTURES_SQUAREOFF[1]:02d}",
            "expiry_date":    exp_date_str,
            "already_traded": bool(
                (self.sess_date or _db_get_config("LAST_TRADED_EXPIRY") or "") == _expiry_iso()
            ),
            "fut_feed_age_s":   round(_feed_age_s("fut"), 1),
            "opt_feed_age_s":   round(_feed_age_s("opt"), 1),
            "rest_fail_streak": _rest_fail_streak,
            "rest_last_ok_s":   round(time.time() - _rest_last_ok, 1) if _rest_last_ok > 0 else None,
            "min_expiry_h":   MIN_EXPIRY_HOURS,
            "min_strike_gap": MIN_STRIKE_GAP,
            "max_total_mark": MAX_TOTAL_MARK,
            "max_premium_gap": MAX_PREMIUM_GAP,
            "options_recovery_pct": OPTIONS_RECOVERY_PCT,
            "valuation":      "mark_only",
            "trade_qty":      TRADE_QTY,
            "paper_wallet":   round(_paper_wallet_balance, 2),
            "paper_trade":    PAPER_TRADE,
        }

        if self.state == "SLEEP":
            reasons = []
            if not win_open:        reasons.append("window closed")
            if not within_24h:      reasons.append("expiry > 24h away")
            if _fut_stale:          reasons.append(f"futures feed stale {d['fut_feed_age_s']:.0f}s")
            if _opt_stale:          reasons.append(f"options feed stale {d['opt_feed_age_s']:.0f}s")
            if not _all_strikes:    reasons.append("chain not loaded")
            elif not spot:          reasons.append("no spot price")
            if d["already_traded"]: reasons.append("already traded today")
            d["sleep_reason"] = ", ".join(reasons) if reasons else "waiting"

        if self.state in ("SLEEP", "SCANNING") and data_ready and spot:
            try:
                pair = _find_itm_pair(spot)
                if pair:
                    call_opt, put_opt = pair
                    now_ms = int(time.time() * 1000)
                    c_mark = call_opt.get("mark")    or 0.0
                    p_mark = put_opt.get("mark")     or 0.0
                    gap    = put_opt["strike"] - call_opt["strike"]
                    h_left = (_expiry_ms - now_ms) / 3_600_000 if _expiry_ms else 0.0
                    d["conditions"] = {
                        "call_sym":       call_opt["symbol"],
                        "put_sym":        put_opt["symbol"],
                        "expiry_h":       round(h_left, 1),
                        "expiry_ok":      h_left >= MIN_EXPIRY_HOURS,
                        "gap":            round(gap, 0),
                        "gap_ok":         gap >= MIN_STRIKE_GAP,
                        "call_mark":      round(c_mark, 0),
                        "put_mark":       round(p_mark, 0),
                        "premium_gap":    round(abs(c_mark - p_mark), 0),
                        "premium_gap_ok": abs(c_mark - p_mark) <= MAX_PREMIUM_GAP,
                        "total_mark":     round(c_mark + p_mark, 0),
                        "total_mark_ok":  (c_mark + p_mark) <= MAX_TOTAL_MARK,
                        "valuation":      "mark_only",
                    }
            except Exception:
                pass

        # Live per-leg UPnL — computed every tick so panel updates at 1s resolution.
        # Only emitted when at least one mark price is available; zero-mark state is
        # NOT broadcast so the dashboard doesn't overwrite the stored snapshot UPnL.
        if self.state == "MANAGING" and (self.call_fill > 0 or self.put_fill > 0):
            _sp = _price_safe()
            _cm = _exact_option_mark(self.call_sym)
            _pm = _exact_option_mark(self.put_sym)
            _options_fresh = ((not self.call_open or self.call_fill <= 0 or _cm > 0)
                              and (not self.put_open or self.put_fill <= 0 or _pm > 0))
            _futures_fresh = (not (self.long_filled or self.short_filled) or _sp > 0)
            if _options_fresh and _futures_fresh:
                _cu = round((_cm - self.call_fill)  * self.call_qty,  4) if _cm > 0 else 0.0
                _pu = round((_pm - self.put_fill)   * self.put_qty,   4) if _pm > 0 else 0.0
                _lu = round((_sp - self.long_entry) * self.long_qty,  4) if _sp and self.long_entry  > 0 else 0.0
                _su = round((self.short_entry - _sp)* self.short_qty, 4) if _sp and self.short_entry > 0 else 0.0
                d["live_upnl"] = {
                    "call":  _cu,
                    "put":   _pu,
                    "long":  _lu,
                    "short": _su,
                    "total": round(_cu + _pu + _lu + _su, 4),
                }
                # Authoritative current position for the dashboard.  Exact-zero
                # UPnL at entry is valid and must not look like missing data.
                d["live_position"] = {
                    "call_entry": self.call_fill,
                    "put_entry": self.put_fill,
                    "call_qty": self.call_qty,
                    "put_qty": self.put_qty,
                    "call_mark": _cm,
                    "put_mark": _pm,
                    "call_upnl": _cu,
                    "put_upnl": _pu,
                    "long_entry": self.long_entry if self.long_filled else 0.0,
                    "short_entry": self.short_entry if self.short_filled else 0.0,
                    "long_qty": self.long_qty if self.long_filled else 0.0,
                    "short_qty": self.short_qty if self.short_filled else 0.0,
                    "btc_mark": _sp,
                    "long_upnl": _lu,
                    "short_upnl": _su,
                    "total_upnl": round(_cu + _pu + _lu + _su, 4),
                }

        return d

    #
    #  STATUS LOG
    #
    def _log_status(self):
        spot = _price()
        if self.state == "SCANNING" and not self.call_sym:
            # Show actual market data for the ITM pair being scanned (not position data)
            pair = _find_itm_pair(spot) if spot else None
            if pair:
                cd, pd = pair
                log("BOT",
                    f"[SCANNING]  spot={spot:.0f}  "
                    f"CALL {cd['symbol']} mark={cd.get('mark') or 0:.0f}  "
                    f"PUT {pd['symbol']} mark={pd.get('mark') or 0:.0f}", "INFO")
            else:
                log("BOT", f"[SCANNING]  spot={spot:.0f}  waiting for chain data", "INFO")
            return
        c_mark = _exact_option_mark(self.call_sym)
        p_mark = _exact_option_mark(self.put_sym)
        log("BOT",
            f"[{self.state}]  spot={spot:.0f}  "
            f"CALL mark={c_mark:.0f}  PUT mark={p_mark:.0f}", "INFO")

    async def _send_summary(self, sq_type: str, opts_pnl: float, fut_pnl: float, net_pnl: float):
        """Send final P&L summary to Telegram."""
        tag  = {"TP_HIT": "✅", "PARTIAL_TP": "⚡", "TIMEOUT": "⏱"}.get(sq_type, "📋")
        sign = "+" if net_pnl >= 0 else ""

        call_line = (f"  CALL  buy={self.call_fill:.0f}  exit={self.call_sq_exit:.0f}"
                     f"  pnl={(self.call_sq_exit - self.call_fill) * self.call_qty:+.2f}")
        put_line  = (f"  PUT   buy={self.put_fill:.0f}  exit={self.put_sq_exit:.0f}"
                     f"  pnl={(self.put_sq_exit - self.put_fill) * self.put_qty:+.2f}")

        long_line  = (f"  LONG  entry={self.long_entry:.0f}  exit={self.long_sq_exit:.0f}"
                      f"  {'TP ✓' if self.long_tp_done else 'TIMEOUT'}"
                      f"  pnl={(self.long_sq_exit - self.long_entry) * self.long_qty:+.2f}"
                      if self.long_filled else "  LONG  not filled")
        short_line = (f"  SHORT entry={self.short_entry:.0f}  exit={self.short_sq_exit:.0f}"
                      f"  {'TP ✓' if self.short_tp_done else 'TIMEOUT'}"
                      f"  pnl={(self.short_entry - self.short_sq_exit) * self.short_qty:+.2f}"
                      if self.short_filled else "  SHORT not filled")

        await _tg(
            f"{tag} <b>Session Closed — {sq_type}</b>\n"
            f"──────────────────────────────────────────\n"
            f"Entry : {self.entry_ts} IST\n\n"
            f"<b>Options</b>  (prem paid = {self.total_prem:.0f})\n"
            f"{call_line}\n"
            f"{put_line}\n"
            f"  Opts PnL : <b>{opts_pnl:+.2f} USDT</b>\n\n"
            f"<b>Futures</b>\n"
            f"{long_line}\n"
            f"{short_line}\n"
            f"  Fut  PnL : <b>{fut_pnl:+.2f} USDT</b>\n\n"
            f"<b>Net PnL  : {sign}{net_pnl:.2f} USDT</b>"
        )

    #
    #  STATE RESTORE  -  called on restart when open positions detected
    #

    async def _restore_from_db(self) -> bool:
        """Paper-mode restore: rebuild bot state from the most recent ACTIVE session."""
        if not _db:
            return False
        sess = _db.execute(
            "SELECT * FROM sessions WHERE state='ACTIVE' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not sess:
            log("BOT", "[PAPER] No active session in DB", "WARN")
            return False

        self._session_id = sess["id"]
        self.call_sym    = sess["call_sym"]   or ""
        self.put_sym     = sess["put_sym"]    or ""
        self.call_fill   = sess["call_fill"]  or 0.0
        self.put_fill    = sess["put_fill"]   or 0.0
        self.call_qty    = sess["call_qty"]   or TRADE_QTY
        self.put_qty     = sess["put_qty"]    or TRADE_QTY
        self.call_open   = True
        self.put_open    = True
        self.total_prem  = (self.call_fill + self.put_fill)
        self.long_entry  = sess["long_entry"]  or 0.0
        self.short_entry = sess["short_entry"] or 0.0
        self.long_qty    = sess["long_qty"]    or TRADE_QTY
        self.short_qty   = sess["short_qty"]   or TRADE_QTY
        self.long_filled  = self.long_entry  > 0
        self.short_filled = self.short_entry > 0
        self.long_tp_px   = sess["long_tp_px"]   or 0.0
        self.short_tp_px  = sess["short_tp_px"]  or 0.0
        # Preserve the expiry identity stored with this trade. On reconnect the
        # scanner may already have rolled to the next chain.
        saved_expiry_dt = str(sess["expiry_dt"] or "")
        if saved_expiry_dt:
            self.sess_date = saved_expiry_dt[:10]
        elif sess["expiry_sym"]:
            try:
                self.sess_date = datetime.strptime(str(sess["expiry_sym"]), "%y%m%d").date().isoformat()
            except ValueError:
                self.sess_date = ""
        else:
            self.sess_date = ""
        self.entry_ts     = "restored"
        self.entry_ts_ms  = int(sess["entry_ts_ms"] or 0)

        # Restore paper order IDs from orders table and re-register in _paper_orders
        # so _paper_fill_monitor() can re-evaluate pending fills after restart.
        orders = _db.execute(
            "SELECT * FROM orders WHERE session_id=?", (self._session_id,)
        ).fetchall()
        for o in orders:
            label = o["leg_label"]
            oid   = o["paper_order_id"]
            px    = float(o["limit_price"] or 0)
            if not oid:
                continue
            st = o["status"]
            if label == "LONG_ENTRY" and st == "NEW":
                self.long_oid       = oid
                self.long_limit_px  = px
                _paper_orders[oid]  = {
                    "orderId": oid, "symbol": "BTCUSDT", "side": "BUY",
                    "type": "LIMIT", "qty": self.long_qty, "price": px,
                    "status": "NEW", "fill_price": 0.0,
                    "asset": "future", "pos_side": "LONG", "placed_at": time.time(),
                }
            elif label == "SHORT_ENTRY" and st == "NEW":
                self.short_oid      = oid
                self.short_limit_px = px
                _paper_orders[oid]  = {
                    "orderId": oid, "symbol": "BTCUSDT", "side": "SELL",
                    "type": "LIMIT", "qty": self.short_qty, "price": px,
                    "status": "NEW", "fill_price": 0.0,
                    "asset": "future", "pos_side": "SHORT", "placed_at": time.time(),
                }
            elif label == "LONG_TP" and st == "NEW":
                self.long_tp_oid    = oid
                self.long_tp_px     = px
                _paper_orders[oid]  = {
                    "orderId": oid, "symbol": "BTCUSDT", "side": "SELL",
                    "type": "LIMIT", "qty": self.long_qty, "price": px,
                    "status": "NEW", "fill_price": 0.0,
                    "asset": "future", "pos_side": "LONG", "placed_at": time.time(),
                }
            elif label == "SHORT_TP" and st == "NEW":
                self.short_tp_oid   = oid
                self.short_tp_px    = px
                _paper_orders[oid]  = {
                    "orderId": oid, "symbol": "BTCUSDT", "side": "BUY",
                    "type": "LIMIT", "qty": self.short_qty, "price": px,
                    "status": "NEW", "fill_price": 0.0,
                    "asset": "future", "pos_side": "SHORT", "placed_at": time.time(),
                }


        # Restore session high/low from pnl_snapshots so in-memory tracking continues correctly
        try:
            hl = _db.execute(
                "SELECT MAX(btc_mark), MIN(btc_mark) FROM pnl_snapshots"
                " WHERE session_id=? AND btc_mark > 0",
                (self._session_id,)
            ).fetchone()
            if hl and hl[0]:
                self.session_high = float(hl[0])
                self.session_low  = float(hl[1])
                log("BOT", f"Session high/low restored: high={self.session_high:.0f} low={self.session_low:.0f}", "INFO")
        except Exception:
            pass

        # Set the correct run state — MANAGING handles polling, PnL snapshots, and squareoff
        self.state = "MANAGING"
        log("BOT", f"[PAPER] Session {self._session_id} restored -> MANAGING  "
                   f"CALL={self.call_sym} PUT={self.put_sym}  "
                   f"long_oid={self.long_oid} short_oid={self.short_oid}  "
                   f"long_filled={self.long_filled} short_filled={self.short_filled}  "
                   f"paper_orders_restored={len(_paper_orders)}", "OK")
        _db_event_insert(self._session_id, "SESSION_RESTORED",
                         f"restarted  paper_orders={len(_paper_orders)}"
                         f" long_filled={self.long_filled} short_filled={self.short_filled}")

        # Write immediate PnL snapshot so dashboard Data timestamp is fresh
        self._write_pnl_snapshot()
        return True

    async def restore_from_exchange(self) -> bool:
        """
        Read open positions and rebuild bot state. Paper mode: from MariaDB.
        Live mode: from Binance APIs. Returns True if any position was found.
        """
        if PAPER_TRADE:
            return await self._restore_from_db()

        log("BOT", "Restoring state from Binance...", "INFO")
        found = False

        # â"€â"€ Open option positions â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
        try:
            rows = await _get(EAPI, "/eapi/v1/position")
            for p in (rows if isinstance(rows, list) else []):
                sym = p.get("symbol", "")
                qty = float(p.get("quantity") or 0)
                if not sym.startswith("BTC-") or qty <= 0:
                    continue
                fill = float(p.get("entryPrice") or 0)
                if sym.endswith("-C"):
                    self.call_sym  = sym;  self.call_fill = fill
                    self.call_qty  = qty;  self.call_open = True
                    found = True
                    log("BOT", f"  CALL restored: {sym} @ {fill:.0f}", "WARN")
                elif sym.endswith("-P"):
                    self.put_sym   = sym;  self.put_fill  = fill
                    self.put_qty   = qty;  self.put_open  = True
                    found = True
                    log("BOT", f"  PUT  restored: {sym} @ {fill:.0f}", "WARN")
        except Exception as e:
            log("BOT", f"Restore options query: {e}", "WARN")

        # â"€â"€ Open futures positions â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
        try:
            rows = await _get(FAPI, "/fapi/v2/positionRisk", {"symbol": "BTCUSDT"})
            for p in rows:
                ps  = p.get("positionSide")
                amt = abs(float(p.get("positionAmt") or 0))
                if amt < 0.001:
                    continue
                entry = float(p.get("entryPrice") or 0)
                if ps == "LONG":
                    self.long_filled = True;  self.long_entry = entry;  self.long_qty = amt
                    found = True
                    log("BOT", f"  LONG futures restored @ {entry:.0f}  qty={amt}", "WARN")
                elif ps == "SHORT":
                    self.short_filled = True; self.short_entry = entry; self.short_qty = amt
                    found = True
                    log("BOT", f"  SHORT futures restored @ {entry:.0f}  qty={amt}", "WARN")
        except Exception as e:
            log("BOT", f"Restore futures query: {e}", "WARN")

        if not found:
            log("BOT", "Restore: no open positions found on Binance", "INFO")
            return False

        # â"€â"€ Classify and restore open futures orders â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
        try:
            orders = await _get(FAPI, "/fapi/v1/openOrders", {"symbol": "BTCUSDT"})
            for o in (orders if isinstance(orders, list) else []):
                oid      = int(o.get("orderId", 0))
                side     = o.get("side", "")
                pos_side = o.get("positionSide", "")
                price    = float(o.get("price") or 0)
                if not oid:
                    continue
                if pos_side == "LONG" and self.long_filled:
                    if side == "SELL":
                        # GTC TP order still live  -  restore it
                        self.long_tp_oid = oid
                        self.long_tp_px  = price
                        log("BOT", f"  Restored LONG TP oid={oid} @ {price:.0f}", "INFO")
                    else:
                        # Stale entry limit (position already filled)  -  cancel
                        try:
                            await futures_cancel(oid)
                            log("BOT", f"  Cancelled stale LONG entry limit oid={oid}", "INFO")
                        except Exception:
                            pass
                elif pos_side == "SHORT" and self.short_filled:
                    if side == "BUY":
                        # GTC TP order still live  -  restore it
                        self.short_tp_oid = oid
                        self.short_tp_px  = price
                        log("BOT", f"  Restored SHORT TP oid={oid} @ {price:.0f}", "INFO")
                    else:
                        # Stale entry limit (position already filled)  -  cancel
                        try:
                            await futures_cancel(oid)
                            log("BOT", f"  Cancelled stale SHORT entry limit oid={oid}", "INFO")
                        except Exception:
                            pass
                else:
                    # No corresponding open position  -  orphaned order, cancel it
                    try:
                        await futures_cancel(oid)
                        log("BOT", f"  Cancelled orphaned futures order oid={oid}", "INFO")
                    except Exception:
                        pass
        except Exception as e:
            log("BOT", f"Restore: futures orders: {e}", "WARN")

        # â"€â"€ Cancel any stale open option orders (no protection orders in use) â"€â"€â"€â"€
        for sym in [s for s in [self.call_sym, self.put_sym] if s]:
            try:
                opt_orders = await _get(EAPI, "/eapi/v1/openOrders", {"symbol": sym})
                for o in (opt_orders if isinstance(opt_orders, list) else []):
                    oid  = int(o.get("orderId", 0))
                    side = o.get("side", "")
                    if oid and side == "SELL":
                        try:
                            await option_cancel(sym, oid)
                            log("BOT", f"  Cancelled stale option SELL order {sym} oid={oid}", "INFO")
                        except Exception:
                            pass
            except Exception as e:
                log("BOT", f"Restore: option orders {sym}: {e}", "WARN")

        # â"€â"€ Place TP for any filled futures that have no live TP order â"€â"€â"€â"€â"€â"€â"€â"€
        if self.long_filled and not self.long_tp_oid and not self.long_tp_done:
            try:
                _cs = float(self.call_sym.split("-")[2]) if self.call_sym else 0
                _ps = float(self.put_sym.split("-")[2])  if self.put_sym  else 0
                _gap = _ps - _cs; _tv = self.total_prem - _gap
                tp_px = round(self.long_entry + self.total_prem + _tv, 1)
                self.long_tp_px = tp_px
                r = await futures_limit("SELL", self.long_qty, tp_px, "LONG")
                self.long_tp_oid = int(r.get("orderId", 0))
                log("BOT", f"  Placed missing LONG TP @ {tp_px:.0f}  oid={self.long_tp_oid}", "OK")
                await _tg(f"⚡ <b>Restore: LONG TP placed</b>\nEntry @ {self.long_entry:.0f}\nTP @ {tp_px:.0f}")
            except Exception as e:
                log("BOT", f"  LONG TP placement failed on restore: {e}", "ERROR")
                await _tg(f" Restore: LONG TP failed:\n{e}")

        if self.short_filled and not self.short_tp_oid and not self.short_tp_done:
            try:
                _cs = float(self.call_sym.split("-")[2]) if self.call_sym else 0
                _ps = float(self.put_sym.split("-")[2])  if self.put_sym  else 0
                _gap = _ps - _cs; _tv = self.total_prem - _gap
                tp_px = round(self.short_entry - (self.total_prem + _tv), 1)
                self.short_tp_px = tp_px
                r = await futures_limit("BUY", self.short_qty, tp_px, "SHORT")
                self.short_tp_oid = int(r.get("orderId", 0))
                log("BOT", f"  Placed missing SHORT TP @ {tp_px:.0f}  oid={self.short_tp_oid}", "OK")
                await _tg(f"⚡ <b>Restore: SHORT TP placed</b>\nEntry @ {self.short_entry:.0f}\nTP @ {tp_px:.0f}")
            except Exception as e:
                log("BOT", f"  SHORT TP placement failed on restore: {e}", "ERROR")
                await _tg(f" Restore: SHORT TP failed:\n{e}")

        # â"€â"€ Rebuild derived state â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
        self.total_prem = (self.call_fill or 0.0) + (self.put_fill or 0.0)
        self.sess_date  = _expiry_iso()
        self.entry_ts   = "restored"

        # â"€â"€ Set state based on current time â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
        now = datetime.now(IST)
        past_sq = False
        if self.entry_ts_ms and self.entry_ts_ms > 0:
            # Use actual entry time to compute squareoff deadline — handles overnight sessions
            entry_dt = datetime.fromtimestamp(self.entry_ts_ms / 1000, tz=IST)
            sq_h, sq_m = SQUAREOFF_START
            sq_dt = entry_dt.replace(hour=sq_h, minute=sq_m, second=0, microsecond=0)
            if sq_dt <= entry_dt:
                sq_dt += timedelta(days=1)
            past_sq = now >= sq_dt
        else:
            # Fallback: calendar-date check (works for same-day sessions)
            past_sq = _is_expiry_day() and _to_min(now.hour, now.minute) >= _to_min(*SQUAREOFF_START)
        if past_sq:
            # Squareoff window already passed — run squareoff immediately
            asyncio.create_task(self._do_squareoff())
        self.state = "MANAGING"   # _do_squareoff() will flip to DONE when complete

        log("BOT", f"State restored -> {self.state}", "OK")
        return True


# ╔╗
# â•'                        STARTUP POSITION CHECK                             â•'
# ╚

# Set to True if Binance reports open positions at startup.
# _tick_sleep() refuses to transition to SCANNING while this is True,
# preventing a fresh entry on top of an unrecognised existing position.
_has_open_positions: bool = False


async def _check_existing_positions():
    """
    Check for open positions on startup. In paper mode, checks MariaDB for an
    active session. In live mode, queries Binance APIs.
    """
    global _has_open_positions

    if PAPER_TRADE:
        if _db:
            row = _db.execute(
                "SELECT id FROM sessions WHERE state='ACTIVE' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                _has_open_positions = True
                log("SYS", f"[PAPER] Active session {row[0]} found in DB  -  will restore state", "WARN")
            else:
                log("SYS", "[PAPER] No active session in DB  -  clean start", "OK")
        return

    found_futures = False
    found_options = False

    try:
        rows = await _get(FAPI, "/fapi/v2/positionRisk", {"symbol": "BTCUSDT"})
        open_fut = [p for p in rows if abs(float(p.get("positionAmt") or 0)) > 0.001]
        if open_fut:
            found_futures = True
            for p in open_fut:
                log("SYS",
                    f"  FUTURES {p.get('positionSide','?')} "
                    f"amt={p.get('positionAmt')}  entry={p.get('entryPrice')}  "
                    f"liq={p.get('liquidationPrice')}", "CRIT")
    except Exception as e:
        log("SYS", f"Futures position check failed: {e}", "WARN")

    try:
        rows = await _get(EAPI, "/eapi/v1/position")
        if isinstance(rows, list):
            open_opt = [p for p in rows
                        if p.get("symbol", "").startswith("BTC-")
                        and float(p.get("quantity") or 0) > 0]
            if open_opt:
                found_options = True
                for p in open_opt:
                    log("SYS",
                        f"  OPTION {p.get('symbol')} "
                        f"qty={p.get('quantity')}  entry={p.get('entryPrice')}", "CRIT")
    except Exception as e:
        log("SYS", f"Option position check failed: {e}", "WARN")

    if found_futures or found_options:
        _has_open_positions = True
        log("SYS", "Open positions detected  -  will restore state after YES", "WARN")
    else:
        log("SYS", "No open positions  -  clean start", "OK")


# ╔╗
# â•'                        SINGLE INSTANCE LOCK                               â•'
# ╚
_lock_sock: Optional[socket.socket] = None

def _acquire_lock():
    global _lock_sock
    _lock_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _lock_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # allow rebind after crash
    try:
        _lock_sock.bind(("127.0.0.1", 19877))
        _lock_sock.listen(1)
    except OSError:
        print("\n⚠  straddle_trader.py is already running (port 19877 busy). Exiting.\n")
        sys.exit(1)


# ╔╗
# â•'                        STARTUP TELEGRAM MESSAGE                           â•'
# ╚
def _fmt_hm(total_mins: int) -> str:
    """Format a minute count as '2h 05m' or '45m'."""
    if total_mins <= 0:
        return "now"
    h, m = divmod(int(total_mins), 60)
    return f"{h}h {m:02d}m" if h > 0 else f"{m}m"

def _build_startup_msg() -> str:
    """
    Build a comprehensive startup Telegram message.
    Called after _fetch_chain() so _expiry_ms is populated.
    Shows session name, window open/close countdown, squareoff countdown.
    """
    now       = datetime.now(IST)
    now_min   = _to_min(now.hour, now.minute)
    start_min = _to_min(*WINDOW_START)
    end_min   = _to_min(*WINDOW_END)
    overnight = start_min > end_min   # e.g. 23:00 -> 05:00

    # â"€â"€ Session / expiry â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    if _expiry_ms:
        exp_dt      = datetime.fromtimestamp(_expiry_ms / 1000, tz=IST)
        session_str = f" Session  : {exp_dt.strftime('%d %b %Y')}  |  Expiry {exp_dt.strftime('%H:%M')} IST"

        sq_dt   = exp_dt.replace(hour=SQUAREOFF_START[0], minute=SQUAREOFF_START[1], second=0, microsecond=0)
        hard_dt = exp_dt.replace(hour=SQUAREOFF_HARD[0],  minute=SQUAREOFF_HARD[1],  second=0, microsecond=0)
        mins_to_sq   = max(0, int((sq_dt   - now).total_seconds() / 60))
        mins_to_hard = max(0, int((hard_dt - now).total_seconds() / 60))
        sq_str   = f"  {SQUAREOFF_START[0]:02d}:{SQUAREOFF_START[1]:02d} IST  -  in {_fmt_hm(mins_to_sq)}"
        hard_str = f"  Hard stop  : {SQUAREOFF_HARD[0]:02d}:{SQUAREOFF_HARD[1]:02d} IST  -  in {_fmt_hm(mins_to_hard)}"
    else:
        session_str = " Session  : loading..."
        sq_str   = f"  {SQUAREOFF_START[0]:02d}:{SQUAREOFF_START[1]:02d} IST"
        hard_str = f"  Hard stop  : {SQUAREOFF_HARD[0]:02d}:{SQUAREOFF_HARD[1]:02d} IST"

    # â"€â"€ Entry window countdown â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    if _in_entry_window(now_min):
        if not overnight:
            mins_to_close = end_min - now_min
        else:
            mins_to_close = ((24 * 60 - now_min) + end_min) if now_min >= start_min else (end_min - now_min)
        win_str = (
            f"  {WINDOW_START[0]:02d}:{WINDOW_START[1]:02d} -> {WINDOW_END[0]:02d}:{WINDOW_END[1]:02d} IST\n"
            f"  🟢 OPEN  -  closes in {_fmt_hm(mins_to_close)}"
        )
    else:
        if not overnight:
            mins_to_open = (start_min - now_min) if now_min < start_min else ((24 * 60 - now_min) + start_min)
        else:
            mins_to_open = start_min - now_min   # between end_min and start_min
        win_str = (
            f"  {WINDOW_START[0]:02d}:{WINDOW_START[1]:02d} -> {WINDOW_END[0]:02d}:{WINDOW_END[1]:02d} IST\n"
            f"   Closed  -  opens in {_fmt_hm(mins_to_open)}"
        )

    return (
        f"🚀 <b>Straddle Bot LIVE</b>\n"
        f"─────────────────────────────────────────────────────────────────────\n"
        f"{session_str}\n\n"
        f" <b>Entry Window</b>\n"
        f"{win_str}\n\n"
        f"🎯 <b>Squareoff</b>\n"
        f"{sq_str}\n"
        f"{hard_str}\n\n"
        f" <b>Entry Conditions</b>\n"
        f"  Qty / leg      : {TRADE_QTY} BTC\n"
        f"  Min expiry     : {MIN_EXPIRY_HOURS:.0f}h\n"
        f"  Min strike gap : {MIN_STRIKE_GAP} USDT\n"
        f"  Max combined mark premium : {MAX_TOTAL_MARK} USDT\n\n"
        f"🔔 <b>Levels</b>\n"
        f"  LONG  = PUT_strike âˆ' total_prem\n"
        f"  SHORT = CALL_strike + total_prem\n"
        f"  Futures TP : entry ± (prem + TV_at_entry)\n"
        f"  Liq prot   : immediate on fill @ intrinsic at liq price\n"
        f"─────────────────────────────────────────────────────────────────────\n"
    )


# ╔╗
# â•'                        MAIN                                               â•'
# ╚
async def main():
    global _active_bot
    _acquire_lock()
    _db_init()
    _db_load_config()

    # Log uncaught exceptions from any asyncio task so they're never silently dropped
    def _handle_task_exception(loop, context):
        exc = context.get("exception")
        msg = context.get("message", "unknown asyncio error")
        if exc:
            log("SYS", f"Unhandled task exception: {exc!r}\n{msg}", "ERROR")
        else:
            log("SYS", f"Asyncio error: {msg}", "ERROR")
    _loop = asyncio.get_running_loop()
    _loop.set_exception_handler(_handle_task_exception)
    _loop.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="rest")
    )

    separator("BTC ITM Straddle Bot")

    # Load chain first so expiry date is known before we print or Telegram anything
    log("SYS", "Fetching options chain...", "INFO")
    await _fetch_chain()

    # Check for existing positions
    await _check_existing_positions()

    # Terminal confirm prompt
    print()
    print(f"  Session      : {datetime.fromtimestamp(_expiry_ms/1000, tz=IST).strftime('%d %b %Y  Expiry %H:%M IST') if _expiry_ms else 'unknown'}")
    print(f"  Entry window : {WINDOW_START[0]:02d}:{WINDOW_START[1]:02d} -> "
          f"{WINDOW_END[0]:02d}:{WINDOW_END[1]:02d} IST")
    print(f"  SQ window    : {SQUAREOFF_START[0]:02d}:{SQUAREOFF_START[1]:02d} -> "
          f"{SQUAREOFF_HARD[0]:02d}:{SQUAREOFF_HARD[1]:02d} IST")
    print(f"  Qty          : {TRADE_QTY} BTC")
    print(f"  Combined mark <= : {MAX_TOTAL_MARK} USDT")
    print()
    if _UNATTENDED:
        print("  Unattended mode (--yes)  -  starting automatically.\n")
    else:
        confirm = input("  Type YES to start: ").strip().upper()
        if confirm != "YES":
            print("  Aborted."); sys.exit(0)
        print()

    # Send rich startup message to Telegram (chain loaded -> expiry + countdowns are accurate)
    await _tg(_build_startup_msg())

    bot = StraddleBot()
    _active_bot = bot

    # If positions were found at startup, restore state from Binance
    # instead of blocking in SLEEP  -  bot resumes MANAGING (or fires squareoff if past SQ time)
    if _has_open_positions:
        restored = await bot.restore_from_exchange()
        if not restored:
            # Couldn't confirm positions via API  -  stay blocked in SLEEP for safety
            log("SYS",
                "⛔ Position restore failed  -  staying in SLEEP to prevent double entry.",
                "CRIT")
            await _tg(
                "⛔ <b>Position restore FAILED</b>\n"
                "Could not read open positions from Binance.\n"
                "Bot locked in SLEEP. Close all positions manually and restart."
            )

    # Start all background tasks
    tasks = [
        asyncio.create_task(_supervised(_ticker_loop,        "options_api")),
        asyncio.create_task(_supervised(_chain_refresh_loop, "chain_refresh")),
        asyncio.create_task(_supervised(_fapi_mark_fallback, "futures_mark_api")),
        asyncio.create_task(bot.run()),
    ]
    if PAPER_TRADE:
        tasks.append(asyncio.create_task(_paper_fill_monitor()))

    # SIGTERM handler  -  systemctl stop sends SIGTERM, not SIGINT
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, lambda: [t.cancel() for t in tasks])
    except NotImplementedError:
        pass   # Windows: add_signal_handler not supported

    log("SYS", "All tasks running. Ctrl+C to stop.", "OK")
    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log("SYS", "Shutting down...", "WARN")
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await _tg(f"{_PTAG}🛑 <b>Straddle Bot stopped</b>\nProcess terminated (manual stop or system shutdown).")
        except Exception:
            pass
        if _lock_sock:
            _lock_sock.close()
        _active_bot = None


async def handle_runtime_command(command: str) -> dict:
    """Execute a command already authenticated and claimed through Frappe."""
    global _control_paused
    command = str(command or "").upper()
    if command == "PAUSE":
        _control_paused = True
        return {"status": "paused", "entry_blocked": True}
    if command == "RESUME":
        _control_paused = False
        return {"status": "running", "entry_blocked": False}
    if command in {"FORCE_CLOSE", "SQUARE_OFF", "EMERGENCY_SQUARE_OFF"}:
        if _active_bot is None:
            raise RuntimeError("Straddle runtime is not ready")
        if _active_bot.state in ("SLEEP", "DONE") and not _active_bot._session_id:
            return {"status": "no_position", "state": _active_bot.state}
        _active_bot._squareoff_reason = "FRAPPE_EMERGENCY_COMMAND"
        await _active_bot._do_squareoff()
        return {"status": "squareoff_done", "state": _active_bot.state}
    raise RuntimeError(f"Unsupported Straddle command: {command}")


_UNATTENDED = True    # paper-mode auto-start; set False to require interactive YES prompt

if __name__ == "__main__":
    import argparse as _ap
    _p = _ap.ArgumentParser(add_help=False)
    _p.add_argument("--yes", action="store_true", help="Unattended mode  -  skip interactive YES prompt")
    if _p.parse_known_args()[0].yes:
        _UNATTENDED = True
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
