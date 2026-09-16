import asyncio
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from readonly_mcp import ReadOnlyStore, TABLES


class ReadOnlyMCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.db"
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TABLE hedge_fills (id INTEGER PRIMARY KEY, session_id INTEGER, trader_leg TEXT, side TEXT, fill_price REAL, fill_qty REAL, fee REAL, created_at TEXT)")
            connection.executemany("INSERT INTO hedge_fills VALUES (?, ?, 'bull', 'BUY', 100, 1, 0, '2026-09-08')", [(1, 1), (2, 2), (3, 1)])
            connection.execute("CREATE TABLE hedge_config (key TEXT, value TEXT)")
            connection.executemany("INSERT INTO hedge_config VALUES (?, ?)", [("TRADE_QTY", "0.1"), ("BINANCE_API_KEY", "secret"), ("NOTES", "private"), ("NEW_SECRET", "private")])
            connection.execute("CREATE TABLE users (password_hash TEXT)")
            connection.commit()
        self.store = ReadOnlyStore(self.path)

    def test_pagination_and_filter(self):
        page = self.store.read_records("hedge_fills", limit=1, session_id=1)
        self.assertEqual(page["rows"][0]["id"], 3)
        self.assertEqual(page["next_offset"], 1)
        page = self.store.read_records("hedge_fills", limit=1, offset=1, session_id=1)
        self.assertEqual(page["rows"][0]["id"], 1)
        self.assertIsNone(page["next_offset"])

    def test_configuration_excludes_secrets_and_unknown_keys(self):
        self.assertEqual(self.store.read_configuration("hedge")["configuration"], {"TRADE_QTY": "0.1"})

    def test_denies_writes_and_non_allowlisted_reads(self):
        before = hashlib.sha256(self.path.read_bytes()).digest()
        with self.store.connect() as connection:
            for sql in ["DELETE FROM hedge_fills", "DROP TABLE hedge_fills", "CREATE TABLE evil (id)", "PRAGMA query_only=OFF", "SELECT password_hash FROM users", "ATTACH DATABASE ':memory:' AS evil", "SELECT load_extension('evil')"]:
                with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(sql)
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).digest())

    def test_validation_and_missing_database(self):
        for dataset in ["users", "hedge_fills; DROP TABLE users", "hedge_config"]:
            with self.assertRaises(ValueError):
                self.store.read_records(dataset)
        for options in [{"limit": 201}, {"limit": 0}, {"offset": -1}, {"offset": 100001}, {"session_id": "1 OR 1=1"}]:
            with self.assertRaises(ValueError):
                self.store.read_records("hedge_fills", **options)
        missing = self.path.parent / "missing.db"
        with self.assertRaises(FileNotFoundError):
            ReadOnlyStore(missing)
        self.assertFalse(missing.exists())

    def test_stdio_protocol(self):
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            self.skipTest("Install requirements-mcp.txt to run the MCP protocol test")

        async def check():
            parameters = StdioServerParameters(command=sys.executable, args=[
                "-B", str(Path(__file__).resolve().parents[1] / "readonly_mcp.py"),
                "--database", str(self.path),
            ])
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    self.assertEqual({tool.name for tool in listing.tools}, {"list_datasets", "read_records", "read_configuration"})
                    self.assertTrue(all(tool.annotations.readOnlyHint for tool in listing.tools))
                    catalog = await session.call_tool("list_datasets", {})
                    self.assertFalse(catalog.isError)
                    result = await session.call_tool("read_records", {"dataset": "hedge_fills", "limit": 1})
                    self.assertFalse(result.isError)
                    self.assertEqual(result.structuredContent["rows"][0]["id"], 3)
                    denied = await session.call_tool("read_records", {"dataset": "users"})
                    self.assertTrue(denied.isError)
                    config = await session.call_tool("read_configuration", {"strategy": "hedge"})
                    self.assertEqual(config.structuredContent["configuration"], {"TRADE_QTY": "0.1"})
        asyncio.run(asyncio.wait_for(check(), timeout=30))


if __name__ == "__main__":
    unittest.main()
