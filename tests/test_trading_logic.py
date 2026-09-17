"""Offline regression tests. Never start the app or touch its trading database.

Run: .venv/Scripts/python.exe -m unittest discover -s tests -v
"""
import asyncio
import json
from datetime import datetime, timezone, timedelta
import unittest
import time
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.core.straddle_engine as se
import app.core.hedge_engine as he
import app.core.binance_client as bc
import app.core.market_data as md
import app.core.futures_targets as ft
from app.models.schema import (
    Base, StraddleConfig, StraddleSession, StraddleTradeOrder, StraddleWalletLedger,
    HedgeConfig, HedgeStrategyConfig, HedgeSession, HedgeTradeOrder,
    HedgeOpenPosition, HedgePaperLedgerEntry, HedgeSessionEvent, HedgeRuntimeCommand,
    HedgeFill, StraddleFill, StraddleSessionEvent,
)


class Clock(datetime):
    day_now = 8
    hour_now = 6
    minute_now = 0
    elapsed_seconds = 0

    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 9, cls.day_now, cls.hour_now, cls.minute_now, tzinfo=he.ist) + timedelta(seconds=cls.elapsed_seconds)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


class TradingLogicTests(unittest.TestCase):
    def setUp(self):
        Clock.day_now = 8
        Clock.hour_now, Clock.minute_now, Clock.elapsed_seconds = 6, 0, 0
        self.engine = create_engine('sqlite://', poolclass=StaticPool,
                                    connect_args={'check_same_thread': False})
        Base.metadata.create_all(self.engine)
        self.DB = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.DB()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.price = 60000.0
        self.quotes = [dict(symbol=f'BTC-260908-{strike}-{side}', markPrice=str(100 + max(0, strike - 60000) if side == 'P' else 100 + max(0, 60000 - strike)))
                       for strike in (59500, 60000, 60500, 61000)
                       for side in ('C', 'P')]
        async def price():
            Clock.elapsed_seconds += 6
            return self.price
        async def quotes():
            return self.quotes
        async def trades(symbol, **kwargs):
            # Separate mock trade-history feed; dedicated tests cover differing marks.
            timestamp = ft.millis(Clock.now(he.ist)) - 1
            if kwargs.get('end_ms') is not None:
                timestamp = min(timestamp, kwargs['end_ms'])
            return [dict(a=kwargs.get('from_id') or 1, T=timestamp,
                         p=str(self.price), q='1')]
        patcher = patch.object(ft, 'get_futures_aggregate_trades', trades)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Schedule tests jump hours at once; allow replay to catch up in one cycle.
        patcher = patch.object(ft, 'MAX_PAGES', 48)
        patcher.start()
        self.addCleanup(patcher.stop)
        async def anchor():
            now = ft.millis(Clock.now(he.ist))
            return dict(last_id=0, last_trade_ms=now-1, active_ms=now, clock_offset_ms=0)
        for module in (se, he):
            for name, value in [('SessionLocal', self.DB), ('datetime', Clock),
                                ('get_futures_trade_anchor', anchor),
                                ('get_btc_spot_price', price),
                                ('get_btc_futures_mark_price', price),
                                ('get_btc_options_mark_prices', quotes)]:
                patcher = patch.object(module, name, value)
                patcher.start()
                self.addCleanup(patcher.stop)
        patcher = patch.object(md, 'datetime', Clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key, value in dict(BOT_ENABLED='1', SKIP_WEEKENDS='0', WINDOW_START='05:00',
                WINDOW_END='07:00', FUTURES_ENTRY_CUTOFF='11:00', SQ_END='12:30',
                TRADE_QTY='1', PAPER_WALLET_USDT='100000', MAX_TOTAL_MARK='400').items():
            self.db.add(StraddleConfig(key=key, value=value))
        for key, value in dict(BOT_ENABLED='1', ENGINE_ENABLED='1', GLOBAL_PAUSE='0',
                SKIP_WEEKENDS='0', MAX_OPTION_SPEND='400', PAPER_WALLET_USDT='100000').items():
            self.db.add(HedgeConfig(key=key, value=value))
        for role, start, end, close in [('1st Trader', 5, 7, 13), ('2nd Trader', 7, 11, 17)]:
            self.db.add(HedgeStrategyConfig(strategy_name=role, strategy_key=role,
                direction='Bullish', trade_start_h=start, trade_end_h=end,
                force_close_h=close, contract_qty=1, max_premium=250, max_time_value=229))
        self.db.commit()

    def config(self, model, key, value):
        row = self.db.query(model).filter_by(key=key).first()
        if row:
            row.value = str(value)
        else:
            self.db.add(model(key=key, value=str(value)))
        self.db.commit()

    def run_straddle(self, bot):
        async def stop(_):
            raise asyncio.CancelledError
        with patch.object(se.asyncio, 'sleep', stop):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(bot.run_loop())
        self.db.expire_all()

    def enter_straddle(self):
        bot = se.StraddleEngine()
        self.run_straddle(bot)
        self.assertEqual(bot.state, 'LIMITS_PLACED')
        return bot

    def hedge_entry(self, bot, role='1st Trader'):
        config = self.db.query(HedgeStrategyConfig).filter_by(strategy_name=role).one()
        return asyncio.run(bot.execute_slot_entry(self.db, role, config, self.price, self.price))

    def test_straddle_pending_prices_are_locked(self):
        bot = self.enter_straddle()
        self.price = 60300
        self.run_straddle(bot)
        order = self.db.query(StraddleTradeOrder).filter_by(leg_label='SHORT_LIMIT').one()
        self.assertEqual((order.price, order.status), (60200, 'FILLED'))
        self.assertEqual(bot.state, 'IN_TRADE')

    def test_reported_1740_crossing_has_exchange_fill_time_not_poll_time(self):
        Clock.day_now, Clock.hour_now, Clock.minute_now = 17, 17, 5
        for k, v in dict(WINDOW_START='17:05', WINDOW_END='18:00',
                         FUTURES_ENTRY_CUTOFF='18:30', SQ_END='19:00',
                         MAX_TOTAL_MARK='1500', OCO_LIMIT_MULTIPLIER='.4',
                         FUTURES_TP_MULTIPLIER='.2').items():
            self.config(StraddleConfig, k, v)
        self.price = 76310
        self.quotes = [dict(symbol='BTC-260918-76250-C', markPrice='435.809'),
                       dict(symbol='BTC-260918-76250-P', markPrice='348.808')]
        self.enter_straddle()
        short = self.db.query(StraddleTradeOrder).filter_by(leg_label='SHORT_LIMIT').one()
        placed = short.created_at
        self.assertAlmostEqual(short.price, 76563.8468)
        crossing = datetime(2026,9,17,17,40,12,758000,tzinfo=he.ist)
        rows = [dict(a=1,T=ft.millis(crossing)-1,p='76563.80',q='1'),
                dict(a=2,T=ft.millis(crossing),p='76563.90',q='1'),
                dict(a=3,T=ft.millis(crossing)+1,p='76550',q='1')]
        async def history(symbol, **kw):
            return [r for r in rows if (kw.get('from_id') is None or r['a'] >= kw['from_id'])]
        Clock.minute_now, Clock.elapsed_seconds = 41, 10
        with patch.object(ft,'get_futures_aggregate_trades',history):
            self.run_straddle(se.StraddleEngine())
        self.db.refresh(short)
        self.assertEqual(short.created_at, placed)
        self.assertEqual(short.filled_at, crossing.replace(tzinfo=None))
        self.assertEqual(short.processed_at, datetime(2026,9,17,17,41,22))
        self.assertEqual(short.aggregate_trade_id, 2)
        self.assertEqual(short.status, 'FILLED')
        self.assertEqual(self.db.query(StraddleFill).filter_by(order_id=short.id).one().created_at, short.filled_at)

    def test_entry_and_target_in_same_batch_follow_id_order_in_same_millisecond(self):
        bot = self.enter_straddle()
        tracking = self.db.query(StraddleSessionEvent).filter_by(event_type='FUTURES_ENTRY_TRACKING').one()
        active = json.loads(tracking.payload_json)['active_ms']
        rows = [dict(a=1,T=active+1000,p='60300',q='1'),
                dict(a=2,T=active+1000,p='59700',q='1')]
        async def history(symbol, **kw):
            return [r for r in rows if kw.get('from_id') is None or r['a'] >= kw['from_id']]
        with patch.object(ft,'get_futures_aggregate_trades',history):
            self.run_straddle(bot)
        short=self.db.query(StraddleTradeOrder).filter_by(leg_label='SHORT_LIMIT').one()
        long=self.db.query(StraddleTradeOrder).filter_by(leg_label='LONG_LIMIT').one()
        target=self.db.query(StraddleTradeOrder).filter_by(leg_label='FUTURES_CLOSE').one()
        self.assertEqual((short.status,long.status,target.status),('FILLED','CANCELLED','FILLED'))
        self.assertEqual((short.aggregate_trade_id,target.aggregate_trade_id),(1,2))
        self.assertEqual(short.filled_at,target.filled_at)
        self.assertEqual(target.created_at,short.filled_at)
        self.assertEqual(self.db.query(StraddleSession).one().pnl_realized,400)
        self.assertEqual(self.db.query(StraddleFill).count(),2)
        self.run_straddle(se.StraddleEngine())
        self.assertEqual(self.db.query(StraddleFill).count(),2)

    def test_oco_chooses_first_trade_not_sell_side_priority(self):
        bot=self.enter_straddle()
        event=self.db.query(StraddleSessionEvent).filter_by(event_type='FUTURES_ENTRY_TRACKING').one()
        active=json.loads(event.payload_json)['active_ms']
        rows=[dict(a=1,T=active+1000,p='59700',q='1'),dict(a=2,T=active+2000,p='60300',q='1')]
        async def history(symbol,**kw):
            return [r for r in rows if kw.get('from_id') is None or r['a'] >= kw['from_id']]
        with patch.object(ft,'get_futures_aggregate_trades',history): self.run_straddle(bot)
        self.assertEqual(self.db.query(StraddleTradeOrder).filter_by(leg_label='LONG_LIMIT').one().status,'FILLED')
        self.assertEqual(self.db.query(StraddleTradeOrder).filter_by(leg_label='SHORT_LIMIT').one().status,'CANCELLED')

    def test_cutoff_waits_for_contiguous_later_trade_and_recovers_late_entry(self):
        bot=self.enter_straddle()
        Clock.hour_now=11
        with patch.object(ft,'get_futures_aggregate_trades',AsyncMock(return_value=[])):
            self.run_straddle(bot)
        self.assertEqual(bot.state,'LIMITS_PLACED')
        crossing=datetime(2026,9,8,10,59,59,999000,tzinfo=he.ist)
        rows=[dict(a=1,T=ft.millis(crossing),p='60300',q='1'),
              dict(a=2,T=ft.millis(crossing)+1,p='60300',q='1')]
        async def history(symbol,**kw):
            return [r for r in rows if kw.get('from_id') is None or r['a'] >= kw['from_id']]
        with patch.object(ft,'get_futures_aggregate_trades',history): self.run_straddle(se.StraddleEngine())
        order=self.db.query(StraddleTradeOrder).filter_by(leg_label='SHORT_LIMIT').one()
        self.assertEqual(order.status,'FILLED')
        self.assertEqual(order.filled_at,crossing.replace(tzinfo=None))

    def test_entry_cutoff_excludes_trade_exactly_at_deadline(self):
        bot=self.enter_straddle()
        Clock.hour_now=11
        trade=dict(a=1,T=ft.millis(datetime(2026,9,8,11,tzinfo=he.ist)),p='60300',q='1')
        with patch.object(ft,'get_futures_aggregate_trades',AsyncMock(return_value=[trade])):
            self.run_straddle(bot)
        self.assertEqual(self.db.query(StraddleTradeOrder).filter_by(leg_label='SHORT_LIMIT').one().status,'EXPIRED')
        self.assertEqual(self.db.query(StraddleFill).count(),0)

    def test_hedge_target_recovers_crossing_when_latest_mark_never_hits(self):
        self.price = 76471.88
        self.quotes = [dict(symbol='BTC-260908-76500-P', markPrice='81.31')]
        cfg = self.db.query(HedgeStrategyConfig).filter_by(strategy_name='1st Trader').one()
        cfg.contract_qty = 10
        self.db.commit()
        sid = self.hedge_entry(he.HedgeEngine())
        order, event = ft.target_for_session(self.db, HedgeTradeOrder, HedgeSessionEvent, sid)
        active = json.loads(event.payload_json)['active_ms']
        self.assertAlmostEqual(order.price, 76553.19)
        # Price crosses and returns; the current mark remains below the target.
        trades = [dict(a=1, T=active+1000, p='76500', q='1'),
                  dict(a=2, T=active+2000, p='76560', q='1'),
                  dict(a=3, T=active+3000, p='76510', q='1')]
        with patch.object(ft, 'get_futures_aggregate_trades', AsyncMock(return_value=trades)):
            asyncio.run(he.HedgeEngine().tick(self.db))
        self.assertEqual((order.order_type, order.status), ('LIMIT', 'FILLED'))
        self.assertAlmostEqual(self.db.get(HedgeSession, sid).bull_exit, 76553.19)
        self.assertEqual(self.db.get(HedgeSession, sid).realized_pnl, 813.10)
        self.assertEqual(float(self.db.get(HedgeConfig, 'PAPER_WALLET_USDT').value), 100813.10)
        fill = self.db.query(HedgeFill).one()
        self.assertAlmostEqual(fill.fill_price, order.price)
        with patch.object(ft, 'get_futures_aggregate_trades', AsyncMock()) as fetch:
            asyncio.run(he.HedgeEngine().tick(self.db))
        fetch.assert_not_awaited()
        self.assertEqual(self.db.query(HedgeFill).count(), 1)
        self.assertEqual(self.db.query(HedgePaperLedgerEntry).count(), 1)

    def test_hedge_mark_crossing_without_trade_crossing_does_not_fill(self):
        sid = self.hedge_entry(he.HedgeEngine())
        order, event = ft.target_for_session(self.db, HedgeTradeOrder, HedgeSessionEvent, sid)
        active = json.loads(event.payload_json)['active_ms']
        self.price = 60500
        with patch.object(ft, 'get_futures_aggregate_trades', AsyncMock(return_value=[dict(a=1, T=active+1000, p='60050', q='1')])):
            asyncio.run(he.HedgeEngine().tick(self.db))
        self.assertEqual(order.status, 'PENDING')
        self.assertEqual(self.db.query(HedgeOpenPosition).count(), 2)
        self.assertEqual(self.db.query(HedgePaperLedgerEntry).count(), 0)

    def test_hedge_history_failure_then_recovery_does_not_double_credit(self):
        sid = self.hedge_entry(he.HedgeEngine())
        order, event = ft.target_for_session(self.db, HedgeTradeOrder, HedgeSessionEvent, sid)
        before = event.payload_json
        self.price = 60500
        with patch.object(ft, 'get_futures_aggregate_trades', AsyncMock(side_effect=ValueError('DATA_GAP'))):
            with self.assertLogs(he.logger, level='WARNING'):
                asyncio.run(he.HedgeEngine().tick(self.db))
        self.assertEqual(order.status, 'PENDING')
        self.assertEqual(event.payload_json, before)
        self.assertEqual(self.db.query(HedgePaperLedgerEntry).count(), 0)
        asyncio.run(he.HedgeEngine().tick(self.db))
        self.assertEqual(order.status, 'FILLED')
        self.assertEqual(self.db.get(HedgeSession, sid).realized_pnl, 100)

    def test_straddle_recovers_crossing_and_credits_limit_not_mark(self):
        bot = self.enter_straddle()
        self.price = 60300
        self.run_straddle(bot)
        sid = bot.active_session_id
        order, event = ft.target_for_session(self.db, StraddleTradeOrder, StraddleSessionEvent, sid)
        active = json.loads(event.payload_json)['active_ms']
        next_id = json.loads(event.payload_json)['last_id'] + 1
        trades = [dict(a=next_id, T=active+1000, p='60300', q='1'),
                  dict(a=next_id+1, T=active+2000, p='59700', q='1'),
                  dict(a=next_id+2, T=active+3000, p='60300', q='1')]
        with patch.object(ft, 'get_futures_aggregate_trades', AsyncMock(return_value=trades)):
            self.run_straddle(se.StraddleEngine())
        self.db.refresh(order)
        sess = self.db.get(StraddleSession, sid)
        self.assertEqual((order.status, order.order_type, order.price), ('FILLED', 'LIMIT', 59800))
        self.assertEqual(sess.futures_exit_price, 59800)
        self.assertEqual(sess.pnl_realized, 400)
        self.assertEqual(float(self.db.get(StraddleConfig, 'PAPER_WALLET_USDT').value), 100400)
        self.assertEqual(self.db.query(StraddleFill).filter_by(order_id=order.id).one().fill_price, 59800)
        self.run_straddle(se.StraddleEngine())
        self.assertEqual(self.db.query(StraddleFill).count(), 2)
        self.assertEqual(self.db.query(StraddleTradeOrder).filter_by(leg_label='FUTURES_CLOSE').count(), 1)

    def test_straddle_long_target_and_manual_cancel(self):
        bot = self.enter_straddle()
        self.price = 59700
        self.run_straddle(bot)
        sid = bot.active_session_id
        order, event = ft.target_for_session(self.db, StraddleTradeOrder, StraddleSessionEvent, sid)
        self.assertEqual((order.price, order.side), (60200, 'SELL'))
        bot.state = 'SQUAREOFF'
        self.run_straddle(bot)
        self.db.refresh(order)
        self.assertEqual(order.status, 'CANCELLED')
        self.assertEqual(self.db.query(StraddleFill).filter_by(order_id=order.id).count(), 0)

    def test_straddle_long_target_exact_touch(self):
        bot = self.enter_straddle()
        self.price = 59700
        self.run_straddle(bot)
        sid = bot.active_session_id
        self.price = 60200
        self.run_straddle(bot)
        self.assertEqual(self.db.get(StraddleSession, sid).futures_exit_price, 60200)
        self.assertEqual(self.db.get(StraddleSession, sid).pnl_realized, 400)

    def test_straddle_no_futures_no_fictitious_profit_and_repeat_safe(self):
        bot = self.enter_straddle()
        sid = bot.active_session_id
        bot.state = 'SQUAREOFF'
        self.run_straddle(bot)
        self.assertEqual(self.db.get(StraddleSession, sid).pnl_realized, 0)
        self.assertEqual(self.db.query(StraddleTradeOrder).filter_by(leg_label='FUTURES_CLOSE').count(), 0)
        self.assertEqual(float(self.db.get(StraddleConfig, 'PAPER_WALLET_USDT').value), 100000)
        count = self.db.query(StraddleWalletLedger).count()
        bot.state = 'SQUAREOFF'
        self.run_straddle(bot)
        self.assertEqual(self.db.query(StraddleWalletLedger).count(), count)

    def test_straddle_close_uses_entry_qty_and_zero_option_mark(self):
        bot = self.enter_straddle()
        self.config(StraddleConfig, 'TRADE_QTY', 10)
        self.quotes = [dict(symbol=q['symbol'], markPrice='0') for q in self.quotes]
        bot.state = 'SQUAREOFF'
        self.run_straddle(bot)
        closes = self.db.query(StraddleTradeOrder).filter_by(side='SELL', asset_type='OPTION').all()
        self.assertEqual([(o.qty, o.price) for o in closes], [(1, 0), (1, 0)])
        self.assertEqual(self.db.query(StraddleSession).one().pnl_realized, -200)
        self.assertEqual(float(self.db.get(StraddleConfig, 'PAPER_WALLET_USDT').value), 99800)

    def test_straddle_restart_restores_orders_and_exit(self):
        self.enter_straddle()
        bot = se.StraddleEngine()
        self.price = 60300
        self.run_straddle(bot)
        self.assertEqual(bot.state, 'IN_TRADE')
        bot = se.StraddleEngine()
        Clock.hour_now = 13
        self.run_straddle(bot)
        self.assertEqual(self.db.query(StraddleSession).one().status, 'Completed')
        self.assertEqual(self.db.query(StraddleSession).count(), 1)

    def test_straddle_cutoff_prevents_late_futures_entry(self):
        bot = self.enter_straddle()
        Clock.hour_now = 11
        self.price = 60300
        self.run_straddle(bot)
        orders = self.db.query(StraddleTradeOrder).filter_by(asset_type='FUTURES').all()
        self.assertEqual([o.status for o in orders], ['EXPIRED', 'EXPIRED'])

    def test_straddle_tp_and_ledger_tally(self):
        bot = self.enter_straddle()
        self.price = 60300
        self.run_straddle(bot)
        sid = bot.active_session_id
        self.price = 59700
        self.run_straddle(bot)
        sess = self.db.get(StraddleSession, sid)
        self.assertEqual(sess.status, 'Completed')
        self.assertEqual(sess.pnl_realized, 400)
        self.assertEqual(sess.futures_exit_price, 59800)
        ledger_delta = sum(l.amount for l in self.db.query(StraddleWalletLedger).all())
        self.assertEqual(ledger_delta, 400)
        self.assertEqual(float(self.db.get(StraddleConfig, 'PAPER_WALLET_USDT').value), 100400)

    def test_straddle_manual_squareoff_during_entry_window(self):
        bot = self.enter_straddle()
        sess = self.db.get(StraddleSession, bot.active_session_id)
        sess.status = 'MANUAL_SQUAREOFF'
        self.db.commit()
        fresh = se.StraddleEngine()
        self.run_straddle(fresh)
        self.assertEqual(sess.status, 'Completed')
        self.assertEqual(self.db.query(StraddleTradeOrder).filter_by(side='SELL', asset_type='OPTION').count(), 2)

    def test_straddle_disable_reenable(self):
        bot = se.StraddleEngine()
        self.config(StraddleConfig, 'BOT_ENABLED', 0)
        self.run_straddle(bot)
        self.assertEqual(bot.state, 'DISABLED')
        self.config(StraddleConfig, 'BOT_ENABLED', 1)
        self.run_straddle(bot)
        self.assertEqual(bot.state, 'LIMITS_PLACED')

    def test_disabled_straddle_refreshes_preview_without_trading(self):
        self.config(StraddleConfig, 'BOT_ENABLED', 0)
        bot = se.StraddleEngine()
        self.run_straddle(bot)
        self.price = 60050
        self.quotes = [dict(symbol=q['symbol'], markPrice='200') for q in self.quotes]
        self.run_straddle(bot)
        snapshot = bot.get_live_monitoring_snapshot(self.db)
        self.assertEqual(snapshot['state'], 'DISABLED')
        self.assertEqual(snapshot['last_spot_price'], 60050)
        self.assertEqual(snapshot['combined_premium'], 400)
        self.assertTrue(snapshot['preview_data_available'])
        self.assertEqual(self.db.query(StraddleSession).count(), 0)
        self.assertEqual(self.db.query(StraddleTradeOrder).count(), 0)
        self.assertEqual(self.db.get(StraddleConfig, 'PAPER_WALLET_USDT').value, '100000')
        self.quotes = []
        with self.assertLogs(se.logger, level='ERROR'):
            self.run_straddle(bot)
        snapshot = bot.get_live_monitoring_snapshot(self.db)
        self.assertFalse(snapshot['preview_data_available'])
        self.assertFalse(snapshot['cond_premium_valid'])
        self.assertFalse(snapshot['cond_premium_gap_valid'])
        self.assertEqual(snapshot['current_call_mark'], 0)

    def test_hedge_preview_refreshes_under_every_entry_block(self):
        for control, value, state in [('BOT_ENABLED', 0, 'DISABLED'),
                                       ('ENGINE_ENABLED', 0, 'DISABLED'),
                                       ('GLOBAL_PAUSE', 1, 'PAUSED'),
                                       ('SKIP_WEEKENDS', 1, 'SKIP_WEEKEND')]:
            with self.subTest(control=control):
                for key, default in [('BOT_ENABLED', 1), ('ENGINE_ENABLED', 1),
                                     ('GLOBAL_PAUSE', 0), ('SKIP_WEEKENDS', 0)]:
                    self.config(HedgeConfig, key, default)
                self.config(HedgeConfig, control, value)
                for cfg in self.db.query(HedgeStrategyConfig).all():
                    cfg.max_premium, cfg.max_time_value = 50, 10
                self.db.commit()
                self.quotes = [dict(symbol='BTC-260908-60000-P', markPrice='100')]
                bot = he.HedgeEngine()
                with patch.object(he, 'is_weekend_session', return_value=True):
                    asyncio.run(bot.tick(self.db))
                    self.assertIn('premium <= 50 and TV <= 10', bot.preview_slot1_put_reason)
                    for cfg in self.db.query(HedgeStrategyConfig).all():
                        cfg.max_premium, cfg.max_time_value = 900, 700
                    self.db.commit()
                    self.quotes[0]['markPrice'] = '200'
                    asyncio.run(bot.tick(self.db))
                    snap = bot.get_live_monitoring_snapshot(self.db)
                self.assertEqual(bot.state, state)
                for slot in ('slot1', 'slot2'):
                    self.assertEqual(snap[slot]['bullish']['option_mark'], 200)
                    self.assertEqual(snap[slot]['bullish']['selection_reason'], '')
                    self.assertTrue(snap[slot]['bullish']['rule_b_valid'])
                if state == 'DISABLED':
                    self.assertIn('Trading disabled', snap['slot1']['idle_reason'])
                if state == 'PAUSED':
                    self.assertIn('paused', snap['slot1']['idle_reason'])
                self.assertEqual(self.db.query(HedgeSession).count(), 0)
                self.assertEqual(self.db.query(HedgeTradeOrder).count(), 0)
                self.assertEqual(self.db.get(HedgeConfig, 'PAPER_WALLET_USDT').value, '100000')

    def test_hedge_failed_refresh_removes_old_preview_reason(self):
        self.config(HedgeConfig, 'BOT_ENABLED', 0)
        self.db.query(HedgeStrategyConfig).update({'max_premium': 50})
        self.db.commit()
        bot = he.HedgeEngine()
        asyncio.run(bot.tick(self.db))
        self.assertIn('premium <= 50', bot.preview_slot1_put_reason)
        self.db.query(HedgeStrategyConfig).update({'max_premium': 900})
        self.db.commit()
        with patch.object(he, 'get_btc_spot_price', AsyncMock(side_effect=ValueError('offline'))):
            with self.assertRaises(ValueError):
                asyncio.run(bot.tick(self.db))
        snap = bot.get_live_monitoring_snapshot(self.db)['slot1']['bullish']
        self.assertEqual(snap['option_mark'], 0)
        self.assertIn('Market data unavailable', snap['selection_reason'])
        self.assertNotIn('50', snap['selection_reason'])
        self.assertEqual(self.db.query(HedgeSession).count(), 0)

    def test_straddle_missing_pair_and_wrong_expiry_are_rejected(self):
        bot = se.StraddleEngine()
        self.quotes = [dict(symbol='BTC-260909-60000-C', markPrice='100')]
        with self.assertRaises(ValueError):
            asyncio.run(bot.find_same_strike_pair(60000))
        with self.assertRaises(ValueError):
            asyncio.run(bot.get_specific_strike_marks(60000, 60000, '260908'))

    def test_hedge_independent_entry_windows_and_restart_lock(self):
        bot = he.HedgeEngine()
        asyncio.run(bot.tick(self.db))
        self.assertIsNotNone(bot.slot1_session_id)
        self.assertIsNone(bot.slot2_session_id)
        bot = he.HedgeEngine()
        Clock.hour_now = 8
        asyncio.run(bot.tick(self.db))
        self.assertIsNotNone(bot.slot2_session_id)
        self.assertEqual(self.db.query(HedgeSession).count(), 2)
        asyncio.run(he.HedgeEngine().tick(self.db))
        self.assertEqual(self.db.query(HedgeSession).count(), 2)

    def test_hedge_first_tp_keeps_option_then_target_closes(self):
        bot = he.HedgeEngine()
        sid = self.hedge_entry(bot)
        self.price = 60150
        asyncio.run(bot.tick(self.db))
        positions = self.db.query(HedgeOpenPosition).filter_by(session_id=sid).all()
        self.assertEqual([p.symbol for p in positions], ['BTC-260908-60000-P'])
        target = self.db.query(HedgeTradeOrder).filter_by(order_type='OPTION_TARGET').one()
        self.assertEqual((target.price, target.status), (200, 'PENDING'))
        self.assertEqual(self.db.get(HedgeSession, sid).realized_pnl, 100)
        bot = he.HedgeEngine()
        self.quotes = [dict(symbol=q['symbol'], markPrice='220') for q in self.quotes]
        asyncio.run(bot.tick(self.db))
        self.assertEqual(self.db.get(HedgeSession, sid).status, 'Completed')
        self.assertEqual(self.db.get(HedgeSession, sid).realized_pnl, 220)
        self.assertEqual(self.db.query(HedgeSessionEvent).filter_by(event_type='FUTURES_TP').count(), 1)
        self.assertEqual(float(self.db.get(HedgeConfig, 'PAPER_WALLET_USDT').value), 100220)
        self.assertEqual(target.status, 'FILLED')

    def test_second_tp_reentry_survives_restart_and_is_cancelled_or_filled(self):
        self.db.query(HedgeStrategyConfig).filter_by(strategy_name='1st Trader').one().direction = 'Bearish'
        self.db.commit()
        bot = he.HedgeEngine()
        self.hedge_entry(bot)
        sid2 = self.hedge_entry(bot, '2nd Trader')
        self.price = 59850
        asyncio.run(bot.tick(self.db))
        self.price = 60150
        asyncio.run(bot.tick(self.db))
        reentry = self.db.query(HedgeTradeOrder).filter_by(order_type='REENTRY_LIMIT').one()
        self.assertEqual((reentry.price, reentry.status), (59900, 'PENDING'))
        self.assertEqual(bot.tp_rank_1_slot, '1st Trader')
        self.assertEqual(bot.tp_rank_2_slot, '2nd Trader')
        self.price = 59850
        asyncio.run(he.HedgeEngine().tick(self.db))
        self.assertEqual(reentry.status, 'FILLED')
        fut = self.db.query(HedgeOpenPosition).filter_by(session_id=sid2, symbol='BTC-USDT-FUTURES').one()
        self.assertEqual(fut.entry_price, 59850)
        self.price = 60150
        asyncio.run(he.HedgeEngine().tick(self.db))
        self.assertEqual(self.db.query(HedgeSessionEvent).filter_by(event_type='FUTURES_TP').count(), 2)

    def test_hedge_squareoff_repeat_and_role_attribution(self):
        bot = he.HedgeEngine()
        sid = self.hedge_entry(bot, '2nd Trader')
        self.price = 61000
        asyncio.run(bot.execute_squareoff(self.db, 'Manual Emergency Squareoff'))
        self.assertEqual(self.db.get(HedgeSession, sid).realized_pnl, 1000)
        count = self.db.query(HedgePaperLedgerEntry).count()
        asyncio.run(bot.execute_squareoff(self.db, 'Manual Emergency Squareoff'))
        self.assertEqual(self.db.get(HedgeSession, sid).realized_pnl, 1000)
        self.assertEqual(self.db.query(HedgePaperLedgerEntry).count(), count)
        self.assertTrue(all(o.trader_leg == '2nd Trader' for o in self.db.query(HedgeTradeOrder).all()))

    def test_hedge_scheduled_close_cancels_unfilled_targets_and_reentry(self):
        bot = he.HedgeEngine()
        self.hedge_entry(bot)
        self.hedge_entry(bot, '2nd Trader')
        self.price = 60150
        asyncio.run(bot.tick(self.db))
        self.price = 59850
        asyncio.run(bot.tick(self.db))
        asyncio.run(bot.execute_squareoff(self.db))
        pending = self.db.query(HedgeTradeOrder).filter(
            HedgeTradeOrder.order_type.in_(['OPTION_TARGET', 'REENTRY_LIMIT'])).all()
        self.assertEqual([o.status for o in pending], ['CANCELLED', 'CANCELLED'])
        self.assertEqual(self.db.query(HedgeOpenPosition).count(), 0)
        self.assertEqual(sum(s.realized_pnl for s in self.db.query(HedgeSession).all()), 200)
        self.assertEqual(sum(l.amount for l in self.db.query(HedgePaperLedgerEntry).all()), 200)
        self.assertEqual(float(self.db.get(HedgeConfig, 'PAPER_WALLET_USDT').value), 100200)

    def test_hedge_bearish_tp_and_sell_reentry(self):
        for config in self.db.query(HedgeStrategyConfig).all():
            config.direction = 'Bullish'
        self.db.commit()
        bot = he.HedgeEngine()
        self.hedge_entry(bot)
        sid = self.hedge_entry(bot, '2nd Trader')
        self.price = 60150
        asyncio.run(bot.tick(self.db))
        self.price = 59850
        asyncio.run(bot.tick(self.db))
        reentry = self.db.query(HedgeTradeOrder).filter_by(order_type='REENTRY_LIMIT').one()
        self.assertEqual((reentry.side, reentry.price), ('SELL', 60100))
        self.price = 60150
        asyncio.run(he.HedgeEngine().tick(self.db))
        fut = self.db.query(HedgeOpenPosition).filter_by(session_id=sid, symbol='BTC-USDT-FUTURES').one()
        self.assertEqual((fut.side, fut.entry_price), ('SHORT', 60150))

    def test_queued_hedge_manual_close_survives_restart_and_disable(self):
        self.hedge_entry(he.HedgeEngine())
        self.config(HedgeConfig, 'ENGINE_ENABLED', 0)
        command = HedgeRuntimeCommand(command='SQUAREOFF', status='QUEUED')
        self.db.add(command)
        self.db.commit()
        asyncio.run(he.HedgeEngine().tick(self.db))
        self.assertEqual(command.status, 'COMPLETED')
        self.assertEqual(self.db.query(HedgeOpenPosition).count(), 0)

    def test_hedge_pause_blocks_entry_but_allows_management(self):
        bot = he.HedgeEngine()
        self.config(HedgeConfig, 'GLOBAL_PAUSE', 1)
        asyncio.run(bot.tick(self.db))
        self.assertEqual(self.db.query(HedgeSession).count(), 0)
        self.config(HedgeConfig, 'GLOBAL_PAUSE', 0)
        asyncio.run(bot.tick(self.db))
        self.config(HedgeConfig, 'ENGINE_ENABLED', 0)
        self.price = 60150
        asyncio.run(bot.tick(self.db))
        self.assertEqual(self.db.query(HedgeSessionEvent).filter_by(event_type='FUTURES_TP').count(), 1)

    def test_preview_distinguishes_filtered_quotes_from_qualifying_contract(self):
        config = self.db.query(HedgeStrategyConfig).filter_by(strategy_name='1st Trader').one()
        config.max_premium = 50
        self.db.commit()
        bot = he.HedgeEngine()
        asyncio.run(bot.tick(self.db))
        snapshot = bot.get_live_monitoring_snapshot(self.db)['slot1']
        self.assertIn('Quotes available', snapshot['bearish']['selection_reason'])
        config.max_premium = 250
        self.db.commit()
        self.config(HedgeConfig, 'MAX_OPTION_SPEND', 50)
        asyncio.run(bot.tick(self.db))
        snapshot = bot.get_live_monitoring_snapshot(self.db)['slot1']
        self.assertIsNotNone(bot.slot1_session_id)
        self.assertIsNone(snapshot['idle_reason'])

    def test_legacy_global_spend_cap_does_not_block_ten_btc_entry(self):
        self.config(HedgeConfig, 'MAX_OPTION_SPEND', 450)
        cfg = self.db.query(HedgeStrategyConfig).filter_by(strategy_name='1st Trader').one()
        cfg.contract_qty = 10
        cfg.max_premium = 600
        cfg.max_time_value = 300
        self.db.commit()
        self.price = 79400
        self.quotes = [dict(symbol='BTC-260908-79750-P', markPrice='580.59')]
        bot = he.HedgeEngine()
        asyncio.run(bot.tick(self.db))
        self.assertIsNotNone(bot.slot1_session_id)
        option = self.db.query(HedgeOpenPosition).filter_by(symbol='BTC-260908-79750-P').one()
        self.assertEqual(option.qty, 10)
        snapshot = bot.get_live_monitoring_snapshot(self.db)
        self.assertEqual(snapshot['slot1']['bullish']['estimated_option_cost'], 5805.9)
        self.assertNotIn('cond_max_spend_valid', snapshot)

    def test_trader2_is_opposite_even_with_same_configured_direction(self):
        for first_direction, expected in [('Bullish', 'Bearish'), ('Bearish', 'Bullish')]:
            with self.subTest(first_direction=first_direction):
                # Each subcase has independent trading records.
                for model in (HedgeSessionEvent, HedgeOpenPosition, HedgeTradeOrder, HedgeSession):
                    self.db.query(model).delete()
                self.db.query(HedgeStrategyConfig).filter_by(strategy_name='1st Trader').one().direction = first_direction
                self.db.query(HedgeStrategyConfig).filter_by(strategy_name='2nd Trader').one().direction = first_direction
                self.db.commit()
                bot = he.HedgeEngine()
                self.hedge_entry(bot)
                self.assertIsNotNone(self.hedge_entry(bot, '2nd Trader'))
                self.assertEqual(bot.slot2_direction, expected)

    def test_trader2_waits_when_only_same_direction_qualifies(self):
        self.db.query(HedgeStrategyConfig).filter_by(strategy_name='1st Trader').one().direction = 'Bearish'
        self.db.commit()
        self.quotes = [dict(symbol='BTC-260908-60000-C', markPrice='100')]
        bot = he.HedgeEngine()
        self.hedge_entry(bot)
        self.assertIsNone(self.hedge_entry(bot, '2nd Trader'))
        Clock.hour_now = 8
        asyncio.run(bot.tick(self.db))
        self.assertIn('Bullish', bot.get_live_monitoring_snapshot(self.db)['slot2']['idle_reason'])
        self.assertEqual(self.db.query(HedgeSession).count(), 1)

    def test_opposite_direction_survives_restart_and_first_close(self):
        self.db.query(HedgeStrategyConfig).filter_by(strategy_name='1st Trader').one().direction = 'Bearish'
        self.db.commit()
        bot = he.HedgeEngine()
        self.hedge_entry(bot)
        asyncio.run(bot.execute_squareoff(self.db))
        fresh = he.HedgeEngine()
        self.assertIsNotNone(self.hedge_entry(fresh, '2nd Trader'))
        self.assertEqual(fresh.slot2_direction, 'Bullish')

    def test_opposite_direction_allows_lower_second_strike(self):
        first = self.db.query(HedgeStrategyConfig).filter_by(strategy_name='1st Trader').one()
        first.max_premium = 600
        self.db.commit()
        self.quotes = [dict(symbol='BTC-260908-60500-P', markPrice='600'),
                       dict(symbol='BTC-260908-60000-C', markPrice='100')]
        bot = he.HedgeEngine()
        self.assertIsNotNone(self.hedge_entry(bot))
        self.assertIsNotNone(self.hedge_entry(bot, '2nd Trader'))
        self.assertEqual(bot.slot2_direction, 'Bearish')
        self.assertLess(bot.slot2_strike, bot.slot1_strike)
        self.assertNotIn('cond_rule_c_valid', bot.get_live_monitoring_snapshot(self.db))

    def selection(self, entries, direction='Auto', premium=600, tv=300, now=None):
        quotes = [dict(symbol=f'BTC-{expiry}-{strike}-{side}', markPrice=str(mark))
                  for expiry, strike, side, mark in entries]
        return he.HedgeEngine().select_itm_option(
            quotes, 79400, direction, premium, tv, now or Clock.now(timezone.utc))

    def test_selection_skips_nearest_failing_call(self):
        result = self.selection([('260908', 79250, 'C', 550), ('260908', 79000, 'C', 600)], 'Bearish')
        self.assertEqual(result[0], 79000)

    def test_selection_lowest_tv_even_when_nearer_also_passes(self):
        result = self.selection([('260908', 79250, 'C', 400), ('260908', 79000, 'C', 600)], 'Bearish')
        self.assertEqual(result[0], 79000)

    def test_selection_puts_and_irregular_strike_spacing(self):
        result = self.selection([('260908', 79500, 'P', 450), ('260908', 79750, 'P', 550),
                                 ('260908', 80000, 'P', 750)], 'Bullish')
        self.assertEqual(result[0], 79750)

    def test_selection_auto_compares_calls_and_puts(self):
        result = self.selection([('260908', 79500, 'P', 350), ('260908', 79000, 'C', 600)])
        self.assertTrue(result[3].endswith('-C'))

    def test_selection_limits_are_inclusive_and_no_later_expiry_fallback(self):
        self.assertEqual(self.selection([('260908', 79100, 'C', 600)])[0], 79100)
        with self.assertRaises(ValueError):
            self.selection([('260908', 79250, 'C', 550), ('260909', 79000, 'C', 600)])

    def test_selection_expiry_boundary_is_strictly_less_than_24h(self):
        quotes = [('260908', 79000, 'C', 600), ('260909', 79000, 'C', 600)]
        at_boundary = datetime(2026, 9, 8, 13, 30, tzinfo=he.ist)
        with self.assertRaises(ValueError):
            self.selection(quotes, now=at_boundary)
        after = datetime(2026, 9, 8, 14, 0, tzinfo=he.ist)
        self.assertEqual(self.selection(quotes, now=after)[2], '260909')
        before = datetime(2026, 9, 8, 13, 29, tzinfo=he.ist)
        self.assertEqual(self.selection(quotes, now=before)[2], '260908')

    def test_selection_execution_uses_spot_without_strike_gate(self):
        self.config(HedgeConfig, 'MAX_OPTION_SPEND', 1000)
        cfg = self.db.query(HedgeStrategyConfig).filter_by(strategy_name='2nd Trader').one()
        cfg.direction, cfg.max_premium, cfg.max_time_value = 'Auto', 600, 300
        self.db.commit()
        bot = he.HedgeEngine()
        quotes = [dict(symbol='BTC-260908-79000-C', markPrice='600'),
                  dict(symbol='BTC-260908-79500-P', markPrice='350')]
        bot.slot1_session_id, bot.slot1_strike = 123, 79250
        # Lowest-TV selection is not restricted by the other slot's strike.
        sid = asyncio.run(bot.execute_slot_entry(self.db, '2nd Trader', cfg, 78900, 79400, quotes=quotes))
        self.assertIsNotNone(sid)
        self.assertEqual(self.db.get(HedgeSession, sid).bear_entry, 78900)
        self.assertEqual(bot.slot2_strike, 79000)

    def test_tick_uses_one_options_snapshot_for_preview_and_entry(self):
        quotes = AsyncMock(return_value=self.quotes)
        with patch.object(he, 'get_btc_options_mark_prices', quotes):
            bot = he.HedgeEngine()
            asyncio.run(bot.tick(self.db))
        self.assertEqual(quotes.await_count, 1)
        self.assertEqual(bot.preview_slot1_put_strike, bot.slot1_strike)

    def test_hedge_independent_close_after_expiry_boundary(self):
        bot = he.HedgeEngine()
        sid1 = self.hedge_entry(bot)
        sid2 = self.hedge_entry(bot, '2nd Trader')
        Clock.hour_now = 13
        asyncio.run(bot.tick(self.db))
        self.assertEqual(self.db.get(HedgeSession, sid1).status, 'Completed')
        self.assertEqual(self.db.get(HedgeSession, sid2).status, 'Open')
        Clock.hour_now = 17
        asyncio.run(he.HedgeEngine().tick(self.db))
        self.assertEqual(self.db.get(HedgeSession, sid2).status, 'Completed')

    def test_hedge_missing_quote_does_not_partially_close(self):
        bot = he.HedgeEngine()
        sid = self.hedge_entry(bot)
        self.quotes = []
        with self.assertRaises(ValueError):
            asyncio.run(bot.execute_squareoff(self.db))
        self.assertEqual(self.db.get(HedgeSession, sid).status, 'Open')
        self.assertEqual(self.db.query(HedgeOpenPosition).count(), 2)
        self.assertEqual(self.db.query(HedgePaperLedgerEntry).count(), 0)

    def test_hedge_snapshot_uses_held_contract_and_qty(self):
        bot = he.HedgeEngine()
        self.hedge_entry(bot)
        config = self.db.query(HedgeStrategyConfig).filter_by(strategy_name='1st Trader').one()
        config.contract_qty = 10
        self.db.commit()
        bot.last_futures_mark = 60050
        bot.preview_slot1_put_mark = 500
        bot.option_quotes = [dict(symbol='BTC-260908-60000-P', markPrice='120')]
        snap = bot.get_live_monitoring_snapshot(self.db)['slot1']
        self.assertEqual(snap['qty'], 1)
        self.assertEqual(snap['active_trade']['pnl_usdt'], 70)
        self.assertEqual(snap['filled_fut_entry'], 60000)
        self.assertEqual(snap['filled_fut_tp'], 60100)

    def test_stale_cache_and_empty_rate_limit_are_rejected(self):
        with patch.dict(bc._price_cache, {'BTCUSDT': (60000, 1)}, clear=True):
            with self.assertRaises(ValueError):
                bc.cached_quote('BTCUSDT')
        with patch.dict(bc._price_cache, {}, clear=True), patch.dict(
                bc._rate_limit_until, {'BTC_OPTIONS_MARK': time.time() + 90}, clear=True):
            with self.assertRaises(ValueError):
                asyncio.run(bc.get_btc_options_mark_prices())

    def test_dashboard_serializes_option_only_phase_and_missing_quote(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.database import get_db
        import app.api.dashboard_routes as routes
        bot = he.HedgeEngine()
        self.hedge_entry(bot)
        self.price = 60150
        asyncio.run(bot.tick(self.db))
        app = FastAPI()
        app.include_router(routes.router)
        def database():
            yield self.db
        app.dependency_overrides[get_db] = database
        with patch.object(routes, 'hedge_engine', bot), patch.object(
                routes, 'straddle_engine', se.StraddleEngine()), patch.object(
                routes, 'get_btc_futures_mark_price', AsyncMock(return_value=self.price)), patch.object(
                routes, 'get_btc_spot_price', AsyncMock(return_value=self.price)), patch.object(
                routes, 'get_btc_options_mark_prices', AsyncMock(return_value=[])):
            response = TestClient(app).get('/api/v1/dashboard/snapshot')
        self.assertEqual(response.status_code, 200)
        data = response.json()['hedge']
        self.assertIsNone(data['positions'][0]['current_price'])
        self.assertIsNone(data['live_monitoring']['slot1']['active_trade']['pnl_usdt'])


if __name__ == '__main__':
    unittest.main()
