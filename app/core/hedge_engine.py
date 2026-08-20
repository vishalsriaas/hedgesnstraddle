import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple, List
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models.schema import (
    HedgeConfig, HedgeStrategyConfig, PendingConfig, ConfigAuditLog, HedgeSession, 
    HedgeTradeOrder, HedgeFill, HedgeOpenPosition, HedgePaperLedgerEntry
)
from app.core.binance_client import (
    get_btc_spot_price, get_btc_futures_mark_price, get_btc_options_mark_prices
)

logger = logging.getLogger("hedgesnstraddle.hedge_engine")

ist = timezone(timedelta(hours=5, minutes=30))

def get_session_relative_minutes(time_str: str) -> int:
    try:
        h, m = map(int, time_str.split(":"))
        rel_m = h * 60 + m
        return rel_m if rel_m >= 300 else rel_m + 1440
    except Exception:
        return 0

class HedgeEngine:
    def __init__(self):
        self.is_running = False
        self.state = "IDLE"
        self.active_role = "1st Trader"
        self._task: Optional[asyncio.Task] = None
        
        # Session state variables
        self.slot1_session_id: Optional[int] = None
        self.slot2_session_id: Optional[int] = None
        self.slot1_strike: Optional[float] = None
        self.slot2_strike: Optional[float] = None
        self.slot1_option_mark: Optional[float] = None
        self.slot2_option_mark: Optional[float] = None
        
        self.tp_rank_1_slot: Optional[str] = None  # '1st Trader' or '2nd Trader'
        self.tp_rank_2_slot: Optional[str] = None
        
        self.last_futures_mark: float = 64000.0
        self.last_spot_price: float = 64000.0

    def load_config(self, db: Session) -> Dict[str, str]:
        configs = db.query(HedgeConfig).all()
        return {c.key: c.value for c in configs}

    def get_live_monitoring_snapshot(self, db: Session) -> Dict[str, Any]:
        """Returns dynamic Hedge workflow status, dual trader cards & condition checks for the frontend."""
        cfg = self.load_config(db)
        slot1_cfg = self.get_role_strategy_config(db, "1st Trader")
        slot2_cfg = self.get_role_strategy_config(db, "2nd Trader")

        w_start_h = slot1_cfg.trade_start_h if slot1_cfg else 6
        w_start_m = slot1_cfg.trade_start_m if slot1_cfg else 0
        w_end_h = slot1_cfg.trade_end_h if slot1_cfg else 7
        w_end_m = slot1_cfg.trade_end_m if slot1_cfg else 30
        sq_h = slot1_cfg.force_close_h if slot1_cfg else 11
        sq_m = slot1_cfg.force_close_m if slot1_cfg else 30

        window_start = f"{w_start_h:02d}:{w_start_m:02d}"
        window_end = f"{w_end_h:02d}:{w_end_m:02d}"
        sq_end = f"{sq_h:02d}:{sq_m:02d}"

        now_time_full = datetime.now(ist).strftime("%H:%M:%S")
        now_time_str = now_time_full[:5]

        now_rel = get_session_relative_minutes(now_time_str)
        w_start_rel = get_session_relative_minutes(window_start)
        w_end_rel = get_session_relative_minutes(window_end)
        sq_end_rel = get_session_relative_minutes(sq_end)

        cond_time_window_valid = (w_start_rel <= now_rel <= w_end_rel)
        max_premium = slot1_cfg.max_premium if slot1_cfg else 280.0
        max_option_spend = float(cfg.get("MAX_OPTION_SPEND", "400.0"))

        cond_rule_a_valid = True
        cond_rule_b_valid = ((self.slot1_option_mark or 150.0) <= max_premium)
        cond_rule_c_valid = True
        if self.slot1_strike is not None and self.slot2_strike is not None:
            cond_rule_c_valid = (self.slot2_strike >= self.slot1_strike)

        cond_max_spend_valid = ((self.slot1_option_mark or 150.0) * (slot1_cfg.contract_qty if slot1_cfg else 1.0) <= max_option_spend)

        slot1_dir = slot1_cfg.direction if slot1_cfg else "Bullish"
        slot1_qty = slot1_cfg.contract_qty if slot1_cfg else 1.0
        slot1_fut_entry = self.last_futures_mark
        slot1_tp = (slot1_fut_entry + (self.slot1_option_mark or 150.0)) if slot1_dir == "Bullish" else (slot1_fut_entry - (self.slot1_option_mark or 150.0))

        slot2_dir = slot2_cfg.direction if slot2_cfg else "Bearish"
        slot2_qty = slot2_cfg.contract_qty if slot2_cfg else 1.0
        slot2_fut_entry = self.last_futures_mark
        slot2_tp = (slot2_fut_entry + (self.slot2_option_mark or 150.0)) if slot2_dir == "Bullish" else (slot2_fut_entry - (self.slot2_option_mark or 150.0))

        hedge_wallet_val = float(cfg.get("PAPER_WALLET_USDT", "100000.0"))

        return {
            "state": self.state,
            "active_role": self.active_role,
            "server_time": now_time_full,
            "last_spot_price": self.last_spot_price,
            "last_futures_mark": self.last_futures_mark,
            "window_start": window_start,
            "window_end": window_end,
            "sq_end": sq_end,
            "max_option_spend": max_option_spend,
            "hedge_paper_wallet_usdt": hedge_wallet_val,
            "slot1": {
                "role": "1st Trader",
                "direction": slot1_dir,
                "qty": slot1_qty,
                "strike": self.slot1_strike or (round(self.last_futures_mark / 250.0) * 250.0),
                "option_mark": self.slot1_option_mark or 150.0,
                "futures_entry": slot1_fut_entry if self.slot1_session_id else 0.0,
                "futures_tp": slot1_tp if self.slot1_session_id else 0.0,
                "status": "Active" if self.slot1_session_id else "Idle"
            },
            "slot2": {
                "role": "2nd Trader",
                "direction": slot2_dir,
                "qty": slot2_qty,
                "strike": self.slot2_strike or (round(self.last_futures_mark / 250.0) * 250.0),
                "option_mark": self.slot2_option_mark or 150.0,
                "futures_entry": slot2_fut_entry if self.slot2_session_id else 0.0,
                "futures_tp": slot2_tp if self.slot2_session_id else 0.0,
                "status": "Active" if self.slot2_session_id else "Idle"
            },
            "cond_time_window_valid": cond_time_window_valid,
            "cond_rule_a_valid": cond_rule_a_valid,
            "cond_rule_b_valid": cond_rule_b_valid,
            "cond_rule_c_valid": cond_rule_c_valid,
            "cond_max_spend_valid": cond_max_spend_valid
        }

    def get_role_strategy_config(self, db: Session, role_name: str) -> Optional[HedgeStrategyConfig]:
        """Fetch dynamic Hedge Strategy Config parameters by role name ('1st Trader' vs '2nd Trader')."""
        return db.query(HedgeStrategyConfig).filter(HedgeStrategyConfig.strategy_name == role_name).first()

    def validate_option_spend(self, option_ask: float = 0.0, qty: float = 1.0, max_option_spend: float = 400.0, option_mark: Optional[float] = None) -> bool:
        """Enforces MAX_OPTION_SPEND limit: reject option purchase if (cost) > MAX_OPTION_SPEND."""
        price = option_mark if option_mark is not None else option_ask
        total_cost = price * qty
        if total_cost > max_option_spend:
            logger.warning("Option spend $%.2f exceeds limit $%.2f - REJECTED", total_cost, max_option_spend)
            return False
        return True

    def flush_pending_config_on_session_close(self, db: Session):
        pending_items = db.query(PendingConfig).filter(PendingConfig.config_type == "HEDGE").all()
        if not pending_items:
            return

        logger.info("Hedge session completed. Flushing %d pending hedge config updates...", len(pending_items))
        for p in pending_items:
            old_item = db.query(HedgeConfig).filter(HedgeConfig.key == p.field_name).first()
            old_val = old_item.value if old_item else ""

            if old_item:
                old_item.value = p.pending_value
            else:
                db.add(HedgeConfig(key=p.field_name, value=p.pending_value))

            audit = ConfigAuditLog(
                user_email=p.user_email,
                config_type="HEDGE",
                field_name=p.field_name,
                old_value=old_val,
                new_value=p.pending_value,
                apply_mode="DEFERRED_ON_SESSION_CLOSE",
                status="APPLIED",
                ip_address="HEDGE_EVENT_LOOP"
            )
            db.add(audit)
            db.delete(p)

        db.commit()
        logger.info("Successfully applied pending hedge configurations!")

    async def find_nearest_itm_option(self, futures_mark: float, direction: str) -> Tuple[float, float, str, str]:
        """
        Rule A: Finds nearest In-The-Money (ITM) option contract for today's expiry.
        Bullish -> Nearest PUT (Strike K >= futures_mark)
        Bearish -> Nearest CALL (Strike K <= futures_mark)
        Returns: (strike_price, option_mark, expiry_sym, symbol)
        """
        mark_prices = await get_btc_options_mark_prices()
        now_dt = datetime.now(ist)
        today_sym = now_dt.strftime("%y%m%d")

        candidates = []
        if mark_prices:
            for item in mark_prices:
                sym = item.get("symbol", "")
                parts = sym.split("-")
                if len(parts) == 4 and parts[0] == "BTC":
                    exp_sym, strk_str, opt_type = parts[1], parts[2], parts[3]
                    if exp_sym == today_sym:
                        try:
                            strk = float(strk_str)
                            mark_p = float(item.get("markPrice", 150.0))
                            candidates.append((strk, mark_p, exp_sym, opt_type, sym))
                        except ValueError:
                            pass

        target_type = "P" if direction == "Bullish" else "C"
        matching = [c for c in candidates if c[3] == target_type]

        if not matching:
            # Fallback strike estimation if mock/live mark feed unavailable
            strike = round(futures_mark / 250.0) * 250.0
            return (strike, 150.0, today_sym, f"BTC-{today_sym}-{int(strike)}-{target_type}")

        if direction == "Bullish":
            # ITM PUT: Strike >= futures_mark, pick smallest Strike >= futures_mark
            itm = [c for c in matching if c[0] >= futures_mark]
            best = min(itm, key=lambda x: x[0]) if itm else max(matching, key=lambda x: x[0])
        else:
            # ITM CALL: Strike <= futures_mark, pick largest Strike <= futures_mark
            itm = [c for c in matching if c[0] <= futures_mark]
            best = max(itm, key=lambda x: x[0]) if itm else min(matching, key=lambda x: x[0])

        return (best[0], best[1], best[2], best[4])

    async def execute_slot_entry(
        self, db: Session, role_name: str, role_config: HedgeStrategyConfig, 
        futures_mark: float, spot_price: float, max_option_spend: float
    ) -> Optional[int]:
        """
        Executes atomic slot trade: BUY Option @ option_mark + OPEN Futures @ futures_mark.
        Sets Futures TP = futures_entry ± option_mark.
        """
        direction = role_config.direction or "Bullish"
        qty = role_config.contract_qty
        max_premium = role_config.max_premium

        strike, option_mark, expiry_sym, opt_symbol = await self.find_nearest_itm_option(futures_mark, direction)

        # Rule B: Premium Cap Check (option_mark <= max_premium) and Spend Limit
        if option_mark > max_premium:
            logger.warning("Hedge Slot [%s] option_mark $%.2f > max_premium limit $%.2f - REJECTED", role_name, option_mark, max_premium)
            return None

        if not self.validate_option_spend(option_mark, qty, max_option_spend):
            return None

        now_ist = datetime.now(ist).replace(tzinfo=None)

        # Calculate Futures TP Level based on option_mark
        is_bullish = (direction == "Bullish")
        fut_side = "BUY" if is_bullish else "SELL"
        fut_tp_price = futures_mark + option_mark if is_bullish else futures_mark - option_mark

        # 1. Create HedgeSession
        sess = HedgeSession(
            symbol="BTCUSDT",
            status="Open",
            bull_entry=futures_mark if is_bullish else 0.0,
            bear_entry=futures_mark if not is_bullish else 0.0,
            created_at=now_ist
        )
        db.add(sess)
        db.flush()

        # 2. Record Option BUY Trade Order
        opt_side = "BUY"
        opt_label = "PUT" if is_bullish else "CALL"
        opt_order = HedgeTradeOrder(
            session_id=sess.id,
            symbol=opt_symbol,
            side=opt_side,
            trader_leg=role_name,
            order_type="MARKET",
            qty=qty,
            price=option_mark,
            status="FILLED",
            created_at=now_ist
        )
        db.add(opt_order)

        # 3. Record Futures Open Trade Order
        fut_order = HedgeTradeOrder(
            session_id=sess.id,
            symbol="BTC-USDT-FUTURES",
            side=fut_side,
            trader_leg=role_name,
            order_type="MARKET",
            qty=qty,
            price=futures_mark,
            status="FILLED",
            created_at=now_ist
        )
        db.add(fut_order)

        # 4. Record Open Position Snapshots
        opt_pos = HedgeOpenPosition(
            session_id=sess.id,
            symbol=opt_symbol,
            side="LONG",
            entry_price=option_mark,
            qty=qty,
            unrealized_pnl=0.0
        )
        fut_pos = HedgeOpenPosition(
            session_id=sess.id,
            symbol="BTC-USDT-FUTURES",
            side="LONG" if is_bullish else "SHORT",
            entry_price=futures_mark,
            qty=qty,
            unrealized_pnl=0.0
        )
        db.add(opt_pos)
        db.add(fut_pos)

        db.commit()
        logger.info("Hedge Slot [%s] Entered: Strategy %s, Strike $%.0f, OptionMark $%.2f, FuturesEntry $%.2f, FuturesTP $%.2f",
                    role_name, direction, strike, option_mark, futures_mark, fut_tp_price)

        if role_name == "1st Trader":
            self.slot1_session_id = sess.id
            self.slot1_strike = strike
            self.slot1_option_mark = option_mark
        else:
            self.slot2_session_id = sess.id
            self.slot2_strike = strike
            self.slot2_option_mark = option_mark

        return sess.id

    async def execute_squareoff(self, db: Session, reason: str = "11:30 AM Universal Squareoff"):
        """Forces 11:30 AM universal market squareoff for all open hedge slots."""
        logger.info("Executing Hedge Universal Squareoff: %s", reason)
        cfg = self.load_config(db)
        now_ist = datetime.now(ist).replace(tzinfo=None)

        futures_mark = await get_btc_futures_mark_price()
        open_positions = db.query(HedgeOpenPosition).all()

        total_realized_pnl = 0.0

        for pos in open_positions:
            qty = pos.qty
            if "FUTURES" in pos.symbol:
                if pos.side == "LONG":
                    pnl = (futures_mark - pos.entry_price) * qty
                else:
                    pnl = (pos.entry_price - futures_mark) * qty
                
                close_order = HedgeTradeOrder(
                    session_id=pos.session_id,
                    symbol=pos.symbol,
                    side="SELL" if pos.side == "LONG" else "BUY",
                    trader_leg=self.active_role,
                    order_type="MARKET",
                    qty=qty,
                    price=futures_mark,
                    status="FILLED",
                    created_at=now_ist
                )
                db.add(close_order)
            else:
                # Option close at market
                pnl = (100.0 - pos.entry_price) * qty  # fallback option close mark
                close_order = HedgeTradeOrder(
                    session_id=pos.session_id,
                    symbol=pos.symbol,
                    side="SELL",
                    trader_leg=self.active_role,
                    order_type="MARKET",
                    qty=qty,
                    price=100.0,
                    status="FILLED",
                    created_at=now_ist
                )
                db.add(close_order)

            total_realized_pnl += pnl
            db.delete(pos)

        open_sessions = db.query(HedgeSession).filter(HedgeSession.status == "Open").all()
        for sess in open_sessions:
            sess.status = "Completed"
            sess.exit_reason = reason
            sess.realized_pnl = total_realized_pnl

        # Update Ledger
        cash_item = db.query(HedgeConfig).filter(HedgeConfig.key == "PAPER_WALLET_USDT").first()
        old_cash = float(cash_item.value) if cash_item else 100000.0
        new_cash = old_cash + total_realized_pnl
        if cash_item:
            cash_item.value = str(new_cash)

        ledger = HedgePaperLedgerEntry(
            session_id=self.slot1_session_id or self.slot2_session_id,
            entry_type="SQUAREOFF_SETTLEMENT",
            amount=total_realized_pnl,
            balance_after=new_cash,
            detail=f"Hedge Squareoff Settlement ({reason}) - Net PnL: ${total_realized_pnl:.2f}",
            created_at=now_ist
        )
        db.add(ledger)

        db.commit()

        self.slot1_session_id = None
        self.slot2_session_id = None
        self.slot1_strike = None
        self.slot2_strike = None
        self.tp_rank_1_slot = None
        self.tp_rank_2_slot = None
        self.state = "COMPLETED"

    async def run_loop(self):
        self.is_running = True
        logger.info("Hedge Engine async loop started.")
        while self.is_running:
            try:
                db = SessionLocal()
                cfg = self.load_config(db)
                bot_enabled = cfg.get("BOT_ENABLED", "1") == "1"

                if not bot_enabled:
                    self.state = "DISABLED"
                    db.close()
                    await asyncio.sleep(2.0)
                    continue

                spot_price = await get_btc_spot_price()
                futures_mark = await get_btc_futures_mark_price()
                self.last_spot_price = spot_price
                self.last_futures_mark = futures_mark

                now_time_full = datetime.now(ist).strftime("%H:%M:%S")
                now_time_str = now_time_full[:5]
                now_rel = get_session_relative_minutes(now_time_str)

                slot1_cfg = self.get_role_strategy_config(db, "1st Trader")
                slot2_cfg = self.get_role_strategy_config(db, "2nd Trader")

                w_start_h = slot1_cfg.trade_start_h if slot1_cfg else 6
                w_start_m = slot1_cfg.trade_start_m if slot1_cfg else 0
                w_end_h = slot1_cfg.trade_end_h if slot1_cfg else 7
                w_end_m = slot1_cfg.trade_end_m if slot1_cfg else 30
                sq_h = slot1_cfg.force_close_h if slot1_cfg else 11
                sq_m = slot1_cfg.force_close_m if slot1_cfg else 30

                w_start_rel = get_session_relative_minutes(f"{w_start_h:02d}:{w_start_m:02d}")
                w_end_rel = get_session_relative_minutes(f"{w_end_h:02d}:{w_end_m:02d}")
                sq_end_rel = get_session_relative_minutes(f"{sq_h:02d}:{sq_m:02d}")

                max_option_spend = float(cfg.get("MAX_OPTION_SPEND", "400.0"))

                # 1. State Transition: Entry Window Active
                if w_start_rel <= now_rel <= w_end_rel and self.state in ["IDLE", "SQUAREOFF", "COMPLETED"]:
                    self.state = "ENTRY_WINDOW"
                    logger.info("Entering Hedge Entry Window (%02d:%02d - %02d:%02d)", w_start_h, w_start_m, w_end_h, w_end_m)

                # 2. Phase 1: Slot 1 & Slot 2 Entry Evaluation
                if self.state == "ENTRY_WINDOW":
                    # Evaluate Slot 1
                    if not self.slot1_session_id and slot1_cfg and slot1_cfg.enabled:
                        await self.execute_slot_entry(db, "1st Trader", slot1_cfg, futures_mark, spot_price, max_option_spend)

                    # Evaluate Slot 2 with Rule C (Clash Check)
                    if not self.slot2_session_id and slot2_cfg and slot2_cfg.enabled:
                        should_evaluate_slot2 = True
                        if self.slot1_session_id and self.slot1_strike is not None:
                            # Rule C Clash Check: Slot 2 strike must be >= Slot 1 strike
                            temp_strike, _, _, _ = await self.find_nearest_itm_option(futures_mark, slot2_cfg.direction)
                            if temp_strike < self.slot1_strike:
                                logger.info("Slot 2 Clash Check Failed: Strike $%.0f < Slot 1 Strike $%.0f", temp_strike, self.slot1_strike)
                                should_evaluate_slot2 = False

                        if should_evaluate_slot2:
                            await self.execute_slot_entry(db, "2nd Trader", slot2_cfg, futures_mark, spot_price, max_option_spend)

                    if self.slot1_session_id or self.slot2_session_id:
                        self.state = "IN_TRADE"

                # 3. Phase 3: Universal Squareoff Check
                if self.state in ["SQUAREOFF"] or (now_rel >= sq_end_rel and self.state in ["ENTRY_WINDOW", "IN_TRADE"]):
                    await self.execute_squareoff(db, "11:30 AM Universal Squareoff" if self.state != "SQUAREOFF" else "Manual Emergency Squareoff")

                # 4. Flush Pending Configs on Session Complete
                if self.state == "COMPLETED":
                    self.flush_pending_config_on_session_close(db)
                    self.state = "IDLE"

                db.close()
            except Exception as e:
                logger.error("Error in Hedge Engine loop: %s", str(e), exc_info=True)

            await asyncio.sleep(2.0)

    def start(self):
        if not self.is_running:
            self._task = asyncio.create_task(self.run_loop())

    def stop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()

hedge_engine = HedgeEngine()
