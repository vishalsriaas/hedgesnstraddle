"""MariaDB DB-API adapter used by the embedded straddle runtime/dashboard."""

from __future__ import annotations

import os
import re
from typing import Any

Row = dict
_NAMES = (
    "pnl_snapshots", "wallet_ledger", "bot_status", "sessions", "orders",
    "events", "fills", "config",
)


class FlexRow(dict):
    def __getitem__(self, key):
        return tuple(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class Result:
    def __init__(self, cursor):
        self.rowcount, self.lastrowid = cursor.rowcount, cursor.lastrowid
        self._rows = [FlexRow(row) for row in cursor.fetchall()] if cursor.description else []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


def _translate(sql: str) -> str:
    value = sql
    for name in sorted(_NAMES, key=len, reverse=True):
        value = re.sub(rf"\b{name}\b", f"`straddle_{name}`", value)
    value = value.replace("?", "%s")
    value = re.sub(r"\bSELECT\s+key\s*,", "SELECT `key`,", value, flags=re.I)
    value = re.sub(r"\bWHERE\s+key\b", "WHERE `key`", value, flags=re.I)
    value = re.sub(r"\blower\s*\(\s*key\s*\)", "lower(`key`)", value, flags=re.I)
    value = re.sub(r"\(\s*key\s*,", "(`key`,", value, flags=re.I)
    value = re.sub(r"INSERT\s+OR\s+IGNORE", "INSERT IGNORE", value, flags=re.I)
    value = re.sub(r"INSERT\s+OR\s+REPLACE", "REPLACE", value, flags=re.I)
    value = re.sub(
        r"REPLACE\s+INTO\s+`straddle_bot_status`[\s\n]*\((.*?)\)[\s\n]*VALUES[\s\n]*\((.*?)\)",
        r"INSERT INTO `straddle_bot_status` (\1) VALUES (\2) ON DUPLICATE KEY UPDATE ts=VALUES(ts), state=VALUES(state), btc_mark=VALUES(btc_mark), session_id=VALUES(session_id), detail=VALUES(detail)",
        value, flags=re.I
    )
    value = re.sub(r"\bAS\s+TEXT\b", "AS CHAR", value, flags=re.I)
    return re.sub(r"\bBEGIN\s+IMMEDIATE\b", "START TRANSACTION", value, flags=re.I)


class Connection:
    def __init__(self, **_kwargs):
        self._kwargs = _kwargs
        self._connect()

    def _connect(self):
        import pymysql
        self._conn = pymysql.connect(
            host=os.environ["MARIADB_HOST"], port=int(os.environ.get("MARIADB_PORT", "3306")),
            user=os.environ["MARIADB_USER"], password=os.environ["MARIADB_PASSWORD"],
            database=os.environ["MARIADB_DATABASE"], charset="utf8mb4", autocommit=False,
            cursorclass=pymysql.cursors.DictCursor, connect_timeout=10,
        )
        self.row_factory = Row

    def _ensure_conn(self):
        try:
            self._conn.ping(reconnect=True)
        except Exception:
            try:
                self._connect()
            except Exception:
                pass

    def execute(self, sql: str, params: Any = ()):
        if sql.strip().upper().startswith("PRAGMA"):
            return Result(_EmptyCursor())
        self._ensure_conn()
        translated = _translate(sql)
        try:
            cursor = self._conn.cursor()
            if params:
                cursor.execute(translated, params)
            else:
                cursor.execute(translated)
            return Result(cursor)
        except Exception:
            self._ensure_conn()
            cursor = self._conn.cursor()
            if params:
                cursor.execute(translated, params)
            else:
                cursor.execute(translated)
            return Result(cursor)

    def executescript(self, _sql: str):
        return None

    def commit(self):
        try:
            self._conn.commit()
        except Exception:
            self._ensure_conn()
            try:
                self._conn.commit()
            except Exception:
                pass

    def rollback(self):
        try:
            self._conn.rollback()
        except Exception:
            pass

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb):
        self.rollback() if exc_type else self.commit()
        self.close()


class _EmptyCursor:
    rowcount = 0
    lastrowid = None
    description = None

    @staticmethod
    def fetchall():
        return ()


def connect(_path: str | None = None, **kwargs):
    return Connection(**kwargs)


def ensure_schema() -> None:
    ddl = [
        """CREATE TABLE IF NOT EXISTS `straddle_config` (
            `key` VARCHAR(190) PRIMARY KEY, value LONGTEXT, label VARCHAR(255),
            input_type VARCHAR(40) DEFAULT 'text', is_sensitive TINYINT DEFAULT 0,
            section VARCHAR(80), sort_order INT) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS `straddle_sessions` (
            id BIGINT PRIMARY KEY AUTO_INCREMENT, date VARCHAR(16), expiry_sym VARCHAR(40),
            expiry_dt VARCHAR(32), state VARCHAR(40) DEFAULT 'ACTIVE', call_sym VARCHAR(190),
            put_sym VARCHAR(190), call_fill DOUBLE DEFAULT 0, put_fill DOUBLE DEFAULT 0,
            call_qty DOUBLE DEFAULT 0, put_qty DOUBLE DEFAULT 0, total_premium DOUBLE DEFAULT 0,
            long_entry DOUBLE DEFAULT 0, short_entry DOUBLE DEFAULT 0, long_qty DOUBLE DEFAULT 0,
            short_qty DOUBLE DEFAULT 0, long_tp_px DOUBLE DEFAULT 0, short_tp_px DOUBLE DEFAULT 0,
            long_liq_px DOUBLE DEFAULT 0, short_liq_px DOUBLE DEFAULT 0, margin_used DOUBLE DEFAULT 0,
            entry_btc DOUBLE DEFAULT 0, tp_dist DOUBLE DEFAULT 0, wallet_before DOUBLE DEFAULT 0,
            sq_call_exit DOUBLE DEFAULT 0, sq_put_exit DOUBLE DEFAULT 0, sq_long_exit DOUBLE DEFAULT 0,
            sq_short_exit DOUBLE DEFAULT 0, options_pnl DOUBLE DEFAULT 0, futures_pnl DOUBLE DEFAULT 0,
            net_pnl DOUBLE DEFAULT 0, sq_type VARCHAR(80), wallet_after DOUBLE DEFAULT 0,
            entry_ts_ms BIGINT DEFAULT 0, start_dt VARCHAR(32), end_dt VARCHAR(32),
            INDEX idx_sessions_state(state), INDEX idx_sessions_expiry(expiry_dt)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS `straddle_orders` (
            id BIGINT PRIMARY KEY AUTO_INCREMENT, session_id BIGINT, paper_order_id BIGINT,
            symbol VARCHAR(190), asset_type VARCHAR(30), leg_label VARCHAR(60), side VARCHAR(16),
            order_type VARCHAR(30), qty DOUBLE, limit_price DOUBLE DEFAULT 0,
            fill_price DOUBLE DEFAULT 0, status VARCHAR(40), placed_at VARCHAR(32),
            filled_at VARCHAR(32), cancel_reason VARCHAR(255), updated_at VARCHAR(32),
            INDEX idx_orders_session(session_id), INDEX idx_orders_status(session_id,status)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS `straddle_pnl_snapshots` (
            id BIGINT PRIMARY KEY AUTO_INCREMENT, session_id BIGINT, ts VARCHAR(32), btc_mark DOUBLE,
            call_mark DOUBLE, put_mark DOUBLE, call_upnl DOUBLE, put_upnl DOUBLE, long_upnl DOUBLE,
            short_upnl DOUBLE, total_upnl DOUBLE, long_liq DOUBLE, short_liq DOUBLE,
            margin_used DOUBLE, INDEX idx_snapshots_sess(session_id)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS `straddle_events` (
            id BIGINT PRIMARY KEY AUTO_INCREMENT, session_id BIGINT, ts VARCHAR(32),
            event_type VARCHAR(100), detail LONGTEXT, INDEX idx_events_session(session_id)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS `straddle_wallet_ledger` (
            id BIGINT PRIMARY KEY AUTO_INCREMENT, ts VARCHAR(32) NOT NULL, session_id BIGINT DEFAULT 0,
            type VARCHAR(60) NOT NULL, amount DOUBLE NOT NULL, balance_after DOUBLE NOT NULL,
            note LONGTEXT, INDEX idx_ledger_session(session_id), INDEX idx_ledger_type(type)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS `straddle_bot_status` (
            id BIGINT PRIMARY KEY DEFAULT 1, ts VARCHAR(32), state VARCHAR(60), btc_mark DOUBLE,
            session_id BIGINT, detail LONGTEXT) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS `straddle_fills` (
            id BIGINT PRIMARY KEY AUTO_INCREMENT, session_id BIGINT, ts VARCHAR(32),
            instrument VARCHAR(80), side VARCHAR(16), qty DOUBLE, ask_at_order DOUBLE,
            fill_price DOUBLE, slippage DOUBLE, order_id VARCHAR(190), note LONGTEXT,
            INDEX idx_fills_session(session_id)) ENGINE=InnoDB""",
    ]
    with connect() as connection:
        for statement in ddl:
            connection.execute(statement)
