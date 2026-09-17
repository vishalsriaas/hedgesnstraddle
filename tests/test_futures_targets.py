"""Offline recovery and accounting tests; no working database or network access."""
import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker

from app.core import futures_targets as ft, binance_client as bc
from app.models.schema import Base, HedgeTradeOrder, HedgeSessionEvent
from app.database import migrate_execution_timestamps


class TimestampMigrationTests(unittest.TestCase):
    def test_additive_migration_preserves_history_and_is_repeatable(self):
        engine = create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            for table in ('straddle_trade_orders','hedge_trade_orders','straddle_fills','hedge_fills'):
                connection.execute(text(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY, price FLOAT, created_at TIMESTAMP)'))
                connection.execute(text(f"INSERT INTO {table} VALUES (1,76563.8468,'2026-09-17 17:05:02')"))
        migrate_execution_timestamps(engine)
        migrate_execution_timestamps(engine)
        with engine.connect() as connection:
            for table in ('straddle_trade_orders','hedge_trade_orders'):
                row = connection.execute(text(f'SELECT price,created_at,filled_at,processed_at,aggregate_trade_id FROM {table}')).one()
                self.assertEqual(tuple(row),(76563.8468,'2026-09-17 17:05:02',None,None,None))
            for table in ('straddle_fills','hedge_fills'):
                self.assertIn('order_id',{c['name'] for c in inspect(connection).get_columns(table)})


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.DB = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.DB()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.now = datetime(2026, 9, 17, 6, tzinfo=timezone.utc)
        self.order = HedgeTradeOrder(session_id=1, symbol='BTC-USDT-FUTURES',
            trader_leg='1st Trader', side='SELL', qty=10, price=76553.19,
            order_type='LIMIT', status='PENDING')
        self.event = ft.create_target(self.db, self.order, HedgeSessionEvent, self.now)
        self.db.commit()

    def row(self, trade_id, seconds, price=76500):
        return dict(a=trade_id, T=ft.millis(self.now) + int(seconds * 1000), p=str(price), q='1')

    def replay(self, seconds=5, until=None):
        return asyncio.run(ft.replay_target(self.order, self.event,
            self.now + timedelta(seconds=seconds), until=until))

    def test_cross_and_return_fills_at_locked_target_with_event_time(self):
        fetch = AsyncMock(return_value=[self.row(1, 1), self.row(2, 2, 76560), self.row(3, 4)])
        with patch.object(ft, 'get_futures_aggregate_trades', fetch):
            hit = self.replay()
        self.assertEqual(hit['fill_price'], 76553.19)
        self.assertEqual(hit['trade_time_ms'], ft.millis(self.now) + 2000)
        self.assertEqual(hit['processed_time_ms'], ft.millis(self.now) + 5000)
        self.assertEqual(hit['aggregate_trade_id'], 2)
        self.assertEqual(fetch.call_args.args, ('BTCUSDT',))

    def test_restart_resumes_last_id_and_duplicate_processing_is_safe(self):
        with patch.object(ft, 'get_futures_aggregate_trades', AsyncMock(return_value=[self.row(70, 2)])):
            self.assertIsNone(self.replay())
        self.db.commit()
        self.db.close()
        self.db = self.DB()
        self.addCleanup(self.db.close)
        self.order, self.event = ft.target_for_session(self.db, HedgeTradeOrder, HedgeSessionEvent, 1)
        fetch = AsyncMock(return_value=[self.row(71, 7, 76560)])
        with patch.object(ft, 'get_futures_aggregate_trades', fetch):
            hit = self.replay(10)
            self.assertEqual(self.replay(11), hit)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.kwargs['from_id'], 71)
        self.order.status = 'FILLED'
        self.assertIsNone(self.replay(20))

    def test_full_pages_same_timestamp_resume_without_skipping(self):
        # Reduced page size exercises the identical production pagination path.
        with patch.object(ft, 'PAGE_LIMIT', 2), patch.object(ft, 'MAX_PAGES', 1):
            with patch.object(ft, 'get_futures_aggregate_trades', AsyncMock(return_value=[self.row(10, 1), self.row(11, 1)])):
                self.assertIsNone(self.replay())
            self.db.commit()
            fetch = AsyncMock(return_value=[self.row(12, 1, 76560)])
            with patch.object(ft, 'get_futures_aggregate_trades', fetch):
                self.assertIsNotNone(self.replay(10))
            self.assertEqual(fetch.call_args.kwargs['from_id'], 12)

    def test_full_page_at_deadline_is_not_mistaken_for_complete_history(self):
        deadline = self.now + timedelta(seconds=5)
        with patch.object(ft, 'PAGE_LIMIT', 2), patch.object(ft, 'MAX_PAGES', 1):
            with patch.object(ft, 'get_futures_aggregate_trades', AsyncMock(return_value=[self.row(10, 5), self.row(11, 5)])):
                self.assertIsNone(self.replay(5, until=deadline))
            self.assertFalse(ft.replay_complete(self.event, deadline))
            with patch.object(ft, 'get_futures_aggregate_trades', AsyncMock(return_value=[self.row(12, 5, 76560)])):
                self.assertIsNotNone(self.replay(10, until=deadline))

    def test_paginates_all_pages_in_one_poll(self):
        fetch = AsyncMock(side_effect=[[self.row(1, 1), self.row(2, 2)], [self.row(3, 3, 76560)]])
        with patch.object(ft, 'PAGE_LIMIT', 2), patch.object(ft, 'get_futures_aggregate_trades', fetch):
            self.assertIsNotNone(self.replay())
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args.kwargs['from_id'], 3)

    def test_activation_boundary_is_not_backdated(self):
        fetch = AsyncMock(return_value=[self.row(1, 0, 76560), self.row(2, 1)])
        with patch.object(ft, 'get_futures_aggregate_trades', fetch):
            self.assertIsNone(self.replay())

    def test_buy_target_and_exact_touch(self):
        self.order.side = 'BUY'
        state = json.loads(self.event.payload_json)
        state['side'] = 'BUY'
        self.event.payload_json = json.dumps(state)
        with patch.object(ft, 'get_futures_aggregate_trades', AsyncMock(return_value=[self.row(1, 2, 76553.19)])):
            self.assertEqual(self.replay()['fill_price'], 76553.19)

    def test_cancelled_order_never_fetches_or_fills(self):
        self.order.status = 'CANCELLED'
        with patch.object(ft, 'get_futures_aggregate_trades', AsyncMock()) as fetch:
            self.assertIsNone(self.replay())
        fetch.assert_not_awaited()

    def test_limit_activated_after_cutoff_has_no_eligible_history(self):
        deadline = self.now - timedelta(milliseconds=1)
        with patch.object(ft,'get_futures_aggregate_trades',AsyncMock()) as fetch:
            self.assertIsNone(self.replay(5,until=deadline))
        self.assertTrue(ft.replay_complete(self.event,deadline))
        fetch.assert_not_awaited()

    def test_late_published_trade_recovers_by_id_after_covered_timestamp(self):
        with patch.object(ft, 'get_futures_aggregate_trades', AsyncMock(return_value=[self.row(10, 2)])):
            self.replay(5)
        with patch.object(ft, 'get_futures_aggregate_trades', AsyncMock(return_value=[self.row(11, 4, 76560)])):
            self.assertIsNotNone(self.replay(10))

    def test_errors_do_not_advance_cursor(self):
        original = self.event.payload_json
        with patch.object(ft, 'get_futures_aggregate_trades', AsyncMock(side_effect=ValueError('DATA_GAP'))):
            with self.assertRaises(ValueError):
                self.replay()
        self.assertEqual(self.event.payload_json, original)
        self.assertEqual(self.order.status, 'PENDING')

    def test_missing_id_is_not_silently_skipped(self):
        with patch.object(ft, 'get_futures_aggregate_trades', AsyncMock(return_value=[self.row(10, 2)])):
            self.replay()
        before = self.event.payload_json
        with patch.object(ft, 'get_futures_aggregate_trades', AsyncMock(return_value=[self.row(12, 7, 76560)])):
            with self.assertRaisesRegex(ValueError, 'Non-contiguous'):
                self.replay(10)
        self.assertEqual(self.event.payload_json, before)

    def test_identity_and_retention_fail_closed(self):
        self.order.symbol = 'ETHUSDT'
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            self.replay()
        self.order.symbol = 'BTC-USDT-FUTURES'
        self.order.price += 1
        with self.assertRaisesRegex(ValueError, 'locked terms'):
            self.replay()
        self.order.price -= 1
        with self.assertRaisesRegex(ValueError, 'retention'):
            self.replay(48 * 3600)

    def test_throttles_successful_polls_and_records_empty_interval(self):
        fetch = AsyncMock(return_value=[])
        with patch.object(ft, 'get_futures_aggregate_trades', fetch):
            self.replay(5)
            self.replay(6)
        self.assertEqual(fetch.call_count, 1)
        self.assertFalse(ft.replay_complete(self.event, self.now + timedelta(seconds=5)))

    def test_deadline_excludes_later_crossing(self):
        # First establish cursor so continuation uses fromId (without endTime).
        with patch.object(ft, 'get_futures_aggregate_trades', AsyncMock(return_value=[self.row(1, 2)])):
            self.replay(5)
        with patch.object(ft, 'get_futures_aggregate_trades', AsyncMock(return_value=[self.row(2, 12, 76560)])):
            self.assertIsNone(self.replay(20, until=self.now + timedelta(seconds=10)))
        self.assertTrue(ft.replay_complete(self.event, self.now + timedelta(seconds=10)))


class HistoryClientTests(unittest.TestCase):
    def setUp(self):
        self.old_backoff = bc._rate_limit_until.copy()
        bc._rate_limit_until.pop('FUTURES_TRADES', None)
        self.addCleanup(self.restore)

    def restore(self):
        bc._rate_limit_until.clear()
        bc._rate_limit_until.update(self.old_backoff)

    def fetch(self, response):
        client = AsyncMock()
        client.get.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = client
        with patch.object(bc.httpx, 'AsyncClient', return_value=context):
            result = asyncio.run(bc.get_futures_aggregate_trades('BTCUSDT', start_ms=1000, end_ms=2000))
        self.assertEqual(client.get.call_args.kwargs['params']['symbol'], 'BTCUSDT')
        return result

    def response(self, data, status=200):
        return httpx.Response(status, json=data, request=httpx.Request('GET', 'https://fapi.binance.com/fapi/v1/aggTrades'))

    def test_valid_price_history(self):
        rows = [dict(a=1, T=1500, p='76560', q='1')]
        self.assertEqual(self.fetch(self.response(rows)), rows)

    def test_rejects_mismatch_invalid_prices_stale_and_unordered_data(self):
        base = dict(a=1, T=1500, p='76560', q='1')
        for changes in ({'symbol': 'ETHUSDT'}, {'p': 'NaN'}, {'p': '0'}, {'T': 999}, {'q': '-1'}, {'a': '1'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.fetch(self.response([dict(base, **changes)]))
        with self.assertRaises(ValueError):
            self.fetch(self.response([base, dict(base, a=3)]))

    def test_rate_limit_has_no_cached_price_fallback(self):
        with self.assertRaises(ValueError):
            self.fetch(self.response({'code': -1003}, 429))
        with patch.object(bc.httpx, 'AsyncClient') as client:
            with self.assertRaisesRegex(ValueError, 'rate-limited'):
                asyncio.run(bc.get_futures_aggregate_trades('BTCUSDT', start_ms=1000, end_ms=2000))
        client.assert_not_called()

    def test_anchor_requires_id_and_valid_exchange_clock(self):
        with patch.object(bc,'get_futures_aggregate_trades',AsyncMock(return_value=[])):
            with self.assertRaisesRegex(ValueError,'baseline'):
                asyncio.run(bc.get_futures_trade_anchor())
        context=AsyncMock()
        context.__aenter__.return_value.get.return_value=self.response({'serverTime':2000})
        with patch.object(bc,'get_futures_aggregate_trades',AsyncMock(return_value=[dict(a=100,T=1900,p='76500',q='1')])), patch.object(bc.httpx,'AsyncClient',return_value=context), patch.object(bc.time,'time',return_value=1.5):
            anchor=asyncio.run(bc.get_futures_trade_anchor())
        self.assertEqual(anchor,dict(last_id=100,last_trade_ms=1900,active_ms=2000,clock_offset_ms=500))
