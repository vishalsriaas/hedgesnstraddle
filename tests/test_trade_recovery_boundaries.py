"""Regression cases for the previously reported late-publication recovery defects.

python -m unittest discover -s tests -p test_trade_recovery_boundaries.py -v
No network access or application database writes.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from app.core import futures_targets as ft


class RecoveryBoundaryAudit(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 9, 17, tzinfo=timezone.utc)
        self.ms = ft.millis(self.start)
        self.order = SimpleNamespace(id=1, status='PENDING', symbol='BTC-USDT-FUTURES',
                                     price=76580., qty=1., side='SELL')
        self.event = SimpleNamespace(payload_json=json.dumps(dict(order_id=1,
            symbol='BTCUSDT', active_ms=self.ms, checked_ms=self.ms, last_id=None,
            last_trade_ms=None, last_poll_ms=0, limit_price=76580., qty=1., side='SELL')),
            message='')

    def replay(self, seconds, deadline=None):
        return asyncio.run(ft.replay_target(self.order, self.event,
            self.start + timedelta(seconds=seconds), until=deadline))

    def test_delayed_first_trade_is_recovered_after_initial_empty_response(self):
        with patch.object(ft, 'get_futures_aggregate_trades', AsyncMock(return_value=[])):
            self.replay(5)
        late = dict(a=100, T=self.ms+4000, p='76585', q='1')

        async def history(symbol, **kwargs):
            return [late] if kwargs.get('from_id') is not None or kwargs['start_ms'] <= late['T'] <= kwargs['end_ms'] else []

        with patch.object(ft, 'get_futures_aggregate_trades', history):
            hit = self.replay(10)
        self.assertIsNotNone(hit, 'Late first crossing skipped because empty bootstrap advanced startTime')

    def test_late_crossing_before_deadline_is_not_skipped(self):
        deadline = self.start + timedelta(seconds=5)
        with patch.object(ft, 'get_futures_aggregate_trades', AsyncMock(return_value=[
                dict(a=100, T=self.ms+2000, p='76570', q='1')])):
            self.replay(5, deadline)
        fetch = AsyncMock(return_value=[dict(a=101, T=self.ms+4000, p='76585', q='1')])
        with patch.object(ft, 'get_futures_aggregate_trades', fetch):
            hit = self.replay(10, deadline)
        self.assertIsNotNone(hit, 'Deadline coverage suppresses the follow-up fetch of a late crossing')

    def test_second_page_failure_does_not_advance_persisted_cursor(self):
        before = self.event.payload_json
        fetch = AsyncMock(side_effect=[[
            dict(a=100, T=self.ms+2000, p='76570', q='1')], ValueError('page 2 failed')])
        with patch.object(ft, 'PAGE_LIMIT', 1), patch.object(ft, 'get_futures_aggregate_trades', fetch):
            with self.assertRaises(ValueError):
                self.replay(5)
        self.assertEqual(self.event.payload_json, before)
