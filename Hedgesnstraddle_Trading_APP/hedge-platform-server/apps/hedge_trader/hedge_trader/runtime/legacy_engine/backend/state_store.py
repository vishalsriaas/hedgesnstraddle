"""
State persistence — SQLite (primary) with PostgreSQL fallback.

Tables:
  kv_state             — arbitrary key-value state (executor snapshots, config, etc.)
  paper_trades         — persistent trade history per trader (survives restarts)
  trading_sessions     — per-trader session records (window open → squareoff)
  trader_config        — per-trader user-saved config defaults
  system_events        — log of important system events (connections, errors, etc.)
"""

import asyncio
import json
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

log  = logging.getLogger("state_store")
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="state_io")


async def _mirror_to_frappe(method_name: str, *args, **kwargs):
    try:
        from backend.frappe_bridge import bridge
        method = getattr(bridge, f"mirror_{method_name}")
        return await method(*args, **kwargs)
    except Exception as e:
        log.debug(f"Frappe mirror skipped for {method_name}: {e}")
        return False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trader_name   TEXT    NOT NULL,
    session_id    TEXT,
    ts_ist        TEXT    NOT NULL,
    ts_utc        REAL    NOT NULL,
    action        TEXT    NOT NULL,
    symbol        TEXT,
    side          TEXT,
    qty           REAL    DEFAULT 0,
    fill_price    REAL    DEFAULT 0,
    pnl           REAL    DEFAULT 0,
    notes         TEXT,
    created_at    REAL    DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_pt_trader ON paper_trades(trader_name, ts_utc DESC);
CREATE INDEX IF NOT EXISTS idx_pt_session ON paper_trades(session_id);

CREATE TABLE IF NOT EXISTS trading_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    UNIQUE NOT NULL,
    trader_name     TEXT    NOT NULL,
    session_date    TEXT    NOT NULL,
    target_line     REAL,
    entry_zone      TEXT,
    entry_price     REAL,
    entry_ts_ist    TEXT,
    close_reason    TEXT,
    close_ts_ist    TEXT,
    futures_pnl     REAL    DEFAULT 0,
    hedge_pnl       REAL    DEFAULT 0,
    total_pnl       REAL    DEFAULT 0,
    status          TEXT    DEFAULT 'open',
    created_at      REAL    DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_ts_trader ON trading_sessions(trader_name, created_at DESC);

CREATE TABLE IF NOT EXISTS trader_config (
    trader_name  TEXT    PRIMARY KEY,
    config_json  TEXT    NOT NULL,
    updated_at   REAL    DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS system_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT,
    source     TEXT,
    message    TEXT,
    ts_ist     TEXT,
    ts_utc     REAL    DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS trade_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc     REAL,
    ts_ist     TEXT,
    executor   TEXT,
    action     TEXT,
    instrument TEXT,
    side       TEXT,
    qty        REAL,
    price      REAL,
    pnl        REAL,
    status     TEXT,
    detail     TEXT,
    is_paper   INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS session_event_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trader_name  TEXT    NOT NULL,
    session_date TEXT    NOT NULL,
    event_ts_ist TEXT    NOT NULL,
    event_type   TEXT    NOT NULL,
    state        TEXT,
    price        REAL,
    locked_line  REAL,
    message      TEXT,
    created_at   REAL    DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_sel_trader_date ON session_event_log(trader_name, session_date DESC);

CREATE TABLE IF NOT EXISTS frappe_sync_cursor (
    table_name TEXT PRIMARY KEY,
    last_id    INTEGER NOT NULL DEFAULT 0
);

"""


class StateStore:
    def __init__(self):
        self._pg_pool  = None
        self._db_path: Optional[str] = None
        self._cache: Dict[str, Any]  = {}
        self._frappe_replay_task = None

    # ── Connection ─────────────────────────────────────────────────────────

    async def connect(self, dsn: str = ""):
        """
        Dual-backend setup:
          PostgreSQL (if DATABASE_URL set) → KV cache (agent_state) only.
          SQLite                           → ALL structured data:
                                             paper_trades, trading_sessions,
                                             session_event_log, etc.

        This split allows the server to use Postgres for state persistence
        while keeping all trade/journal records in a reliable local SQLite file.
        Both backends are ALWAYS initialised — PostgreSQL is never a replacement
        for SQLite, only an addition for the KV layer.
        """
        import os as _os
        loop = asyncio.get_running_loop()

        # ── 1. PostgreSQL for KV (agent_state) ─────────────────────────────
        if dsn and dsn.startswith("postgresql"):
            try:
                import asyncpg
                self._pg_pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
                await self._pg_ensure_table()
                await self._pg_load_all()
                log.info("PostgreSQL connected — KV store active.")
            except Exception as e:
                log.warning(f"PostgreSQL unavailable ({e}) — KV will use SQLite cache.")

        # ── 2. SQLite for ALL structured data (always, regardless of Postgres) ─
        default_db = _os.path.join(
            _os.path.dirname(_os.path.abspath(__file__)), "..", "hedge_state.db"
        )
        self._db_path = _os.path.abspath(
            _os.environ.get("DB_PATH", default_db)
        )
        await loop.run_in_executor(_pool, self._sqlite_init)
        # Load KV from SQLite only if PostgreSQL is NOT available
        if not self._pg_pool:
            await loop.run_in_executor(_pool, self._sqlite_load_all)
        log.info(f"SQLite ready (trades/sessions/events): {self._db_path}")
        if self._frappe_replay_task is None or self._frappe_replay_task.done():
            self._frappe_replay_task = asyncio.create_task(self._frappe_replay_loop())
        # Sanity check — verify paper_trades table is writable at startup
        try:
            def _write_test():
                with sqlite3.connect(self._db_path) as c:
                    c.execute(
                        "INSERT INTO paper_trades "
                        "(trader_name,action,symbol,side,qty,fill_price,pnl,ts_ist,ts_utc) "
                        "VALUES ('__test__','TEST','TEST','BUY',0,0,0,'',0)"
                    )
                    c.execute("DELETE FROM paper_trades WHERE trader_name='__test__'")
            await loop.run_in_executor(_pool, _write_test)
            log.info("paper_trades write-test: OK")
        except Exception as e:
            log.error(
                f"paper_trades write-test FAILED: {e} "
                f"— DB path={self._db_path} — trades will NOT persist!"
            )

    def _sqlite_replay_rows(self, table: str, limit: int = 100) -> list:
        with sqlite3.connect(self._db_path) as c:
            c.row_factory = sqlite3.Row
            if table == "trading_sessions":
                rows = c.execute("SELECT * FROM trading_sessions ORDER BY id").fetchall()
            else:
                cursor = c.execute(
                    "SELECT last_id FROM frappe_sync_cursor WHERE table_name=?", (table,)
                ).fetchone()
                last_id = int(cursor[0] or 0) if cursor else 0
                rows = c.execute(
                    f"SELECT * FROM {table} WHERE id>? ORDER BY id LIMIT ?",
                    (last_id, limit),
                ).fetchall()
        return [dict(row) for row in rows]

    def _sqlite_advance_replay(self, table: str, row_id: int):
        with sqlite3.connect(self._db_path) as c:
            c.execute(
                "INSERT INTO frappe_sync_cursor(table_name,last_id) VALUES (?,?) "
                "ON CONFLICT(table_name) DO UPDATE SET last_id=excluded.last_id",
                (table, int(row_id)),
            )

    async def _frappe_replay_once(self):
        mapping = {
            "paper_trades": "paper_trade",
            "trade_log": "trade_log",
            "session_event_log": "session_event",
        }
        loop = asyncio.get_running_loop()
        sessions = await loop.run_in_executor(
            _pool, self._sqlite_replay_rows, "trading_sessions", 0
        )
        for row in sessions:
            ok = await _mirror_to_frappe("session", row)
            if ok is False:
                break

        for table, mirror_name in mapping.items():
            rows = await loop.run_in_executor(
                _pool, self._sqlite_replay_rows, table, 100
            )
            for row in rows:
                ok = await _mirror_to_frappe(mirror_name, row)
                if not ok:
                    break
                await loop.run_in_executor(
                    _pool, self._sqlite_advance_replay, table, row["id"]
                )

    async def _frappe_replay_loop(self):
        """Replay durable SQLite rows until Frappe acknowledges each one."""
        await asyncio.sleep(5)
        while True:
            try:
                await self._frappe_replay_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Frappe backlog replay failed; retrying: %s", exc)
            await asyncio.sleep(15)

    async def close(self):
        if self._frappe_replay_task and not self._frappe_replay_task.done():
            self._frappe_replay_task.cancel()
            await asyncio.gather(self._frappe_replay_task, return_exceptions=True)
        self._frappe_replay_task = None
        if self._pg_pool:
            await self._pg_pool.close()
            self._pg_pool = None

    # ── SQLite init ────────────────────────────────────────────────────────

    def _sqlite_init(self):
        with sqlite3.connect(self._db_path) as c:
            c.execute("PRAGMA journal_mode=WAL")   # concurrent-safe writes
            c.execute("PRAGMA synchronous=NORMAL")
            c.executescript(_SCHEMA)
        # Migrations — each wrapped in try/except since the column may already exist
        with sqlite3.connect(self._db_path) as c:
            for ddl in [
                "ALTER TABLE trading_sessions ADD COLUMN is_force_closed INTEGER DEFAULT 0",
                "ALTER TABLE trading_sessions ADD COLUMN balance_before REAL",
                "ALTER TABLE trading_sessions ADD COLUMN balance_after  REAL",
                "ALTER TABLE trading_sessions ADD COLUMN window_open_ts TEXT",
                "ALTER TABLE trading_sessions ADD COLUMN display_name   TEXT",
                "ALTER TABLE session_event_log ADD COLUMN session_id TEXT",
            ]:
                try: c.execute(ddl)
                except Exception: pass
            try:
                c.execute("CREATE INDEX IF NOT EXISTS idx_sel_session_id ON session_event_log(session_id)")
            except Exception: pass
            # VolatileTrader was retired. Remove its legacy allocation, state,
            # sessions and trades so this database contains only Bull/Bear
            # trader-owned records. This cleanup is idempotent on every start.
            c.execute("DELETE FROM paper_trades WHERE trader_name LIKE 'VolatileTrader%'")
            c.execute("DELETE FROM trading_sessions WHERE trader_name LIKE 'VolatileTrader%'")
            c.execute("DELETE FROM trader_config WHERE trader_name LIKE 'VolatileTrader%'")
            c.execute("DELETE FROM session_event_log WHERE trader_name LIKE 'VolatileTrader%'")
            c.execute("DELETE FROM trade_log WHERE executor LIKE 'VolatileTrader%'")
            c.execute(
                "DELETE FROM kv_state WHERE lower(key) LIKE '%volatile%'"
            )
    def _sqlite_load_all(self):
        with sqlite3.connect(self._db_path) as c:
            for key, val in c.execute("SELECT key, value FROM kv_state").fetchall():
                try:
                    self._cache[key] = json.loads(val)
                except Exception:
                    pass

    def _sqlite_write(self, key: str, value: Any):
        with sqlite3.connect(self._db_path) as c:
            c.execute(
                "INSERT OR REPLACE INTO kv_state (key, value, updated_at) VALUES (?,?,?)",
                (key, json.dumps(value), time.time()),
            )

    # ── Public KV API ──────────────────────────────────────────────────────

    async def set(self, key: str, value: Any):
        self._cache[key] = value
        loop = asyncio.get_running_loop()
        if self._pg_pool:
            try:
                await self._pg_write(key, value)
            except Exception as e:
                log.error(f"PostgreSQL save [{key}]: {e}")
        elif self._db_path:
            try:
                await loop.run_in_executor(_pool, self._sqlite_write, key, value)
            except Exception as e:
                log.error(f"SQLite save [{key}]: {e}")

    def get(self, key: str, default=None) -> Any:
        return self._cache.get(key, default)

    # ── Paper Trades (structured, per-trader) ─────────────────────────────

    def _sqlite_insert_paper_trade(self, row: dict):
        with sqlite3.connect(self._db_path) as c:
            c.execute(
                """INSERT INTO paper_trades
                   (trader_name, session_id, ts_ist, ts_utc, action, symbol,
                    side, qty, fill_price, pnl, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["trader_name"], row.get("session_id"),
                    row["ts_ist"], row["ts_utc"],
                    row["action"], row.get("symbol", ""),
                    row.get("side", ""), row.get("qty", 0),
                    row.get("fill_price", 0), row.get("pnl", 0),
                    row.get("notes", ""),
                ),
            )

    async def save_paper_trade(self, trader_name: str, action: str, symbol: str,
                                side: str, qty: float, fill_price: float, pnl: float,
                                session_id: str = "", notes: str = "",
                                ts_ist: str = "", ts_utc: float = 0.0):
        from backend.utils import utc_now, ist_now_str
        # Use caller-supplied execution timestamp when provided so DB records
        # match the exact moment the order was placed, not when it was persisted.
        row = {
            "trader_name": trader_name, "session_id": session_id,
            "ts_ist": ts_ist or ist_now_str(),
            "ts_utc": ts_utc or utc_now(),
            "action": action, "symbol": symbol,
            "side": side, "qty": qty, "fill_price": fill_price,
            "pnl": pnl, "notes": notes,
        }
        if self._db_path:
            loop = asyncio.get_running_loop()
            for attempt in range(3):   # 3 attempts before giving up
                try:
                    await loop.run_in_executor(_pool, self._sqlite_insert_paper_trade, row)
                    break   # success
                except Exception as e:
                    if attempt < 2:
                        log.warning(f"save_paper_trade attempt {attempt+1} failed ({e}) — retrying")
                        await asyncio.sleep(0.5)
                    else:
                        # All 3 attempts failed — Telegram alert
                        msg = (
                            f"save_paper_trade FAILED after 3 attempts!\n"
                            f"action={action} symbol={symbol} pnl={pnl}\n"
                            f"session={session_id} err={e}"
                        )
                        log.error(msg)
                        try:
                            from backend import telegram_alert as _tg
                            _tg.send(f"🚨 <b>DB WRITE FAILED</b>\n{msg}")
                        except Exception:
                            pass
        else:
            log.error("save_paper_trade: NO DB PATH — trade lost permanently!")
            try:
                from backend import telegram_alert as _tg
                _tg.send(f"🚨 <b>DB PATH MISSING</b>\nsave_paper_trade called but _db_path is None\naction={action} symbol={symbol}")
            except Exception:
                pass
        await _mirror_to_frappe("paper_trade", row)
        return row

    def _sqlite_get_paper_trades(self, trader_name: str, limit: int) -> list:
        with sqlite3.connect(self._db_path) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM paper_trades WHERE trader_name=? ORDER BY ts_utc DESC LIMIT ?",
                (trader_name, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    async def get_paper_trades(self, trader_name: str, limit: int = 200) -> list:
        if not self._db_path:
            return []
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(_pool, self._sqlite_get_paper_trades,
                                              trader_name, limit)
        except Exception as e:
            log.error(f"get_paper_trades error: {e}")
            return []

    # ── Trading Sessions ──────────────────────────────────────────────────

    def _sqlite_upsert_session(self, row: dict):
        with sqlite3.connect(self._db_path) as c:
            c.execute(
                """INSERT INTO trading_sessions
                   (session_id, trader_name, session_date, target_line, entry_zone,
                    entry_price, entry_ts_ist, close_reason, close_ts_ist,
                    futures_pnl, hedge_pnl, total_pnl, status,
                    balance_before, balance_after, window_open_ts, display_name)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(session_id) DO UPDATE SET
                     close_reason  = COALESCE(excluded.close_reason, trading_sessions.close_reason),
                     close_ts_ist  = COALESCE(excluded.close_ts_ist, trading_sessions.close_ts_ist),
                     entry_ts_ist  = COALESCE(excluded.entry_ts_ist, trading_sessions.entry_ts_ist),
                     entry_price   = COALESCE(excluded.entry_price, trading_sessions.entry_price),
                     futures_pnl   = excluded.futures_pnl,
                     hedge_pnl     = excluded.hedge_pnl,
                     total_pnl     = excluded.total_pnl,
                     status        = excluded.status,
                     window_open_ts= COALESCE(excluded.window_open_ts, trading_sessions.window_open_ts),
                     display_name  = COALESCE(excluded.display_name, trading_sessions.display_name),
                     balance_after = COALESCE(excluded.balance_after, trading_sessions.balance_after)""",
                (
                    row["session_id"], row["trader_name"], row["session_date"],
                    row.get("target_line"), row.get("entry_zone", ""),
                    row.get("entry_price"), row.get("entry_ts_ist", ""),
                    row.get("close_reason", ""), row.get("close_ts_ist", ""),
                    row.get("futures_pnl", 0), row.get("hedge_pnl", 0),
                    row.get("total_pnl", 0), row.get("status", "open"),
                    row.get("balance_before"), row.get("balance_after"),
                    row.get("window_open_ts"), row.get("display_name"),
                ),
            )

    async def save_session(self, session_id: str, trader_name: str, **kwargs):
        from backend.utils import ist_now, get_session_day
        from backend.config import cfg as _cfg
        n = ist_now()
        try:
            exp_h = int(getattr(_cfg, "session_expiry_h", 13))
            exp_m = int(getattr(_cfg, "session_expiry_m", 30))
            session_date = get_session_day(n, exp_h, exp_m)
        except Exception:
            session_date = n.strftime("%Y-%m-%d")
        row = {
            "session_id": session_id,
            "trader_name": trader_name,
            "session_date": kwargs.pop("session_date", session_date),
            **kwargs,
        }
        if self._db_path:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(_pool, self._sqlite_upsert_session, row)
            except Exception as e:
                log.error(f"save_session error: {e}")
        await _mirror_to_frappe("session", row)

    def _sqlite_get_sessions(self, trader_name: str, limit: int) -> list:
        with sqlite3.connect(self._db_path) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM trading_sessions WHERE trader_name=? ORDER BY created_at DESC LIMIT ?",
                (trader_name, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    async def get_sessions(self, trader_name: str, limit: int = 50) -> list:
        if not self._db_path:
            return []
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(_pool, self._sqlite_get_sessions,
                                              trader_name, limit)
        except Exception as e:
            log.error(f"get_sessions error: {e}")
            return []

    def _sqlite_get_sessions_filtered(self, trader_name: str, from_date: str, to_date: str, limit: int, today_ist: str = "") -> list:
        with sqlite3.connect(self._db_path) as c:
            c.row_factory = sqlite3.Row

            # ── Part A: real sessions from trading_sessions ──────────────────
            conds_a, params_a = [], []
            if trader_name and trader_name.lower() != "all":
                conds_a.append("trader_name=?"); params_a.append(trader_name)
            if from_date:
                conds_a.append("session_date >= ?"); params_a.append(from_date)
            if to_date:
                conds_a.append("session_date <= ?"); params_a.append(to_date)
            where_a = ("WHERE " + " AND ".join(conds_a)) if conds_a else ""
            q_a = f"""
                SELECT session_id, trader_name, session_date,
                       target_line, entry_zone, entry_price,
                       entry_ts_ist, close_reason, close_ts_ist,
                       futures_pnl, hedge_pnl, total_pnl, status,
                       created_at, is_force_closed,
                       window_open_ts, display_name
                FROM trading_sessions {where_a}"""

            # ── Part B: no-trade activity days (events exist, no session row) ──
            # If session_date == today_ist (window currently open, no trade yet) → status='open'
            # so the journal shows it as "CURRENT SESSION ● RUNNING".
            # Past no-trade days stay status='no_trade'.
            conds_b, params_b = [], []
            if trader_name and trader_name.lower() != "all":
                conds_b.append("e.trader_name=?"); params_b.append(trader_name)
            if from_date:
                conds_b.append("e.session_date >= ?"); params_b.append(from_date)
            if to_date:
                conds_b.append("e.session_date <= ?"); params_b.append(to_date)
            where_b = ("WHERE " + " AND ".join(conds_b) + " AND ") if conds_b else "WHERE "
            params_b.append(today_ist)  # for the CASE expression
            q_b = f"""
                SELECT
                    'notrade_' || e.trader_name || '_' || e.session_date AS session_id,
                    e.trader_name,
                    e.session_date,
                    NULL AS target_line,
                    NULL AS entry_zone,
                    NULL AS entry_price,
                    MIN(e.event_ts_ist) AS entry_ts_ist,
                    NULL AS close_reason,
                    MAX(e.event_ts_ist) AS close_ts_ist,
                    0.0 AS futures_pnl,
                    0.0 AS hedge_pnl,
                    0.0 AS total_pnl,
                    CASE WHEN e.session_date = ? THEN 'running' ELSE 'done' END AS status,
                    MIN(e.created_at) AS created_at,
                    0 AS is_force_closed,
                    MIN(e.event_ts_ist) AS window_open_ts,
                    NULL AS display_name
                FROM session_event_log e
                {where_b}NOT EXISTS (
                    SELECT 1 FROM trading_sessions t
                    WHERE t.trader_name = e.trader_name
                      AND t.session_date = e.session_date
                )
                GROUP BY e.trader_name, e.session_date"""

            union_q = f"{q_a} UNION ALL {q_b} ORDER BY created_at DESC LIMIT ?"
            rows = c.execute(union_q, tuple(params_a + params_b + [limit])).fetchall()
        return [dict(r) for r in rows]

    async def get_sessions_filtered(self, trader_name: str, from_date: str = "", to_date: str = "", limit: int = 100) -> list:
        if not self._db_path: return []
        loop = asyncio.get_running_loop()
        try:
            from backend.utils import ist_now as _isn
            today_ist = _isn().strftime("%Y-%m-%d")
            return await loop.run_in_executor(
                _pool, self._sqlite_get_sessions_filtered,
                trader_name, from_date, to_date, limit, today_ist
            )
        except Exception:
            return []

    def _sqlite_get_trades_by_session(self, session_id: str) -> list:
        with sqlite3.connect(self._db_path) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute("SELECT * FROM paper_trades WHERE session_id=? ORDER BY ts_utc ASC", (session_id,)).fetchall()
        return [dict(r) for r in rows]

    async def get_trades_by_session(self, session_id: str) -> list:
        if not self._db_path: return []
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(_pool, self._sqlite_get_trades_by_session, session_id)
        except Exception:
            return []

    # ── Delete Trader History ─────────────────────────────────────────────

    def _sqlite_delete_trader_history(self, trader_name: str):
        with sqlite3.connect(self._db_path) as c:
            c.execute("DELETE FROM paper_trades WHERE trader_name=?", (trader_name,))
            c.execute("DELETE FROM trading_sessions WHERE trader_name=?", (trader_name,))
            # Also wipe persisted executor state so it doesn't reload stale PnL on restart
            c.execute("DELETE FROM kv_state WHERE key=?", (f"{trader_name}_state",))

    async def delete_trader_history(self, trader_name: str):
        if not self._db_path: return
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(_pool, self._sqlite_delete_trader_history, trader_name)
            # Also remove from kv_state cache for this trader
            self._cache.pop(f"{trader_name}_state", None)
        except Exception as e:
            log.error(f"delete_trader_history error: {e}")

    # ── Mark Session Force-Closed ─────────────────────────────────────────

    def _sqlite_mark_force_closed(self, session_id: str):
        with sqlite3.connect(self._db_path) as c:
            c.execute(
                "UPDATE trading_sessions SET is_force_closed=1, status='done' WHERE session_id=?",
                (session_id,)
            )

    async def mark_session_force_closed(self, session_id: str):
        if not self._db_path or not session_id: return
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(_pool, self._sqlite_mark_force_closed, session_id)
        except Exception as e:
            log.error(f"mark_session_force_closed error: {e}")

    # ── Per-trader Config Defaults ─────────────────────────────────────────

    def _sqlite_save_trader_cfg(self, trader_name: str, cfg: dict):
        with sqlite3.connect(self._db_path) as c:
            c.execute(
                """INSERT INTO trader_config (trader_name, config_json, updated_at)
                   VALUES (?,?,?)
                   ON CONFLICT(trader_name) DO UPDATE SET
                     config_json=excluded.config_json,
                     updated_at=excluded.updated_at""",
                (trader_name, json.dumps(cfg), time.time()),
            )

    async def save_trader_config(self, trader_name: str, config: dict):
        if self._db_path:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(_pool, self._sqlite_save_trader_cfg,
                                           trader_name, config)
            except Exception as e:
                log.error(f"save_trader_config error: {e}")

    def _sqlite_get_trader_cfg(self, trader_name: str) -> Optional[dict]:
        with sqlite3.connect(self._db_path) as c:
            row = c.execute(
                "SELECT config_json FROM trader_config WHERE trader_name=?",
                (trader_name,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    async def get_trader_config(self, trader_name: str) -> Optional[dict]:
        if not self._db_path:
            return None
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(_pool, self._sqlite_get_trader_cfg, trader_name)
        except Exception as e:
            log.error(f"get_trader_config error: {e}")
            return None

    # ── Legacy trade_log (kept for backward-compat) ────────────────────────

    def _sqlite_log_trade_legacy(self, row: dict):
        with sqlite3.connect(self._db_path) as c:
            c.execute(
                """INSERT INTO trade_log
                   (ts_utc,ts_ist,executor,action,instrument,side,qty,price,pnl,status,detail,is_paper)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["ts_utc"], row["ts_ist"], row["executor"],
                    row["action"], row["instrument"], row["side"],
                    row["qty"], row["price"], row["pnl"],
                    row["status"], json.dumps(row.get("detail") or {}),
                    1 if row.get("is_paper") else 0,
                ),
            )

    async def log_trade(self, executor: str, action: str, instrument: str,
                        side: str, qty: float, price: float, pnl: float,
                        status: str, detail: dict = None, is_paper: bool = True):
        from backend.utils import utc_now, ist_now_str
        row = {
            "ts_utc": utc_now(), "ts_ist": ist_now_str(),
            "executor": executor, "action": action, "instrument": instrument,
            "side": side, "qty": qty, "price": price, "pnl": pnl,
            "status": status, "detail": detail or {}, "is_paper": is_paper,
        }
        loop = asyncio.get_running_loop()
        if self._db_path:
            try:
                await loop.run_in_executor(_pool, self._sqlite_log_trade_legacy, row)
            except Exception as e:
                log.error(f"log_trade error: {e}")
        await _mirror_to_frappe("trade_log", row)
        return row

    async def get_recent_trades(self, limit: int = 200, is_paper: bool = True) -> list:
        if not self._db_path:
            return []
        loop = asyncio.get_running_loop()
        try:
            def _q():
                with sqlite3.connect(self._db_path) as c:
                    c.row_factory = sqlite3.Row
                    rows = c.execute(
                        "SELECT * FROM trade_log WHERE is_paper=? ORDER BY ts_utc DESC LIMIT ?",
                        (1 if is_paper else 0, limit),
                    ).fetchall()
                return [dict(r) for r in rows]
            return await loop.run_in_executor(_pool, _q)
        except Exception:
            return []

    async def get_manager_logs(self, _limit: int = 200) -> list:
        return []

    async def log_analyst(self, high_line: float, low_line: float,
                          candidates: list, touches: list):
        await self.set("analyst_lines", {
            "high_line": high_line, "low_line": low_line,
            "candidates": candidates, "touches": touches,
        })

    # ── Session Event Log ──────────────────────────────────────────────────

    def _sqlite_insert_session_event(self, row: dict):
        with sqlite3.connect(self._db_path) as c:
            c.execute(
                """INSERT INTO session_event_log
                   (trader_name, session_date, event_ts_ist, event_type,
                    state, price, locked_line, message, session_id)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (row["trader_name"], row["session_date"], row["event_ts_ist"],
                 row["event_type"], row.get("state"), row.get("price"),
                 row.get("locked_line"), row.get("message", ""),
                 row.get("session_id")),
            )

    def _sqlite_get_session_events(self, trader_name: str,
                                    session_date: str, limit: int) -> list:
        with sqlite3.connect(self._db_path) as c:
            c.row_factory = sqlite3.Row
            clauses, params = ["trader_name=?"], [trader_name]
            if session_date:
                clauses.append("session_date=?"); params.append(session_date)
            params.append(limit)
            rows = c.execute(
                f"SELECT * FROM session_event_log WHERE {' AND '.join(clauses)} "
                f"ORDER BY created_at ASC LIMIT ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    async def save_session_event(self, trader_name: str, session_date: str,
                                  event_ts_ist: str, event_type: str,
                                  state: str = "", price: float = 0.0,
                                  locked_line: float = 0.0, message: str = "",
                                  session_id: str = ""):
        row = {
            "trader_name": trader_name, "session_date": session_date,
            "event_ts_ist": event_ts_ist, "event_type": event_type,
            "state": state, "price": price or None,
            "locked_line": locked_line or None, "message": message,
            "session_id": session_id or None,
        }
        if self._db_path:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(_pool, self._sqlite_insert_session_event, row)
            except Exception as e:
                log.error(f"save_session_event error: {e}")
        await _mirror_to_frappe("session_event", row)

    def _sqlite_get_session_events_by_id(self, session_id: str) -> list:
        """
        Get session_event_log rows for a session.
        Handles session_id formats:
          - New format:  'exp_140626_1330_bull' → direct session_id column lookup
          - No-trade:    'notrade_BullishExecutor_Paper_2026-06-07' → parse trader+date
          - Legacy trade:'2026-06-08_00-05-04_IST' → date-based lookup
        """
        from datetime import datetime, timedelta
        with sqlite3.connect(self._db_path) as c:
            c.row_factory = sqlite3.Row

            if session_id.startswith("exp_"):
                # New canonical format — direct lookup by session_id column
                rows = c.execute(
                    "SELECT * FROM session_event_log WHERE session_id=? ORDER BY created_at ASC",
                    (session_id,)
                ).fetchall()
                if rows:
                    return [dict(r) for r in rows]
                # Fallback: lookup via trading_sessions for cross-day events
                row = c.execute(
                    "SELECT trader_name, session_date FROM trading_sessions WHERE session_id=?",
                    (session_id,)
                ).fetchone()
                if not row:
                    return []
                trader_name = row["trader_name"]
                date_str    = row["session_date"] or ""
                try:
                    d = datetime.strptime(date_str, "%Y-%m-%d")
                    candidates = [date_str, (d - timedelta(days=1)).strftime("%Y-%m-%d")]
                except Exception:
                    candidates = [date_str] if date_str else []
                if not candidates:
                    return []
                placeholders = ",".join("?" for _ in candidates)
                rows = c.execute(
                    f"SELECT * FROM session_event_log WHERE trader_name=? "
                    f"AND session_date IN ({placeholders}) ORDER BY created_at ASC",
                    [trader_name] + candidates,
                ).fetchall()
                return [dict(r) for r in rows]

            elif session_id.startswith("notrade_"):
                # Format: notrade_{trader_name}_{YYYY-MM-DD}
                date_str    = session_id[-10:]
                trader_name = session_id[len("notrade_"):-11]
                candidates  = [date_str]
            else:
                # Legacy real session: first 10 chars = date portion
                date_str = session_id[:10]
                try:
                    d = datetime.strptime(date_str, "%Y-%m-%d")
                    candidates = [date_str, (d - timedelta(days=1)).strftime("%Y-%m-%d")]
                except Exception:
                    candidates = [date_str]
                row = c.execute(
                    "SELECT trader_name FROM trading_sessions WHERE session_id=?",
                    (session_id,)
                ).fetchone()
                if not row:
                    return []
                trader_name = row["trader_name"]

            placeholders = ",".join("?" for _ in candidates)
            rows = c.execute(
                f"SELECT * FROM session_event_log WHERE trader_name=? "
                f"AND session_date IN ({placeholders}) ORDER BY created_at ASC",
                [trader_name] + candidates,
            ).fetchall()
        return [dict(r) for r in rows]

    async def get_session_events_by_id(self, session_id: str) -> list:
        if not self._db_path or not session_id:
            return []
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                _pool, self._sqlite_get_session_events_by_id, session_id
            )
        except Exception as e:
            log.error(f"get_session_events_by_id error: {e}")
            return []

    async def get_session_events(self, trader_name: str, session_date: str = "",
                                  limit: int = 500) -> list:
        if not self._db_path:
            return []
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                _pool, self._sqlite_get_session_events,
                trader_name, session_date, limit
            )
        except Exception as e:
            log.error(f"get_session_events error: {e}")
            return []

    def _sqlite_get_session_dates(self, trader_name: str, limit: int) -> list:
        with sqlite3.connect(self._db_path) as c:
            rows = c.execute(
                """SELECT DISTINCT session_date FROM session_event_log
                   WHERE trader_name=? ORDER BY session_date DESC LIMIT ?""",
                (trader_name, limit),
            ).fetchall()
        return [r[0] for r in rows]

    async def get_session_dates(self, trader_name: str, limit: int = 30) -> list:
        if not self._db_path:
            return []
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                _pool, self._sqlite_get_session_dates, trader_name, limit
            )
        except Exception as e:
            return []

    # ── PostgreSQL stubs ────────────────────────────────────────────────────

    async def _pg_ensure_table(self):
        async with self._pg_pool.acquire() as con:
            await con.execute("""
                CREATE TABLE IF NOT EXISTS agent_state (
                    key TEXT PRIMARY KEY,
                    value JSONB NOT NULL,
                    updated_at DOUBLE PRECISION NOT NULL
                );
            """)

    async def _pg_load_all(self):
        async with self._pg_pool.acquire() as con:
            rows = await con.fetch("SELECT key, value FROM agent_state")
            for row in rows:
                self._cache[row["key"]] = json.loads(row["value"])

    async def _pg_write(self, key: str, value: Any):
        async with self._pg_pool.acquire() as con:
            await con.execute(
                """INSERT INTO agent_state (key, value, updated_at)
                   VALUES ($1, $2::jsonb, $3)
                   ON CONFLICT (key) DO UPDATE
                   SET value = EXCLUDED.value,
                       updated_at = EXCLUDED.updated_at""",
                key, json.dumps(value), time.time(),
            )


store = StateStore()

