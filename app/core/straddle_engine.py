import asyncio
import logging
import math
from datetime import datetime
import pytz
ist = pytz.timezone('Asia/Kolkata')
from typing import Dict, Any, Optional, Tuple, List
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models.schema import (
    StraddleConfig, PendingConfig, ConfigAuditLog, StraddleSession, 
    StraddleTradeOrder, StraddleFill, StraddleWalletLedger
)
from app.core.binance_client import get_btc_spot_price, get_btc_futures_mark_price, get_btc_options_tickers, get_btc_options_mark_prices

from app.core.market_data import option_mark, unexpired

logger = logging.getLogger("hedgesnstraddle.straddle_engine")

def get_session_relative_minutes(time_str: str) -> int:
    try:
        parts = time_str.split(":")
        h = int(parts[0])
        m = int(parts[1])
        total_mins = h * 60 + m
        # Expiry Session boundary starts at 13:31 IST
        session_start_mins = 13 * 60 + 31
        
        if total_mins >= session_start_mins:
            return total_mins - session_start_mins
        else:
            return (total_mins + 24 * 60) - session_start_mins
    except Exception:
        return 0

class StraddleEngine:
    def __init__(self):
        self.is_running = False
        self.active_session_id: Optional[int] = None
        self.state = "IDLE"
        
        # Live state properties
        self.last_spot_price: float = 0.0
        self.last_futures_mark: float = 0.0
        self.nearest_expiry: str = "N/A"
        self.current_strike: float = 0.0
        self.current_call_mark: float = 0.0
        self.current_put_mark: float = 0.0
        self.combined_premium: float = 0.0
        
        # Active session position mark tracking (locked to bought strikes)
        self.active_call_mark: float = 0.0
        self.active_put_mark: float = 0.0
        
        # Calculated OCO limit levels
        self.short_limit_price: float = 0.0
        self.long_limit_price: float = 0.0
        self.is_short_limit_active: bool = False
        self.is_long_limit_active: bool = False
        self.target_tp_price: float = 0.0
        
        # Checked conditions status
        self.cond_time_window_valid: bool = False
        self.cond_premium_valid: bool = False
        self.cond_premium_gap_valid: bool = False
        self.cond_same_strike_valid: bool = True
        self.cond_itm_otm_valid: bool = True
        self.cond_weekend_skip: bool = False
        
        self._task: Optional[asyncio.Task] = None

    def load_config(self, db: Session) -> Dict[str, str]:
        configs = db.query(StraddleConfig).all()
        return {c.key: c.value for c in configs}

    def flush_pending_config_on_window_close(self, db: Session):
        pending_items = db.query(PendingConfig).filter(PendingConfig.config_type == "STRADDLE").all()
        if not pending_items:
            return

        logger.info("Window closed. Flushing %d pending straddle config updates...", len(pending_items))
        for p in pending_items:
            old_item = db.query(StraddleConfig).filter(StraddleConfig.key == p.field_name).first()
            old_val = old_item.value if old_item else ""

            if old_item:
                old_item.value = p.pending_value
            else:
                db.add(StraddleConfig(key=p.field_name, value=p.pending_value))

            audit = ConfigAuditLog(
                user_email=p.user_email,
                config_type="STRADDLE",
                field_name=p.field_name,
                old_value=old_val,
                new_value=p.pending_value,
                apply_mode="DEFERRED_ON_WINDOW_CLOSE",
                status="APPLIED",
                ip_address="WINDOW_EVENT_LOOP"
            )
            db.add(audit)
            db.delete(p)

        db.commit()
        logger.info("Successfully applied pending straddle configurations!")

    async def find_same_strike_pair(self, futures_mark: float) -> Tuple[float, float, float, str]:
        """
        Finds the nearest ATM strike using BTC FUTURES MARK PRICE as the reference.

        Why futures mark (not spot):
          - Binance options are priced off the futures mark price, not spot
          - ITM/OTM is defined as: Call ITM if Strike < futures_mark, Put ITM if Strike > futures_mark
          - Using futures mark avoids basis drift between spot and futures

        Returns: (best_strike, call_mark_price, put_mark_price, expiry_date)
        """
        tickers = await get_btc_options_mark_prices()
        symbols = {q.get("symbol", "") for q in tickers}
        pairs = []
        for symbol in symbols:
            if not symbol.endswith("-C") or not unexpired(symbol):
                continue
            put_symbol = symbol[:-1] + "P"
            if put_symbol not in symbols:
                continue
            try:
                call = option_mark(tickers, symbol)
                put = option_mark(tickers, put_symbol)
                parts = symbol.split("-")
                if call > 0 and put > 0:
                    pairs.append((parts[1], float(parts[2]), call, put))
            except ValueError:
                continue
        if not pairs:
            raise ValueError("DATA_GAP: No complete unexpired call/put pair")
        expiry, strike, call, put = min(pairs, key=lambda p: (p[0], abs(p[1] - futures_mark), p[1]))
        return strike, call, put, expiry

    async def get_specific_strike_marks(self, call_strike, put_strike, expiry=None):
        tickers = await get_btc_options_mark_prices()
        return (option_mark(tickers, f"BTC-{expiry}-{int(call_strike)}-C"),
                option_mark(tickers, f"BTC-{expiry}-{int(put_strike)}-P"))

    def session_qty(self, db, session_id):
        order = db.query(StraddleTradeOrder).filter(
            StraddleTradeOrder.session_id == session_id,
            StraddleTradeOrder.asset_type == "OPTION",
            StraddleTradeOrder.side == "BUY",
            StraddleTradeOrder.status == "FILLED").first()
        if not order or not math.isfinite(order.qty) or order.qty <= 0:
            raise ValueError("Invalid or missing original straddle fill quantity")
        return order.qty

    def restore_session(self, db):
        manual = self.state == "SQUAREOFF"
        sess = db.query(StraddleSession).filter(
            StraddleSession.status.in_(["Open", "OPEN", "MANUAL_SQUAREOFF"])
        ).order_by(StraddleSession.id).first()
        self.active_session_id = sess.id if sess else None
        if not sess:
            if not manual:
                self.state = "IDLE"
            return
        orders = db.query(StraddleTradeOrder).filter(StraddleTradeOrder.session_id == sess.id).all()
        self.is_short_limit_active = self.is_long_limit_active = False
        for order in orders:
            if order.leg_label == "SHORT_LIMIT":
                self.short_limit_price = order.price
                self.is_short_limit_active = order.status == "PENDING"
            elif order.leg_label == "LONG_LIMIT":
                self.long_limit_price = order.price
                self.is_long_limit_active = order.status == "PENDING"
        self.combined_premium = sess.net_straddle_ask
        self.target_tp_price = sess.futures_tp_price or 0.0
        if manual or sess.status == "MANUAL_SQUAREOFF":
            self.state = "SQUAREOFF"
        elif sess.futures_entry_price and sess.futures_entry_price > 0:
            self.state = "IN_TRADE"
        elif self.is_short_limit_active or self.is_long_limit_active:
            self.state = "LIMITS_PLACED"
        else:
            self.state = "RECOVERY"

    def get_live_monitoring_snapshot(self, db: Session) -> Dict[str, Any]:
        """Returns dynamic workflow status and limit order computations for the frontend."""
        cfg = self.load_config(db)
        
        window_start = cfg.get("WINDOW_START", "05:00")
        window_end = cfg.get("WINDOW_END", "07:30")
        futures_entry_cutoff = cfg.get("FUTURES_ENTRY_CUTOFF", "11:00")
        sq_end = cfg.get("SQ_END", "12:30")
        max_premium_limit = float(cfg.get("MAX_TOTAL_MARK", "400.0"))
        max_gap_limit = float(cfg.get("MAX_PREMIUM_GAP", "150.0"))
        
        now_time_full = datetime.now(ist).strftime("%H:%M:%S")
        now_time_str = now_time_full[:5]
        
        now_rel = get_session_relative_minutes(now_time_str)
        w_start_rel = get_session_relative_minutes(window_start)
        w_end_rel = get_session_relative_minutes(window_end)
        
        # Check conditions
        self.cond_time_window_valid = (w_start_rel <= now_rel <= w_end_rel)
        self.cond_premium_valid = (self.combined_premium <= max_premium_limit)
        self.cond_premium_gap_valid = (abs(self.current_call_mark - self.current_put_mark) <= max_gap_limit)
        
        # ITM / OTM verification: one must be <= spot, the other >= spot at same strike K
        self.cond_itm_otm_valid = True  # Inherently true for same strike model
        
        # Check Weekend Skip Rule
        skip_weekends_enabled = cfg.get("SKIP_WEEKENDS", "1") == "1"
        is_weekend = False
        if self.nearest_expiry != "N/A":
            try:
                expiry_dt = datetime.strptime(f"20{self.nearest_expiry}", "%Y%m%d")
                if expiry_dt.weekday() in [5, 6]:  # Saturday (5) or Sunday (6)
                    is_weekend = True
            except Exception:
                pass
        self.cond_weekend_skip = is_weekend and skip_weekends_enabled
        
        return {
            "state": self.state,
            "server_time": now_time_full,
            "last_spot_price": self.last_spot_price,
            "last_futures_mark": self.last_futures_mark,
            "nearest_expiry": self.nearest_expiry,
            "current_strike": self.current_strike,
            "current_call_mark": self.current_call_mark,
            "current_put_mark": self.current_put_mark,
            "combined_premium": self.combined_premium,
            "short_limit_price": self.short_limit_price,
            "long_limit_price": self.long_limit_price,
            
            # Constraints
            "window_start": window_start,
            "window_end": window_end,
            "futures_entry_cutoff": futures_entry_cutoff,
            "sq_end": sq_end,
            "max_premium_limit": max_premium_limit,
            "max_gap_limit": max_gap_limit,
            
            # Condition check results
            "cond_time_window_valid": self.cond_time_window_valid,
            "cond_premium_valid": self.cond_premium_valid,
            "cond_premium_gap_valid": self.cond_premium_gap_valid,
            "cond_same_strike_valid": self.cond_same_strike_valid,
            "cond_itm_otm_valid": self.cond_itm_otm_valid,
            "cond_weekend_skip": self.cond_weekend_skip
        }

    async def run_loop(self):
        self.is_running = True
        logger.info("Straddle Engine async loop started.")
        while self.is_running:
            db = SessionLocal()
            try:
                cfg = self.load_config(db)
                bot_enabled = cfg.get("BOT_ENABLED", "1") == "1"

                self.restore_session(db)
                if not bot_enabled and not self.active_session_id and self.state != "SQUAREOFF":
                    self.state = "DISABLED"
                    db.close()
                    await asyncio.sleep(2.0)
                    continue

                # Query live metrics
                spot = await get_btc_spot_price()
                self.last_spot_price = spot

                # Use FUTURES MARK PRICE as reference for strike selection
                # (Binance options are priced relative to futures mark, not spot)
                futures_mark = await get_btc_futures_mark_price()
                self.last_futures_mark = futures_mark

                if self.active_session_id:
                    held = db.get(StraddleSession, self.active_session_id)
                    strike, expiry = held.call_strike, held.expiry_sym
                    call_mark, put_mark = await self.get_specific_strike_marks(held.call_strike, held.put_strike, expiry)
                else:
                    strike, call_mark, put_mark, expiry = await self.find_same_strike_pair(futures_mark)
                self.current_strike = strike
                self.current_call_mark = call_mark
                self.current_put_mark = put_mark
                self.nearest_expiry = expiry
                if not self.active_session_id:
                    oco_limit_multiplier = float(cfg.get("OCO_LIMIT_MULTIPLIER", "1.0"))
                    self.combined_premium = call_mark + put_mark
                    self.short_limit_price = strike + self.combined_premium * oco_limit_multiplier
                    self.long_limit_price = strike - self.combined_premium * oco_limit_multiplier

                now_time_str = datetime.now(ist).strftime("%H:%M")
                now_rel = get_session_relative_minutes(now_time_str)
                
                window_start = cfg.get("WINDOW_START", "05:00")
                window_end = cfg.get("WINDOW_END", "07:30")
                cutoff_time = cfg.get("FUTURES_ENTRY_CUTOFF", "11:00")
                sq_end = cfg.get("SQ_END", "12:30")
                
                w_start_rel = get_session_relative_minutes(window_start)
                w_end_rel = get_session_relative_minutes(window_end)
                cutoff_rel = get_session_relative_minutes(cutoff_time)
                sq_end_rel = get_session_relative_minutes(sq_end)

                # Parse Expiry Weekend check
                skip_weekends = cfg.get("SKIP_WEEKENDS", "1") == "1"
                is_weekend_session = False
                if expiry != "N/A" and skip_weekends:
                    try:
                        expiry_dt = datetime.strptime(f"20{expiry}", "%Y%m%d")
                        if expiry_dt.weekday() in [5, 6]:
                            is_weekend_session = True
                    except Exception:
                        pass

                self.active_call_mark = call_mark if self.active_session_id else 0.0
                self.active_put_mark = put_mark if self.active_session_id else 0.0

                # Handle state transitions and simulated trade punching
                if bot_enabled and w_start_rel <= now_rel <= w_end_rel and self.state in ["IDLE", "COMPLETED"]:
                    self.state = "ENTRY_WINDOW"
                    logger.info("Entering Straddle Entry Window (%s - %s)", window_start, window_end)

                # Punch straddle entry if all conditions met
                if bot_enabled and w_start_rel <= now_rel <= w_end_rel and self.state == "ENTRY_WINDOW" and not self.active_session_id:
                    max_premium_limit = float(cfg.get("MAX_TOTAL_MARK", "400.0"))
                    max_gap_limit = float(cfg.get("MAX_PREMIUM_GAP", "150.0"))

                    premium_ok = (self.combined_premium <= max_premium_limit)
                    gap_ok = (abs(call_mark - put_mark) <= max_gap_limit)

                    last_traded = cfg.get("LAST_TRADED_EXPIRY", "")
                    db_existing_sess = db.query(StraddleSession).filter(StraddleSession.expiry_sym == expiry).first() if expiry != "N/A" else None
                    already_traded = (expiry != "N/A" and (last_traded == expiry or db_existing_sess is not None))

                    if premium_ok and gap_ok and not is_weekend_session and not already_traded:
                        qty = float(cfg.get("TRADE_QTY", "10"))
                        if not math.isfinite(qty) or qty <= 0:
                            raise ValueError("TRADE_QTY must be positive")

                        now_ist = datetime.now(ist).replace(tzinfo=None)

                        # 1. Create a Straddle Session in database
                        new_sess = StraddleSession(
                            expiry_sym=expiry,
                            expiry_dt=expiry,
                            status="Open",
                            btc_entry_spot=spot,
                            call_strike=strike,
                            call_ask=call_mark,   # storing mark price as entry price
                            put_strike=strike,
                            put_ask=put_mark,     # storing mark price as entry price
                            net_straddle_ask=self.combined_premium,
                            pnl_realized=0.0,
                            created_at=now_ist
                        )
                        db.add(new_sess)
                        db.flush()
                        self.active_session_id = new_sess.id

                        # Save last traded expiry to prevent duplicate session triggers
                        last_traded_cfg = db.query(StraddleConfig).filter(StraddleConfig.key == "LAST_TRADED_EXPIRY").first()
                        if last_traded_cfg:
                            last_traded_cfg.value = expiry
                        else:
                            db.add(StraddleConfig(key="LAST_TRADED_EXPIRY", value=expiry))
                        db.flush()

                        # 2. Add simulated BUY fill records for Call and Put options at mark price
                        call_ord = StraddleTradeOrder(
                            session_id=new_sess.id,
                            symbol=f"BTC-{expiry}-{int(strike)}-C",
                            asset_type="OPTION",
                            side="BUY",
                            leg_label="CALL",
                            order_type="MARKET",
                            qty=qty,
                            price=call_mark,    # entry at mark price
                            status="FILLED",
                            created_at=now_ist
                        )
                        put_ord = StraddleTradeOrder(
                            session_id=new_sess.id,
                            symbol=f"BTC-{expiry}-{int(strike)}-P",
                            asset_type="OPTION",
                            side="BUY",
                            leg_label="PUT",
                            order_type="MARKET",
                            qty=qty,
                            price=put_mark,     # entry at mark price
                            status="FILLED",
                            created_at=now_ist
                        )
                        db.add(call_ord)
                        db.add(put_ord)

                        # 3. Add pending OCO Futures Limit Orders immediately after option entry fills
                        short_limit_ord = StraddleTradeOrder(
                            session_id=new_sess.id,
                            symbol="BTC-USDT-FUTURES",
                            asset_type="FUTURES",
                            side="SELL",
                            leg_label="SHORT_LIMIT",
                            order_type="LIMIT",
                            qty=qty,
                            price=self.short_limit_price,
                            status="PENDING",
                            created_at=now_ist
                        )
                        long_limit_ord = StraddleTradeOrder(
                            session_id=new_sess.id,
                            symbol="BTC-USDT-FUTURES",
                            asset_type="FUTURES",
                            side="BUY",
                            leg_label="LONG_LIMIT",
                            order_type="LIMIT",
                            qty=qty,
                            price=self.long_limit_price,
                            status="PENDING",
                            created_at=now_ist
                        )
                        db.add(short_limit_ord)
                        db.add(long_limit_ord)
                        db.flush()

                        # 4. Deduct option entry premium cost from virtual margin account & record wallet ledger
                        total_cost = self.combined_premium * qty
                        wallet_item = db.query(StraddleConfig).filter(StraddleConfig.key == "PAPER_WALLET_USDT").first()
                        old_balance = float(wallet_item.value) if wallet_item else 100000.0
                        new_balance = old_balance - total_cost
                        if wallet_item:
                            wallet_item.value = str(new_balance)
                        else:
                            db.add(StraddleConfig(key="PAPER_WALLET_USDT", value=str(new_balance)))

                        ledger_entry = StraddleWalletLedger(
                            session_id=new_sess.id,
                            entry_type="PREMIUM_BUY",
                            amount=-total_cost,
                            balance_after=new_balance,
                            detail=f"Option entry premium cost for Session #{new_sess.id} (Call mark ${call_mark:.2f} + Put mark ${put_mark:.2f}) x {qty} BTC",
                            created_at=now_ist
                        )
                        db.add(ledger_entry)
                        db.commit()

                        # Set limits state active
                        self.is_short_limit_active = True
                        self.is_long_limit_active = True
                        self.state = "LIMITS_PLACED"
                        logger.info(
                            "Straddle entered at mark prices! Call=%.2f Put=%.2f Qty=%.4f | OCO: Short@$%.2f Long@$%.2f",
                            call_mark, put_mark, qty, self.short_limit_price, self.long_limit_price
                        )

                if self.state == "LIMITS_PLACED" and self.active_session_id and now_rel >= cutoff_rel:
                    self.is_short_limit_active = self.is_long_limit_active = False
                    for order in db.query(StraddleTradeOrder).filter_by(session_id=self.active_session_id, status="PENDING").all():
                        order.status = "EXPIRED"
                        order.cancel_reason = "FUTURES_ENTRY_CUTOFF"
                    db.commit()
                    self.state = "RECOVERY"

                # Monitor OCO Limits
                if self.state == "LIMITS_PLACED" and self.active_session_id:
                    tp_multiplier = float(cfg.get("FUTURES_TP_MULTIPLIER", "2"))
                    
                    # Check if either limit is triggered (use futures_mark for futures market)
                    if futures_mark >= self.short_limit_price and self.is_short_limit_active:
                        # Short Limit triggered: Fill Short futures order at limit price, Cancel Long Limit
                        self.is_long_limit_active = False
                        # Limit order fills at exact limit price, TP calculated from that
                        self.target_tp_price = self.short_limit_price - (self.combined_premium * tp_multiplier)
                        
                        sess = db.query(StraddleSession).filter(StraddleSession.id == self.active_session_id).first()
                        if sess:
                            sess.futures_entry_price = self.short_limit_price
                            sess.futures_tp_price = self.target_tp_price
                        
                        # Update Futures Orders in DB — fill at exact limit price
                        s_ord = db.query(StraddleTradeOrder).filter(
                            StraddleTradeOrder.session_id == self.active_session_id,
                            StraddleTradeOrder.leg_label == "SHORT_LIMIT"
                        ).first()
                        if s_ord:
                            s_ord.status = "FILLED"
                            s_ord.price = self.short_limit_price

                        l_ord = db.query(StraddleTradeOrder).filter(
                            StraddleTradeOrder.session_id == self.active_session_id,
                            StraddleTradeOrder.leg_label == "LONG_LIMIT"
                        ).first()
                        if l_ord:
                            l_ord.status = "CANCELLED"
                            l_ord.cancel_reason = "OCO_CANCELLED"

                        db.commit()
                        
                        self.state = "IN_TRADE"
                        logger.info("OCO Short Limit triggered at $%.2f! Target TP set at $%.2f", self.short_limit_price, self.target_tp_price)
                        
                    elif futures_mark <= self.long_limit_price and self.is_long_limit_active:
                        # Long Limit triggered: Fill Long futures order at limit price, Cancel Short Limit
                        self.is_short_limit_active = False
                        # Limit order fills at exact limit price, TP calculated from that
                        self.target_tp_price = self.long_limit_price + (self.combined_premium * tp_multiplier)
                        
                        sess = db.query(StraddleSession).filter(StraddleSession.id == self.active_session_id).first()
                        if sess:
                            sess.futures_entry_price = self.long_limit_price
                            sess.futures_tp_price = self.target_tp_price

                        # Update Futures Orders in DB — fill at exact limit price
                        l_ord = db.query(StraddleTradeOrder).filter(
                            StraddleTradeOrder.session_id == self.active_session_id,
                            StraddleTradeOrder.leg_label == "LONG_LIMIT"
                        ).first()
                        if l_ord:
                            l_ord.status = "FILLED"
                            l_ord.price = self.long_limit_price

                        s_ord = db.query(StraddleTradeOrder).filter(
                            StraddleTradeOrder.session_id == self.active_session_id,
                            StraddleTradeOrder.leg_label == "SHORT_LIMIT"
                        ).first()
                        if s_ord:
                            s_ord.status = "CANCELLED"
                            s_ord.cancel_reason = "OCO_CANCELLED"

                        db.commit()
                        
                        self.state = "IN_TRADE"
                        logger.info("OCO Long Limit triggered at $%.2f! Target TP set at $%.2f", self.long_limit_price, self.target_tp_price)
                        
                    # Check if Cutoff time reached without triggers
                    elif now_rel >= cutoff_rel:
                        self.is_short_limit_active = False
                        self.is_long_limit_active = False
                        self.state = "RECOVERY"

                        # Expire untriggered pending futures limit orders
                        pending_orders = db.query(StraddleTradeOrder).filter(
                            StraddleTradeOrder.session_id == self.active_session_id,
                            StraddleTradeOrder.status == "PENDING"
                        ).all()
                        for p_ord in pending_orders:
                            p_ord.status = "EXPIRED"
                        db.commit()

                        logger.info("OCO Limits expired at cutoff (%s). Entering Premium Recovery mode.", cutoff_time)

                # Monitor In Trade TP Target
                if self.state == "IN_TRADE" and self.active_session_id:
                    sess = db.query(StraddleSession).filter(StraddleSession.id == self.active_session_id).first()
                    if sess and sess.futures_tp_price:
                        # Verify if Futures TP has been hit
                        tp_hit = False
                        if sess.futures_entry_price and sess.futures_tp_price < sess.futures_entry_price:
                            # Short position: TP hits when futures_mark drops to or below target
                            if futures_mark <= sess.futures_tp_price:
                                tp_hit = True
                        elif sess.futures_entry_price and sess.futures_tp_price > sess.futures_entry_price:
                            # Long position: TP hits when futures_mark rises to or above target
                            if futures_mark >= sess.futures_tp_price:
                                tp_hit = True
                                
                        if tp_hit:
                            # Close positions, calculate profits
                            sess.exit_reason = "Futures TP Hit"
                            sess.status = "Completed"
                            sess.opt_call_close_price = self.active_call_mark
                            sess.opt_put_close_price = self.active_put_mark
                            sess.futures_exit_price = futures_mark if sess.futures_entry_price else 0.0
                            qty_val = self.session_qty(db, sess.id)
                            rec_call = self.active_call_mark
                            rec_put = self.active_put_mark

                            # Options Realized PnL
                            options_pnl = ((rec_call + rec_put) - (sess.net_straddle_ask or self.combined_premium)) * qty_val

                            # Futures Realized PnL at TP Hit
                            futures_pnl = 0.0
                            if sess.futures_entry_price and sess.futures_entry_price > 0:
                                is_short = (sess.futures_tp_price and sess.futures_tp_price < sess.futures_entry_price)
                                if is_short:
                                    futures_pnl = (sess.futures_entry_price - futures_mark) * qty_val
                                else:
                                    futures_pnl = (futures_mark - sess.futures_entry_price) * qty_val

                            net_realized_pnl = round(options_pnl + futures_pnl, 2)
                            sess.pnl_realized = net_realized_pnl

                            # Credit option recovery + futures PnL back to virtual wallet
                            recovery_val = (rec_call + rec_put) * qty_val
                            net_wallet_credit = recovery_val + futures_pnl

                            wallet_item = db.query(StraddleConfig).filter(StraddleConfig.key == "PAPER_WALLET_USDT").first()
                            old_bal = float(wallet_item.value) if wallet_item else 100000.0
                            new_bal = old_bal + net_wallet_credit
                            if wallet_item:
                                wallet_item.value = str(new_bal)
                            else:
                                db.add(StraddleConfig(key="PAPER_WALLET_USDT", value=str(new_bal)))

                            ledger_entry = StraddleWalletLedger(
                                session_id=sess.id,
                                entry_type="TRADE_CLOSE",
                                amount=net_wallet_credit,
                                balance_after=new_bal,
                                detail=f"Session #{sess.id} TP target hit - Combined PnL: ${net_realized_pnl:.2f} (Options: ${options_pnl:.2f}, Futures: ${futures_pnl:.2f})"
                            )
                            db.add(ledger_entry)
                            
                            qty_val = self.session_qty(db, sess.id)
                            now_ist = datetime.now(ist).replace(tzinfo=None)
                            rec_call = self.active_call_mark
                            rec_put = self.active_put_mark
                            
                            call_ord = StraddleTradeOrder(
                                session_id=sess.id,
                                symbol=f"BTC-{sess.expiry_sym}-{int(sess.call_strike)}-C",
                                asset_type="OPTION",
                                side="SELL",
                                leg_label="CALL",
                                order_type="MARKET",
                                qty=qty_val,
                                price=rec_call,
                                status="FILLED",
                                created_at=now_ist
                            )
                            put_ord = StraddleTradeOrder(
                                session_id=sess.id,
                                symbol=f"BTC-{sess.expiry_sym}-{int(sess.put_strike)}-P",
                                asset_type="OPTION",
                                side="SELL",
                                leg_label="PUT",
                                order_type="MARKET",
                                qty=qty_val,
                                price=rec_put,
                                status="FILLED",
                                created_at=now_ist
                            )
                            db.add(call_ord)
                            db.add(put_ord)

                            # Log the futures CLOSE order (opposing side to close the open position)
                            futures_close_side = "BUY" if (sess.futures_tp_price and sess.futures_tp_price < sess.futures_entry_price) else "SELL"
                            futures_close_ord = StraddleTradeOrder(
                                session_id=sess.id,
                                symbol="BTC-USDT-FUTURES",
                                asset_type="FUTURES",
                                side=futures_close_side,
                                leg_label="FUTURES_CLOSE",
                                order_type="MARKET",
                                qty=qty_val,
                                price=futures_mark,
                                status="FILLED",
                                created_at=now_ist
                            )
                            db.add(futures_close_ord)

                            db.commit()
                            self.active_session_id = None
                            self.state = "COMPLETED"
                            logger.info("Futures TP Target hit at $%.2f! Closed all options.", spot)

                # Monitor Recovery State (80% premium threshold)
                if self.state == "RECOVERY" and self.active_session_id:
                    sess = db.query(StraddleSession).filter(StraddleSession.id == self.active_session_id).first()
                    if sess:
                        rec_call = self.active_call_mark
                        rec_put = self.active_put_mark
                        current_recovery_val = rec_call + rec_put
                        entry_premium = sess.net_straddle_ask or self.combined_premium
                        
                        recovery_threshold_pct = float(cfg.get("RECOVERY_THRESHOLD_PCT", "0.65"))
                        if current_recovery_val >= (recovery_threshold_pct * entry_premium):
                            sess.status = "Completed"
                            sess.opt_call_close_price = self.active_call_mark
                            sess.opt_put_close_price = self.active_put_mark
                            sess.futures_exit_price = futures_mark if sess.futures_entry_price else 0.0
                            sess.exit_reason = f"Recovery Target Hit (>= {int(recovery_threshold_pct * 100)}%)"
                            
                            qty_val = self.session_qty(db, sess.id)
                            recovery_payout = current_recovery_val * qty_val
                            entry_cost = entry_premium * qty_val
                            sess.pnl_realized = recovery_payout - entry_cost
                            
                            wallet_item = db.query(StraddleConfig).filter(StraddleConfig.key == "PAPER_WALLET_USDT").first()
                            old_bal = float(wallet_item.value) if wallet_item else 100000.0
                            new_bal = old_bal + recovery_payout
                            if wallet_item:
                                wallet_item.value = str(new_bal)
                            else:
                                db.add(StraddleConfig(key="PAPER_WALLET_USDT", value=str(new_bal)))
                                
                            ledger_entry = StraddleWalletLedger(
                                session_id=sess.id,
                                entry_type="TRADE_CLOSE",
                                amount=recovery_payout,
                                balance_after=new_bal,
                                detail=f"Session #{sess.id} Recovery Target Hit - Value credited: ${recovery_payout:.2f}"
                            )
                            db.add(ledger_entry)
                            
                            now_ist = datetime.now(ist).replace(tzinfo=None)
                            call_ord = StraddleTradeOrder(
                                session_id=sess.id,
                                symbol=f"BTC-{sess.expiry_sym}-{int(sess.call_strike)}-C",
                                asset_type="OPTION",
                                side="SELL",
                                leg_label="CALL",
                                order_type="MARKET",
                                qty=qty_val,
                                price=rec_call,
                                status="FILLED",
                                created_at=now_ist
                            )
                            put_ord = StraddleTradeOrder(
                                session_id=sess.id,
                                symbol=f"BTC-{sess.expiry_sym}-{int(sess.put_strike)}-P",
                                asset_type="OPTION",
                                side="SELL",
                                leg_label="PUT",
                                order_type="MARKET",
                                qty=qty_val,
                                price=rec_put,
                                status="FILLED",
                                created_at=now_ist
                            )
                            db.add(call_ord)
                            db.add(put_ord)
                            
                            db.commit()
                            self.active_session_id = None
                            self.state = "COMPLETED"
                            logger.info("Premium recovered to 80%%! Closed all options for Session #%s.", sess.id)

                # Hard Squareoff / Manual Squareoff
                if self.state == "SQUAREOFF" or (now_rel >= sq_end_rel and self.state in ["IN_TRADE", "ENTRY_WINDOW", "LIMITS_PLACED", "RECOVERY"]):
                    logger.info("Straddle Window Closed (%s). Executing squareoff & config flush...", sq_end)
                    if self.active_session_id:
                        sess = db.query(StraddleSession).filter(StraddleSession.id == self.active_session_id).first()
                        if sess and sess.status in ["Open", "OPEN", "MANUAL_SQUAREOFF"]:
                            sess.exit_reason = sess.exit_reason or ("Manual Emergency Squareoff" if self.state == "SQUAREOFF" else "Scheduled Squareoff")
                            sess.status = "Completed"
                            sess.opt_call_close_price = self.active_call_mark
                            sess.opt_put_close_price = self.active_put_mark
                            sess.futures_exit_price = futures_mark if sess.futures_entry_price else 0.0
                            qty_val = self.session_qty(db, sess.id)
                            rec_call = self.active_call_mark
                            rec_put  = self.active_put_mark
                            
                            # 1. Options Realized PnL
                            recovery_val = (rec_call + rec_put) * qty_val
                            entry_cost = (sess.net_straddle_ask or self.combined_premium) * qty_val
                            options_pnl = recovery_val - entry_cost

                            # 2. Futures Realized PnL (if position was opened)
                            futures_pnl = 0.0
                            if sess.futures_entry_price and sess.futures_entry_price > 0:
                                is_short = (sess.futures_tp_price and sess.futures_tp_price < sess.futures_entry_price)
                                if is_short:
                                    futures_pnl = (sess.futures_entry_price - futures_mark) * qty_val
                                else:
                                    futures_pnl = (futures_mark - sess.futures_entry_price) * qty_val

                            net_realized_pnl = round(options_pnl + futures_pnl, 2)
                            sess.pnl_realized = net_realized_pnl
                            
                            # Cancel any remaining pending limit orders
                            pending_orders = db.query(StraddleTradeOrder).filter(
                                StraddleTradeOrder.session_id == self.active_session_id,
                                StraddleTradeOrder.status == "PENDING"
                            ).all()
                            for p_ord in pending_orders:
                                p_ord.status = "CANCELLED"
                                p_ord.cancel_reason = "SQUAREOFF"

                            net_wallet_credit = recovery_val + futures_pnl
                            wallet_item = db.query(StraddleConfig).filter(StraddleConfig.key == "PAPER_WALLET_USDT").first()
                            old_bal = float(wallet_item.value) if wallet_item else 100000.0
                            new_bal = old_bal + net_wallet_credit
                            if wallet_item:
                                wallet_item.value = str(new_bal)
                            else:
                                db.add(StraddleConfig(key="PAPER_WALLET_USDT", value=str(new_bal)))

                            ledger_entry = StraddleWalletLedger(
                                session_id=sess.id,
                                entry_type="TRADE_CLOSE",
                                amount=net_wallet_credit,
                                balance_after=new_bal,
                                detail=f"Session #{sess.id} Squareoff closed - Combined PnL: ${net_realized_pnl:.2f} (Options: ${options_pnl:.2f}, Futures: ${futures_pnl:.2f})"
                            )
                            db.add(ledger_entry)
                            
                            qty_val = self.session_qty(db, sess.id)
                            now_ist = datetime.now(ist).replace(tzinfo=None)
                            call_ord = StraddleTradeOrder(
                                session_id=sess.id,
                                symbol=f"BTC-{sess.expiry_sym}-{int(sess.call_strike)}-C",
                                asset_type="OPTION",
                                side="SELL",
                                leg_label="CALL",
                                order_type="MARKET",
                                qty=qty_val,
                                price=rec_call,
                                status="FILLED",
                                created_at=now_ist
                            )
                            put_ord = StraddleTradeOrder(
                                session_id=sess.id,
                                symbol=f"BTC-{sess.expiry_sym}-{int(sess.put_strike)}-P",
                                asset_type="OPTION",
                                side="SELL",
                                leg_label="PUT",
                                order_type="MARKET",
                                qty=qty_val,
                                price=rec_put,
                                status="FILLED",
                                created_at=now_ist
                            )
                            db.add(call_ord)
                            db.add(put_ord)

                            # Log the futures CLOSE order only if a futures position was actually opened
                            if sess.futures_entry_price and sess.futures_entry_price > 0:
                                futures_close_side = "BUY" if (sess.futures_tp_price and sess.futures_tp_price < sess.futures_entry_price) else "SELL"
                                futures_close_ord = StraddleTradeOrder(
                                    session_id=sess.id,
                                    symbol="BTC-USDT-FUTURES",
                                    asset_type="FUTURES",
                                    side=futures_close_side,
                                    leg_label="FUTURES_CLOSE",
                                    order_type="MARKET",
                                    qty=qty_val,
                                    price=futures_mark,
                                    status="FILLED",
                                    created_at=now_ist
                                )
                                db.add(futures_close_ord)

                        db.commit()
                        self.active_session_id = None
                        
                    self.state = "COMPLETED"
                    self.flush_pending_config_on_window_close(db)
                    self.state = "IDLE"

                db.close()
            except Exception as e:
                db.rollback()
                logger.error("Error in Straddle Engine loop: %s", str(e), exc_info=True)
            finally:
                db.close()

            await asyncio.sleep(2.0)

    def start(self):
        if not self.is_running:
            self._task = asyncio.create_task(self.run_loop())

    def stop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()

straddle_engine = StraddleEngine()
