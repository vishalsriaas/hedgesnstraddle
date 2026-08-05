"""Small DB-API compatibility layer for the embedded hedge runtime.

The strategy was originally written against sqlite's connection.execute API.
This module preserves that API while making MariaDB (the Frappe site database)
the only durable store.  Each call obtains its own MariaDB connection, which is
important because the legacy state store performs IO in executor threads.
"""

from __future__ import annotations

import os
import re
from typing import Any

Row = dict

_TABLES = {
    "kv_state": "hedge_kv_state",
    "paper_trades": "hedge_paper_trades",
    "trading_sessions": "hedge_trading_sessions",
    "trader_config": "hedge_trader_config",
    "system_events": "hedge_system_events",
    "trade_log": "hedge_trade_log",
    "session_event_log": "hedge_session_event_log",
    "frappe_sync_cursor": "hedge_frappe_sync_cursor",
}


class FlexRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class Result:
    def __init__(self, cursor):
        self.rowcount = cursor.rowcount
        self.lastrowid = cursor.lastrowid
        rows = cursor.fetchall() if cursor.description else ()
        self._rows = [FlexRow(row) for row in rows]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


def _translate(sql: str) -> str:
    value = sql
    for source, target in sorted(_TABLES.items(), key=lambda item: -len(item[0])):
        value = re.sub(rf"\b{re.escape(source)}\b", f"`{target}`", value)
    value = value.replace("?", "%s")
    value = re.sub(r"\bSELECT\s+key\s*,", "SELECT `key`,", value, flags=re.I)
    value = re.sub(r"\bWHERE\s+key\b", "WHERE `key`", value, flags=re.I)
    value = re.sub(r"\blower\s*\(\s*key\s*\)", "lower(`key`)", value, flags=re.I)
    value = re.sub(r"\(\s*key\s*,", "(`key`,", value, flags=re.I)
    value = re.sub(r"INSERT\s+OR\s+IGNORE", "INSERT IGNORE", value, flags=re.I)
    value = re.sub(r"INSERT\s+OR\s+REPLACE", "REPLACE", value, flags=re.I)
    value = re.sub(
        r"ON\s+CONFLICT\s*\(\s*table_name\s*\)\s*DO\s+UPDATE\s+SET\s+last_id\s*=\s*excluded\.last_id",
        "ON DUPLICATE KEY UPDATE last_id=VALUES(last_id)",
        value,
        flags=re.I,
    )
    value = re.sub(
        r"ON\s+CONFLICT\s*\(\s*trader_name\s*\)\s*DO\s+UPDATE\s+SET\s+"
        r"config_json\s*=\s*excluded\.config_json\s*,\s*updated_at\s*=\s*excluded\.updated_at",
        "ON DUPLICATE KEY UPDATE config_json=VALUES(config_json), updated_at=VALUES(updated_at)",
        value,
        flags=re.I | re.S,
    )
    if re.search(r"ON\s+CONFLICT\s*\(\s*session_id\s*\)\s*DO\s+UPDATE", value, re.I):
        value = re.sub(
            r"ON\s+CONFLICT\s*\(\s*session_id\s*\)\s*DO\s+UPDATE\s+SET",
            "ON DUPLICATE KEY UPDATE",
            value,
            flags=re.I,
        )
        value = re.sub(r"\bexcluded\.([a-z_]+)\b", r"VALUES(\1)", value, flags=re.I)
        value = re.sub(r"\btrading_sessions\.([a-z_]+)\b", r"\1", value, flags=re.I)
    value = re.sub(r"\bBEGIN\s+IMMEDIATE\b", "START TRANSACTION", value, flags=re.I)
    return value


class Connection:
    def __init__(self, **kwargs):
        import pymysql
        self._conn = pymysql.connect(
            host=os.environ["MARIADB_HOST"],
            port=int(os.environ.get("MARIADB_PORT", "3306")),
            user=os.environ["MARIADB_USER"],
            password=os.environ["MARIADB_PASSWORD"],
            database=os.environ["MARIADB_DATABASE"],
            charset="utf8mb4",
            autocommit=False,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10,
        )
        self.row_factory = Row

    def execute(self, sql: str, params: Any = ()):
        if sql.strip().upper().startswith("PRAGMA"):
            return Result(_EmptyCursor())
        cursor = self._conn.cursor()
        translated = _translate(sql)
        if params:
            cursor.execute(translated, params)
        else:
            cursor.execute(translated)
        return Result(cursor)

    def executescript(self, _sql: str):
        # Schema is created explicitly by ensure_schema().
        return None

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()


class _EmptyCursor:
    rowcount = 0
    lastrowid = None
    description = None

    @staticmethod
    def fetchall():
        return ()


def connect(_path: str | None = None, **kwargs) -> Connection:
    return Connection(**kwargs)


def ensure_schema() -> None:
    statements = [
        """CREATE TABLE IF NOT EXISTS `hedge_kv_state` (
            `key` VARCHAR(190) PRIMARY KEY, value LONGTEXT NOT NULL,
            updated_at DOUBLE, INDEX idx_hkv_updated(updated_at)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS `hedge_paper_trades` (
            id BIGINT PRIMARY KEY AUTO_INCREMENT, trader_name VARCHAR(190) NOT NULL,
            session_id VARCHAR(190), ts_ist VARCHAR(32) NOT NULL, ts_utc DOUBLE NOT NULL,
            action VARCHAR(100) NOT NULL, symbol VARCHAR(190), side VARCHAR(16),
            qty DOUBLE DEFAULT 0, fill_price DOUBLE DEFAULT 0, pnl DOUBLE DEFAULT 0,
            notes LONGTEXT, created_at DOUBLE DEFAULT (UNIX_TIMESTAMP()),
            INDEX idx_pt_trader(trader_name,ts_utc), INDEX idx_pt_session(session_id)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS `hedge_trading_sessions` (
            id BIGINT PRIMARY KEY AUTO_INCREMENT, session_id VARCHAR(190) UNIQUE NOT NULL,
            trader_name VARCHAR(190) NOT NULL, session_date VARCHAR(16) NOT NULL,
            target_line DOUBLE, entry_zone VARCHAR(100), entry_price DOUBLE,
            entry_ts_ist VARCHAR(32), close_reason VARCHAR(255), close_ts_ist VARCHAR(32),
            futures_pnl DOUBLE DEFAULT 0, hedge_pnl DOUBLE DEFAULT 0, total_pnl DOUBLE DEFAULT 0,
            status VARCHAR(40) DEFAULT 'open', created_at DOUBLE DEFAULT (UNIX_TIMESTAMP()),
            balance_before DOUBLE, balance_after DOUBLE, window_open_ts VARCHAR(32),
            display_name VARCHAR(190), INDEX idx_ts_trader(trader_name,created_at)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS `hedge_trader_config` (
            trader_name VARCHAR(190) PRIMARY KEY, config_json LONGTEXT NOT NULL,
            updated_at DOUBLE) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS `hedge_system_events` (
            id BIGINT PRIMARY KEY AUTO_INCREMENT, event_type VARCHAR(100), source VARCHAR(190),
            message LONGTEXT, ts_ist VARCHAR(32), ts_utc DOUBLE) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS `hedge_trade_log` (
            id BIGINT PRIMARY KEY AUTO_INCREMENT, ts_utc DOUBLE, ts_ist VARCHAR(32),
            executor VARCHAR(190), action VARCHAR(100), instrument VARCHAR(190),
            side VARCHAR(16), qty DOUBLE, price DOUBLE, pnl DOUBLE, status VARCHAR(40),
            detail LONGTEXT, is_paper TINYINT DEFAULT 1,
            INDEX idx_htl_exec(executor,ts_utc)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS `hedge_session_event_log` (
            id BIGINT PRIMARY KEY AUTO_INCREMENT, trader_name VARCHAR(190) NOT NULL,
            session_date VARCHAR(16) NOT NULL, event_ts_ist VARCHAR(32) NOT NULL,
            event_type VARCHAR(100) NOT NULL, state VARCHAR(100), price DOUBLE,
            locked_line DOUBLE, message LONGTEXT, created_at DOUBLE DEFAULT (UNIX_TIMESTAMP()), session_id VARCHAR(190),
            INDEX idx_sel_trader_date(trader_name,session_date),
            INDEX idx_sel_session_id(session_id)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS `hedge_frappe_sync_cursor` (
            table_name VARCHAR(190) PRIMARY KEY, last_id BIGINT NOT NULL DEFAULT 0) ENGINE=InnoDB""",
    ]
    with connect() as connection:
        for statement in statements:
            connection.execute(statement)
