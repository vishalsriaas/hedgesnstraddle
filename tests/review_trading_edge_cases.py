"""Safety expectations found during review; currently fail against the engines.

Run explicitly (excluded from default test*.py discovery):
  .venv/Scripts/python.exe -m unittest discover -s tests -p review_trading_edge_cases.py -v

Uses only the existing in-memory fixture and mocked prices. No live trading/database.
"""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_trading_logic as fixture


class TradingReviewTests(unittest.TestCase):
    def setUp(self):
        self.t = fixture.TradingLogicTests()
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)

    def test_straddle_overdue_session_must_not_fill_after_daily_rollover(self):
        t = self.t
        bot = t.enter_straddle()
        sid = bot.active_session_id
        fixture.Clock.hour_now = 14
        t.price = 60300
        t.run_straddle(fixture.se.StraddleEngine())
        filled = t.db.query(fixture.StraddleTradeOrder).filter_by(
            session_id=sid, asset_type='FUTURES', status='FILLED').count()
        self.assertEqual(filled, 0, 'An overdue futures entry filled after cutoff and squareoff')

    def test_straddle_manual_exit_does_not_require_spot_feed(self):
        t = self.t
        bot = t.enter_straddle()
        bot.state = 'SQUAREOFF'
        with patch.object(fixture.se, 'get_btc_spot_price',
                          AsyncMock(side_effect=ValueError('spot offline'))):
            t.run_straddle(bot)
        self.assertEqual(t.db.query(fixture.StraddleSession).one().status, 'Completed')

    def test_hedge_scheduled_exit_does_not_require_spot_feed(self):
        t = self.t
        bot = fixture.he.HedgeEngine()
        sid = t.hedge_entry(bot)
        fixture.Clock.hour_now = 13
        with patch.object(fixture.he, 'get_btc_spot_price',
                          AsyncMock(side_effect=ValueError('spot offline'))):
            try:
                asyncio.run(bot.tick(t.db))
            except ValueError:
                pass
        self.assertEqual(t.db.get(fixture.HedgeSession, sid).status, 'Completed')

    def test_hedge_option_target_cannot_fill_below_its_limit(self):
        t = self.t
        bot = fixture.he.HedgeEngine()
        sid = t.hedge_entry(bot)
        t.price = 60150
        asyncio.run(bot.tick(t.db))
        # Trigger snapshot says 220, but the subsequent execution fetch says 100.
        bot.option_quotes = [dict(symbol=q['symbol'], markPrice='220') for q in t.quotes]
        asyncio.run(bot.manage_slot(t.db, t.db.get(fixture.HedgeSession, sid), '1st Trader'))
        target = t.db.query(fixture.HedgeTradeOrder).filter_by(order_type='OPTION_TARGET').one()
        self.assertTrue(target.status != 'FILLED' or target.price >= 200,
                        f'Sell limit 200 filled at {target.price}')

    def test_hedge_reentry_requires_unexpired_option_protection(self):
        t = self.t
        bot = fixture.he.HedgeEngine()
        t.hedge_entry(bot)
        t.hedge_entry(bot, '2nd Trader')
        t.price = 60150
        asyncio.run(bot.tick(t.db))
        t.price = 59850
        asyncio.run(bot.tick(t.db))
        fixture.Clock.hour_now = 14
        t.price = 60150
        t.quotes = []
        asyncio.run(bot.tick(t.db))
        order = t.db.query(fixture.HedgeTradeOrder).filter_by(order_type='REENTRY_LIMIT').one()
        self.assertNotEqual(order.status, 'FILLED', 'Futures re-entered after the option expired')

    def test_hedge_api_rejects_invalid_close_hour(self):
        from app.api.config_routes import update_hedge_strategy_rules
        t = self.t
        cfg = t.db.query(fixture.HedgeStrategyConfig).filter_by(strategy_name='1st Trader').one()
        # Direct route call exercises the same unvalidated dict handler as HTTP.
        try:
            update_hedge_strategy_rules({'id': cfg.id, 'force_close_h': 25},
                                       t.db, SimpleNamespace(email='review@example.invalid'))
        except Exception:
            t.db.rollback()
        t.db.expire_all()
        self.assertLessEqual(cfg.force_close_h, 23, 'Invalid hour persisted and breaks tick()')

    def test_straddle_rejects_option_purchase_exceeding_cash(self):
        t = self.t
        t.config(fixture.StraddleConfig, 'PAPER_WALLET_USDT', 50)
        t.run_straddle(fixture.se.StraddleEngine())
        self.assertEqual(t.db.query(fixture.StraddleSession).count(), 0,
                         'Bought 200 of options with only 50 cash')

    def test_hedge_enforces_configured_minimum_paper_balance(self):
        t = self.t
        t.config(fixture.HedgeConfig, 'PAPER_WALLET_USDT', 0)
        t.config(fixture.HedgeConfig, 'MIN_PAPER_BALANCE', 1000)
        asyncio.run(fixture.he.HedgeEngine().tick(t.db))
        self.assertEqual(t.db.query(fixture.HedgeSession).count(), 0,
                         'Entered despite cash below configured MIN_PAPER_BALANCE')


if __name__ == '__main__':
    unittest.main()
