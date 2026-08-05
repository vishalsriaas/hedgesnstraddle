"""
State persistence — Frappe-site MariaDB only.

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
from hedge_trader import mariadb_compat as dbapi
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

log  = logging.getLogger("state_store")
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="state_io")

class ConcurrentStateWrite(RuntimeError):
    """Raised when a KV snapshot was changed after the caller loaded it."""


async def _mirror_to_frappe(method_name: str, *args, **kwargs):
    try:
        from backend.frappe_bridge import bridge
        method = getattr(bridge, f"mirror_{method_name}")
        return await method(*args, **kwargs)
    except Exception as e:
        log.debug(f"Frappe mirror skipped for {method_name}: {e}")
        return False

class StateStore:
    def __init__(self):
        self._db_path: Optional[str] = None
        self._cache: Dict[str, Any]  = {}
        self._frappe_replay_task = None

    # ── Connection ─────────────────────────────────────────────────────────

    async def connect(self, _dsn: str = ""):
        """Connect to the MariaDB database configured by the Frappe worker."""
        import os as _os
        loop = asyncio.get_running_loop()

        self._db_path = _os.environ.get("MARIADB_DATABASE")
        if not self._db_path:
            raise RuntimeError("MARIADB_DATABASE is required; no file-database fallback exists")
        from hedge_trader.mariadb_compat import ensure_schema
        await loop.run_in_executor(_pool, ensure_schema)
        await loop.run_in_executor(_pool, self._mariadb_init)
        await loop.run_in_executor(_pool, self._mariadb_load_all)
        log.info("MariaDB ready (state/trades/sessions/events): %s", self._db_path)
        if self._frappe_replay_task is None or self._frappe_replay_task.done():
            self._frappe_replay_task = asyncio.create_task(self._frappe_replay_loop())
        # Sanity check — verify paper_trades table is writable at startup
        try:
            def _write_test():
                with dbapi.connect(self._db_path) as c:
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
                f"— MariaDB database={self._db_path} — trades will NOT persist!"
            )

    def _mariadb_replay_rows(self, table: str, limit: int = 100) -> list:
        with dbapi.connect(self._db_path) as c:
            c.row_factory = dbapi.Row
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

    def _mariadb_advance_replay(self, table: str, row_id: int):
        with dbapi.connect(self._db_path) as c:
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
            _pool, self._mariadb_replay_rows, "trading_sessions", 0
        )
        for row in sessions:
            ok = await _mirror_to_frappe("session", row)
            if ok is False:
                break

        for table, mirror_name in mapping.items():
            rows = await loop.run_in_executor(
                _pool, self._mariadb_replay_rows, table, 100
            )
            for row in rows:
                ok = await _mirror_to_frappe(mirror_name, row)
                if not ok:
                    break
                await loop.run_in_executor(
                    _pool, self._mariadb_advance_replay, table, row["id"]
                )

    async def _frappe_replay_loop(self):
        """Replay durable MariaDB rows until Frappe acknowledges each one."""
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

    # ── MariaDB init ────────────────────────────────────────────────────────

    def _mariadb_init(self):
        from hedge_trader.mariadb_compat import ensure_schema

        ensure_schema()
        # VolatileTrader is retired; cleanup is idempotent.
        with dbapi.connect(self._db_path) as c:
            c.execute("DELETE FROM paper_trades WHERE trader_name LIKE 'VolatileTrader%'")
            c.execute("DELETE FROM trading_sessions WHERE trader_name LIKE 'VolatileTrader%'")
            c.execute("DELETE FROM trader_config WHERE trader_name LIKE 'VolatileTrader%'")
            c.execute("DELETE FROM session_event_log WHERE trader_name LIKE 'VolatileTrader%'")
            c.execute("DELETE FROM trade_log WHERE executor LIKE 'VolatileTrader%'")
            c.execute("DELETE FROM kv_state WHERE lower(key) LIKE '%volatile%'")
    def _mariadb_load_all(self):
        with dbapi.connect(self._db_path) as c:
            for key, val in c.execute("SELECT key, value FROM kv_state").fetchall():
                try:
                    self._cache[key] = json.loads(val)
                except Exception:
                    pass

    def _mariadb_write(self, key: str, value: Any):
        with dbapi.connect(self._db_path) as c:
            c.execute(
                "INSERT OR REPLACE INTO kv_state (key, value, updated_at) VALUES (?,?,?)",
                (key, json.dumps(value), time.time()),
            )

    def _mariadb_get_versioned(self, key: str):
        with dbapi.connect(self._db_path) as c:
            row = c.execute(
                "SELECT value, updated_at FROM kv_state WHERE key=?", (key,)
            ).fetchone()
        if not row:
            return None, None
        return json.loads(row[0]), float(row[1] or 0)

    def get_versioned(self, key: str):
        if not self._db_path:
            return self._cache.get(key), None
        return self._mariadb_get_versioned(key)

    def _mariadb_cas_write(self, key: str, value: dict, expected_updated_at):
        now = time.time()
        with dbapi.connect(self._db_path, timeout=30) as c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute(
                "SELECT updated_at FROM kv_state WHERE key=?", (key,)
            ).fetchone()
            if expected_updated_at is None:
                if current is not None:
                    raise ConcurrentStateWrite(f"{key} was created concurrently")
                c.execute(
                    "INSERT INTO kv_state(key,value,updated_at) VALUES (?,?,?)",
                    (key, json.dumps(value), now),
                )
            else:
                cur = c.execute(
                    """UPDATE kv_state SET value=?,updated_at=?
                       WHERE key=? AND updated_at=?""",
                    (json.dumps(value), now, key, expected_updated_at),
                )
                if cur.rowcount != 1:
                    actual = float(current[0] or 0) if current else None
                    raise ConcurrentStateWrite(
                        f"{key} changed: expected={expected_updated_at}, actual={actual}"
                    )
        return now

    async def cas_set(self, key: str, value: dict, expected_updated_at):
        if not self._db_path:
            raise RuntimeError("CAS state write requires MariaDB")
        loop = asyncio.get_running_loop()
        version = await loop.run_in_executor(
            _pool, self._mariadb_cas_write, key, value, expected_updated_at
        )
        self._cache[key] = value
        return version

    def _mariadb_atomic_paper_fill(self, key: str, value: dict,
                                  expected_updated_at, paper_row: dict,
                                  trade_row: dict):
        """Commit both ledgers and the recovery snapshot as one unit."""
        now = time.time()
        with dbapi.connect(self._db_path, timeout=30) as c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute(
                "SELECT updated_at FROM kv_state WHERE key=?", (key,)
            ).fetchone()
            current_version = float(current[0] or 0) if current else None
            if expected_updated_at is None:
                if current is not None:
                    raise ConcurrentStateWrite(f"{key} was created concurrently")
            elif current is None or abs(current_version - float(expected_updated_at)) > 1e-9:
                raise ConcurrentStateWrite(
                    f"{key} changed: expected={expected_updated_at}, actual={current_version}"
                )

            c.execute(
                """INSERT INTO paper_trades
                   (trader_name,session_id,ts_ist,ts_utc,action,symbol,side,
                    qty,fill_price,pnl,notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    paper_row["trader_name"], paper_row.get("session_id"),
                    paper_row["ts_ist"], paper_row["ts_utc"], paper_row["action"],
                    paper_row.get("symbol", ""), paper_row.get("side", ""),
                    paper_row.get("qty", 0), paper_row.get("fill_price", 0),
                    paper_row.get("pnl", 0), paper_row.get("notes", ""),
                ),
            )
            c.execute(
                """INSERT INTO trade_log
                   (ts_utc,ts_ist,executor,action,instrument,side,qty,price,
                    pnl,status,detail,is_paper)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    trade_row["ts_utc"], trade_row["ts_ist"],
                    trade_row["executor"], trade_row["action"],
                    trade_row["instrument"], trade_row["side"],
                    trade_row["qty"], trade_row["price"], trade_row["pnl"],
                    trade_row.get("status", "FILLED"),
                    json.dumps(trade_row.get("detail", {})), 1,
                ),
            )
            if current is None:
                c.execute(
                    "INSERT INTO kv_state(key,value,updated_at) VALUES (?,?,?)",
                    (key, json.dumps(value), now),
                )
            else:
                cur = c.execute(
                    """UPDATE kv_state SET value=?, updated_at=?
                       WHERE key=? AND updated_at=?""",
                    (json.dumps(value), now, key, expected_updated_at),
                )
                if cur.rowcount != 1:
                    raise ConcurrentStateWrite(f"{key} CAS update lost")
        return now

    async def atomic_paper_fill(self, key: str, value: dict,
                                expected_updated_at, paper_row: dict,
                                trade_row: dict):
        if not self._db_path:
            raise RuntimeError("Atomic paper fill requires MariaDB")
        loop = asyncio.get_running_loop()
        version = await loop.run_in_executor(
            _pool, self._mariadb_atomic_paper_fill, key, value,
            expected_updated_at, paper_row, trade_row
        )
        self._cache[key] = value
        return version

    def _mariadb_reconcile_paper_state(self) -> list:
        """Independently replay the immutable ledger and compare open state."""
        issues = []
        with dbapi.connect(self._db_path) as c:
            c.row_factory = dbapi.Row
            rows = c.execute(
                """SELECT trader_name,symbol,side,qty,fill_price,pnl
                   FROM paper_trades ORDER BY id"""
            ).fetchall()
            states = {}
            pnl_by_trader = {}
            for row in rows:
                trader = row["trader_name"] or "default"
                symbol = row["symbol"] or ""
                key = (trader, symbol)
                side = str(row["side"] or "").upper()
                qty = float(row["qty"] or 0)
                px = float(row["fill_price"] or 0)
                pnl_by_trader[trader] = pnl_by_trader.get(trader, 0.0) + float(row["pnl"] or 0)
                if not symbol or side not in ("BUY", "SELL") or qty <= 0:
                    continue
                signed = qty if side == "BUY" else -qty
                old_qty, old_avg = states.get(key, (0.0, 0.0))
                if old_qty == 0 or old_qty * signed > 0:
                    total = old_qty + signed
                    avg = ((abs(old_qty) * old_avg + abs(signed) * px) /
                           abs(total)) if total else 0.0
                    states[key] = (total, avg)
                else:
                    remainder = old_qty + signed
                    if abs(remainder) < 1e-8:
                        states.pop(key, None)
                    elif old_qty * remainder > 0:
                        states[key] = (remainder, old_avg)
                    else:
                        states[key] = (remainder, px)

            for trader, state_key in (
                ("BullishExecutor_Paper", "paper_engine_state_bull"),
                ("BearishExecutor_Paper", "paper_engine_state_bear"),
            ):
                row = c.execute(
                    "SELECT value FROM kv_state WHERE key=?", (state_key,)
                ).fetchone()
                snapshot = json.loads(row[0]) if row else {}
                actual = {}
                for bucket in ("positions", "option_positions"):
                    for position in (snapshot.get(bucket) or {}).values():
                        symbol = str(position.get("symbol") or "")
                        signed = float(position.get("qty") or 0)
                        if str(position.get("side") or "").upper() == "SELL":
                            signed = -signed
                        actual[symbol] = (signed, float(position.get("avg_price") or 0))
                expected = {
                    symbol: value for (owner, symbol), value in states.items()
                    if owner == trader and abs(value[0]) > 1e-8
                }
                if expected != actual:
                    issues.append({
                        "trader": trader, "type": "position_mismatch",
                        "ledger": expected, "kv_state": actual,
                    })
                ledger_pnl = round(pnl_by_trader.get(trader, 0.0), 2)
                state_pnl = round(float(snapshot.get("realized_pnl") or 0), 2)
                if abs(ledger_pnl - state_pnl) > 0.01:
                    issues.append({
                        "trader": trader, "type": "realized_pnl_mismatch",
                        "ledger": ledger_pnl, "kv_state": state_pnl,
                    })
        return issues

    async def reconcile_paper_state(self) -> list:
        if not self._db_path:
            return [{"type": "db_unavailable"}]
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_pool, self._mariadb_reconcile_paper_state)

    # ── Public KV API ──────────────────────────────────────────────────────

    async def set(self, key: str, value: Any):
        self._cache[key] = value
        loop = asyncio.get_running_loop()
        if self._db_path:
            try:
                await loop.run_in_executor(_pool, self._mariadb_write, key, value)
            except Exception as e:
                log.error(f"MariaDB save [{key}]: {e}")

    def get(self, key: str, default=None) -> Any:
        return self._cache.get(key, default)

    # ── Paper Trades (structured, per-trader) ─────────────────────────────

    def _mariadb_insert_paper_trade(self, row: dict):
        with dbapi.connect(self._db_path) as c:
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

    def _mariadb_has_atomic_paper_trade(self, row: dict) -> bool:
        with dbapi.connect(self._db_path) as c:
            return bool(c.execute(
                """SELECT 1 FROM paper_trades
                   WHERE trader_name=? AND action=? AND symbol=? AND side=?
                     AND ABS(qty-?)<1e-9 AND ABS(fill_price-?)<1e-9
                     AND notes='atomic_paper_fill' AND ts_utc>=? LIMIT 1""",
                (
                    row["trader_name"], row["action"], row.get("symbol", ""),
                    row.get("side", ""), row.get("qty", 0),
                    row.get("fill_price", 0), time.time() - 10,
                ),
            ).fetchone())

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
            if await loop.run_in_executor(_pool, self._mariadb_has_atomic_paper_trade, row):
                return row
            for attempt in range(3):   # 3 attempts before giving up
                try:
                    await loop.run_in_executor(_pool, self._mariadb_insert_paper_trade, row)
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

    def _mariadb_get_paper_trades(self, trader_name: str, limit: int) -> list:
        with dbapi.connect(self._db_path) as c:
            c.row_factory = dbapi.Row
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
            return await loop.run_in_executor(_pool, self._mariadb_get_paper_trades,
                                              trader_name, limit)
        except Exception as e:
            log.error(f"get_paper_trades error: {e}")
            return []

    # ── Trading Sessions ──────────────────────────────────────────────────

    def _mariadb_upsert_session(self, row: dict):
        with dbapi.connect(self._db_path) as c:
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
                await loop.run_in_executor(_pool, self._mariadb_upsert_session, row)
            except Exception as e:
                log.error(f"save_session error: {e}")
        await _mirror_to_frappe("session", row)

    def _mariadb_get_sessions(self, trader_name: str, limit: int) -> list:
        with dbapi.connect(self._db_path) as c:
            c.row_factory = dbapi.Row
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
            return await loop.run_in_executor(_pool, self._mariadb_get_sessions,
                                              trader_name, limit)
        except Exception as e:
            log.error(f"get_sessions error: {e}")
            return []

    def _mariadb_get_sessions_filtered(self, trader_name: str, from_date: str, to_date: str, limit: int, today_ist: str = "") -> list:
        with dbapi.connect(self._db_path) as c:
            c.row_factory = dbapi.Row

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
                _pool, self._mariadb_get_sessions_filtered,
                trader_name, from_date, to_date, limit, today_ist
            )
        except Exception:
            return []

    def _mariadb_get_trades_by_session(self, session_id: str) -> list:
        with dbapi.connect(self._db_path) as c:
            c.row_factory = dbapi.Row
            rows = c.execute("SELECT * FROM paper_trades WHERE session_id=? ORDER BY ts_utc ASC", (session_id,)).fetchall()
        return [dict(r) for r in rows]

    async def get_trades_by_session(self, session_id: str) -> list:
        if not self._db_path: return []
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(_pool, self._mariadb_get_trades_by_session, session_id)
        except Exception:
            return []

    # ── Delete Trader History ─────────────────────────────────────────────

    def _mariadb_delete_trader_history(self, trader_name: str):
        with dbapi.connect(self._db_path) as c:
            c.execute("DELETE FROM paper_trades WHERE trader_name=?", (trader_name,))
            c.execute("DELETE FROM trading_sessions WHERE trader_name=?", (trader_name,))
            # Also wipe persisted executor state so it doesn't reload stale PnL on restart
            c.execute("DELETE FROM kv_state WHERE key=?", (f"{trader_name}_state",))

    async def delete_trader_history(self, trader_name: str):
        if not self._db_path: return
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(_pool, self._mariadb_delete_trader_history, trader_name)
            # Also remove from kv_state cache for this trader
            self._cache.pop(f"{trader_name}_state", None)
        except Exception as e:
            log.error(f"delete_trader_history error: {e}")

    # ── Mark Session Force-Closed ─────────────────────────────────────────

    def _mariadb_mark_force_closed(self, session_id: str):
        with dbapi.connect(self._db_path) as c:
            c.execute(
                "UPDATE trading_sessions SET is_force_closed=1, status='done' WHERE session_id=?",
                (session_id,)
            )

    async def mark_session_force_closed(self, session_id: str):
        if not self._db_path or not session_id: return
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(_pool, self._mariadb_mark_force_closed, session_id)
        except Exception as e:
            log.error(f"mark_session_force_closed error: {e}")

    def _mariadb_mark_session_expired(self, session_id: str, close_ts_ist: str):
        """Close a stale session without inventing a historical exit price/PnL."""
        with dbapi.connect(self._db_path) as c:
            c.execute(
                """UPDATE trading_sessions
                   SET status='expired_unreconciled',
                       close_reason='contract_expired_while_engine_offline',
                       close_ts_ist=COALESCE(NULLIF(close_ts_ist, ''), ?)
                   WHERE session_id=? AND status IN ('open', 'running')""",
                (close_ts_ist, session_id),
            )

    async def mark_session_expired(self, session_id: str, close_ts_ist: str):
        if not self._db_path or not session_id:
            return
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                _pool, self._mariadb_mark_session_expired, session_id, close_ts_ist
            )
        except Exception as e:
            log.error(f"mark_session_expired error: {e}")

    # ── Per-trader Config Defaults ─────────────────────────────────────────

    def _mariadb_save_trader_cfg(self, trader_name: str, cfg: dict):
        with dbapi.connect(self._db_path) as c:
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
                await loop.run_in_executor(_pool, self._mariadb_save_trader_cfg,
                                           trader_name, config)
            except Exception as e:
                log.error(f"save_trader_config error: {e}")

    def _mariadb_get_trader_cfg(self, trader_name: str) -> Optional[dict]:
        with dbapi.connect(self._db_path) as c:
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
            return await loop.run_in_executor(_pool, self._mariadb_get_trader_cfg, trader_name)
        except Exception as e:
            log.error(f"get_trader_config error: {e}")
            return None

    # ── Legacy trade_log (kept for backward-compat) ────────────────────────

    def _mariadb_log_trade_legacy(self, row: dict):
        with dbapi.connect(self._db_path) as c:
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
                def _already_atomic():
                    with dbapi.connect(self._db_path) as c:
                        return bool(c.execute(
                            """SELECT 1 FROM trade_log
                               WHERE executor=? AND action=? AND instrument=? AND side=?
                                 AND ABS(qty-?)<1e-9 AND ABS(price-?)<1e-9
                                 AND detail LIKE '%atomic_paper_fill%'
                                 AND ts_utc>=? LIMIT 1""",
                            (executor, action, instrument, side, qty, price,
                             time.time() - 10),
                        ).fetchone())
                if not (is_paper and await loop.run_in_executor(_pool, _already_atomic)):
                    await loop.run_in_executor(_pool, self._mariadb_log_trade_legacy, row)
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
                with dbapi.connect(self._db_path) as c:
                    c.row_factory = dbapi.Row
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

    def _mariadb_insert_session_event(self, row: dict):
        with dbapi.connect(self._db_path) as c:
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

    def _mariadb_get_session_events(self, trader_name: str,
                                    session_date: str, limit: int) -> list:
        with dbapi.connect(self._db_path) as c:
            c.row_factory = dbapi.Row
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
                await loop.run_in_executor(_pool, self._mariadb_insert_session_event, row)
            except Exception as e:
                log.error(f"save_session_event error: {e}")
        await _mirror_to_frappe("session_event", row)

    def _mariadb_get_session_events_by_id(self, session_id: str) -> list:
        """
        Get session_event_log rows for a session.
        Handles session_id formats:
          - New format:  'exp_140626_1330_bull' → direct session_id column lookup
          - No-trade:    'notrade_BullishExecutor_Paper_2026-06-07' → parse trader+date
          - Legacy trade:'2026-06-08_00-05-04_IST' → date-based lookup
        """
        from datetime import datetime, timedelta
        with dbapi.connect(self._db_path) as c:
            c.row_factory = dbapi.Row

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
                _pool, self._mariadb_get_session_events_by_id, session_id
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
                _pool, self._mariadb_get_session_events,
                trader_name, session_date, limit
            )
        except Exception as e:
            log.error(f"get_session_events error: {e}")
            return []

    def _mariadb_get_session_dates(self, trader_name: str, limit: int) -> list:
        with dbapi.connect(self._db_path) as c:
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
                _pool, self._mariadb_get_session_dates, trader_name, limit
            )
        except Exception as e:
            return []

    # ── PostgreSQL stubs ────────────────────────────────────────────────────


store = StateStore()

