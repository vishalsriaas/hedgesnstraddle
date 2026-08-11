import asyncio
import logging
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
        self.last_spot_price: float = 64300.0
        self.last_futures_mark: float = 64300.0
        self.nearest_expiry: str = "N/A"
        self.current_strike: float = 64000.0
        self.current_call_mark: float = 180.50
        self.current_put_mark: float = 195.20
        self.combined_premium: float = 375.70
        
        # Calculated OCO limit levels
        self.short_limit_price: float = 64375.70
        self.long_limit_price: float = 63624.30
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

        # Fallback: round futures mark to nearest 500 BTC step
        base_strike = round(futures_mark / 500.0) * 500.0

        if not tickers:
            return base_strike, 180.50, 195.20, "N/A"

        parsed_tickers = []
        for t in tickers:
            sym = t.get("symbol", "")
            parts = sym.split("-")
            if len(parts) >= 4:
                try:
                    expiry_date = parts[1]
                    strike = float(parts[2])
                    side = parts[3]          # "C" = Call, "P" = Put
                    mark_price = float(t.get("markPrice") or 0.0)
                    parsed_tickers.append({
                        "symbol": sym,
                        "expiry": expiry_date,
                        "strike": strike,
                        "side": side,
                        "mark": mark_price
                    })
                except ValueError:
                    continue

        if not parsed_tickers:
            return base_strike, 180.50, 195.20, "N/A"

        # Filter strictly for NEAREST EXPIRY DATE
        available_expiries = sorted(list(set(item["expiry"] for item in parsed_tickers)))
        nearest = available_expiries[0]
        nearest_tickers = [item for item in parsed_tickers if item["expiry"] == nearest]

        # Find closest strike to FUTURES MARK PRICE
        #   ATM  → strike ≈ futures_mark
        #   Call ITM → strike < futures_mark  |  Call OTM → strike > futures_mark
        #   Put  ITM → strike > futures_mark  |  Put  OTM → strike < futures_mark
        strikes = sorted(
            list(set(item["strike"] for item in nearest_tickers)),
            key=lambda k: abs(k - futures_mark)   # ← reference is futures mark
        )
        best_strike = strikes[0] if strikes else base_strike

        # Retrieve Call and Put marks at this strike
        best_call = next((x for x in nearest_tickers if x["strike"] == best_strike and x["side"] == "C"), None)
        best_put  = next((x for x in nearest_tickers if x["strike"] == best_strike and x["side"] == "P"), None)

        call_mark = best_call["mark"] if best_call else 180.50
        put_mark  = best_put["mark"]  if best_put  else 195.20

        return best_strike, call_mark, put_mark, nearest

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
            try:
                db = SessionLocal()
                cfg = self.load_config(db)
                bot_enabled = cfg.get("BOT_ENABLED", "1") == "1"

                if not bot_enabled:
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

                strike, call_mark, put_mark, expiry = await self.find_same_strike_pair(futures_mark)
                self.current_strike = strike
                self.current_call_mark = call_mark
                self.current_put_mark = put_mark
                self.nearest_expiry = expiry
                
                # Calculations
                self.combined_premium = call_mark + put_mark
                self.short_limit_price = strike + self.combined_premium
                self.long_limit_price = strike - self.combined_premium

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

                # Restore active session from DB if not loaded in memory (e.g. after server restart)
                if not self.active_session_id:
                    open_sess = db.query(StraddleSession).filter(StraddleSession.status == "Open").order_by(StraddleSession.id.desc()).first()
                    if open_sess:
                        self.active_session_id = open_sess.id

                # Handle state transitions and simulated trade punching
                if w_start_rel <= now_rel <= w_end_rel and self.state in ["IDLE", "SQUAREOFF", "COMPLETED"]:
                    self.state = "ENTRY_WINDOW"
                    logger.info("Entering Straddle Entry Window (%s - %s)", window_start, window_end)

                # Punch straddle entry if all conditions met
                if self.state == "ENTRY_WINDOW" and not self.active_session_id:
                    max_premium_limit = float(cfg.get("MAX_TOTAL_MARK", "400.0"))
                    max_gap_limit = float(cfg.get("MAX_PREMIUM_GAP", "150.0"))

                    premium_ok = (self.combined_premium <= max_premium_limit)
                    gap_ok = (abs(call_mark - put_mark) <= max_gap_limit)

                    last_traded = cfg.get("LAST_TRADED_EXPIRY", "")
                    db_existing_sess = db.query(StraddleSession).filter(StraddleSession.expiry_sym == expiry).first() if expiry != "N/A" else None
                    already_traded = (expiry != "N/A" and (last_traded == expiry or db_existing_sess is not None))

                    if premium_ok and gap_ok and not is_weekend_session and not already_traded:
                        qty = float(cfg.get("TRADE_QTY", "10"))

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
                        db.commit()
                        db.refresh(new_sess)
                        self.active_session_id = new_sess.id

                        # Save last traded expiry to prevent duplicate session triggers
                        last_traded_cfg = db.query(StraddleConfig).filter(StraddleConfig.key == "LAST_TRADED_EXPIRY").first()
                        if last_traded_cfg:
                            last_traded_cfg.value = expiry
                        else:
                            db.add(StraddleConfig(key="LAST_TRADED_EXPIRY", value=expiry))
                        db.commit()

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
                        db.commit()

                        # 4. Deduct option entry premium cost from virtual margin account & record wallet ledger
                        total_cost = self.combined_premium * qty
                        wallet_item = db.query(StraddleConfig).filter(StraddleConfig.key == "PAPER_WALLET_USDT").first()
                        old_balance = float(wallet_item.value) if wallet_item else 100000.0
                        new_balance = old_balance - total_cost
                        if wallet_item:
                            wallet_item.value = str(new_balance)

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

                # Monitor OCO Limits
                if self.state == "LIMITS_PLACED" and self.active_session_id:
                    tp_multiplier = float(cfg.get("FUTURES_TP_MULTIPLIER", "2"))
                    
                    # Check if either limit is triggered
                    if spot >= self.short_limit_price and self.is_short_limit_active:
                        # Short Limit triggered: Fill Short futures order, Cancel Long Limit
                        self.is_long_limit_active = False
                        self.target_tp_price = spot - (self.combined_premium * tp_multiplier)
                        
                        sess = db.query(StraddleSession).filter(StraddleSession.id == self.active_session_id).first()
                        if sess:
                            sess.futures_entry_price = spot
                            sess.futures_tp_price = self.target_tp_price
                            sess.futures_status = "Open"
                        
                        # Update Futures Orders in DB
                        s_ord = db.query(StraddleTradeOrder).filter(
                            StraddleTradeOrder.session_id == self.active_session_id,
                            StraddleTradeOrder.leg_label == "SHORT_LIMIT"
                        ).first()
                        if s_ord:
                            s_ord.status = "FILLED"
                            s_ord.price = spot

                        l_ord = db.query(StraddleTradeOrder).filter(
                            StraddleTradeOrder.session_id == self.active_session_id,
                            StraddleTradeOrder.leg_label == "LONG_LIMIT"
                        ).first()
                        if l_ord:
                            l_ord.status = "CANCELLED"
                            l_ord.cancel_reason = "OCO_CANCELLED"

                        db.commit()
                        
                        self.state = "IN_TRADE"
                        logger.info("OCO Short Limit triggered at $%.2f! Target TP set at $%.2f", spot, self.target_tp_price)
                        
                    elif spot <= self.long_limit_price and self.is_long_limit_active:
                        # Long Limit triggered: Fill Long futures order, Cancel Short Limit
                        self.is_short_limit_active = False
                        self.target_tp_price = spot + (self.combined_premium * tp_multiplier)
                        
                        sess = db.query(StraddleSession).filter(StraddleSession.id == self.active_session_id).first()
                        if sess:
                            sess.futures_entry_price = spot
                            sess.futures_tp_price = self.target_tp_price
                            sess.futures_status = "Open"

                        # Update Futures Orders in DB
                        l_ord = db.query(StraddleTradeOrder).filter(
                            StraddleTradeOrder.session_id == self.active_session_id,
                            StraddleTradeOrder.leg_label == "LONG_LIMIT"
                        ).first()
                        if l_ord:
                            l_ord.status = "FILLED"
                            l_ord.price = spot

                        s_ord = db.query(StraddleTradeOrder).filter(
                            StraddleTradeOrder.session_id == self.active_session_id,
                            StraddleTradeOrder.leg_label == "SHORT_LIMIT"
                        ).first()
                        if s_ord:
                            s_ord.status = "CANCELLED"
                            s_ord.cancel_reason = "OCO_CANCELLED"

                        db.commit()
                        
                        self.state = "IN_TRADE"
                        logger.info("OCO Long Limit triggered at $%.2f! Target TP set at $%.2f", spot, self.target_tp_price)
                        
                    # Check if Futures Cutoff time reached without triggers -> Enter RECOVERY mode
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
                            p_ord.cancel_reason = "FUTURES_CUTOFF_REACHED"
                        db.commit()

                        logger.info("Futures Cutoff (%s) reached. Pending limit orders expired. Options Recovery Window active.", cutoff_time)

                # Scenario 1 (Recovery Window Check): 80% Premium Recovery Target Check
                if self.state == "RECOVERY" and self.active_session_id:
                    sess = db.query(StraddleSession).filter(StraddleSession.id == self.active_session_id).first()
                    if sess and sess.status == "Open":
                        qty = float(cfg.get("TRADE_QTY", "10"))
                        now_ist = datetime.now(ist).replace(tzinfo=None)
                        entry_combined = sess.call_ask + sess.put_ask
                        live_combined = self.current_call_mark + self.current_put_mark
                        recovery_target_80 = 0.80 * entry_combined

                        # If combined premium recovers to >= 80% of entry premium, sell both options immediately
                        if live_combined >= recovery_target_80 and live_combined > 0:
                            logger.info("Session #%d: 80%% Premium Recovery Target hit ($%.2f >= $%.2f). Selling options...", sess.id, live_combined, recovery_target_80)
                            
                            call_sell = StraddleTradeOrder(
                                session_id=sess.id,
                                symbol=f"BTC-{sess.expiry_sym}-{int(sess.call_strike)}-C",
                                asset_type="OPTION",
                                side="SELL",
                                leg_label="CALL_EXIT",
                                order_type="MARKET",
                                qty=qty,
                                price=self.current_call_mark,
                                status="FILLED",
                                created_at=now_ist
                            )
                            put_sell = StraddleTradeOrder(
                                session_id=sess.id,
                                symbol=f"BTC-{sess.expiry_sym}-{int(sess.put_strike)}-P",
                                asset_type="OPTION",
                                side="SELL",
                                leg_label="PUT_EXIT",
                                order_type="MARKET",
                                qty=qty,
                                price=self.current_put_mark,
                                status="FILLED",
                                created_at=now_ist
                            )
                            db.add(call_sell)
                            db.add(put_sell)

                            recovery_proceeds = live_combined * qty
                            wallet_item = db.query(StraddleConfig).filter(StraddleConfig.key == "PAPER_WALLET_USDT").first()
                            old_bal = float(wallet_item.value) if wallet_item else 100000.0
                            new_bal = old_bal + recovery_proceeds
                            if wallet_item:
                                wallet_item.value = str(new_bal)

                            ledger_entry = StraddleWalletLedger(
                                session_id=sess.id,
                                entry_type="TRADE_CLOSE",
                                amount=recovery_proceeds,
                                balance_after=new_bal,
                                detail=f"Session #{sess.id} 80% Recovery Target hit ($%.2f >= 80% of entry $%.2f) - Sold Call & Put options." % (live_combined, entry_combined),
                                created_at=now_ist
                            )
                            db.add(ledger_entry)

                            sess.status = "Completed"
                            sess.pnl_realized = (live_combined - entry_combined) * qty
                            db.commit()

                            self.active_session_id = None
                            self.state = "COMPLETED"
                            self.current_strike = 0.0
                            self.current_call_mark = 0.0
                            self.current_put_mark = 0.0

                # Scenario 2 (In Trade Check): Monitor Futures Take Profit Target
                if self.state == "IN_TRADE" and self.active_session_id:
                    sess = db.query(StraddleSession).filter(StraddleSession.id == self.active_session_id).first()
                    if sess and sess.futures_tp_price:
                        tp_hit = False
                        if sess.futures_entry_price and sess.futures_tp_price < sess.futures_entry_price:
                            # Short position: TP hits when spot drops to or below target
                            if spot <= sess.futures_tp_price:
                                tp_hit = True
                        elif sess.futures_entry_price and sess.futures_tp_price > sess.futures_entry_price:
                            # Long position: TP hits when spot rises to or above target
                            if spot >= sess.futures_tp_price:
                                tp_hit = True
                                
                        if tp_hit:
                            qty = float(cfg.get("TRADE_QTY", "10"))
                            now_ist = datetime.now(ist).replace(tzinfo=None)
                            payout = (self.combined_premium * tp_multiplier) * qty
                            
                            # Add SELL exit orders for Call, Put, and Futures
                            call_sell = StraddleTradeOrder(
                                session_id=sess.id,
                                symbol=f"BTC-{sess.expiry_sym}-{int(sess.call_strike)}-C",
                                asset_type="OPTION",
                                side="SELL",
                                leg_label="CALL_EXIT",
                                order_type="MARKET",
                                qty=qty,
                                price=self.current_call_mark,
                                status="FILLED",
                                created_at=now_ist
                            )
                            put_sell = StraddleTradeOrder(
                                session_id=sess.id,
                                symbol=f"BTC-{sess.expiry_sym}-{int(sess.put_strike)}-P",
                                asset_type="OPTION",
                                side="SELL",
                                leg_label="PUT_EXIT",
                                order_type="MARKET",
                                qty=qty,
                                price=self.current_put_mark,
                                status="FILLED",
                                created_at=now_ist
                            )
                            fut_close = StraddleTradeOrder(
                                session_id=sess.id,
                                symbol="BTC-USDT-FUTURES",
                                asset_type="FUTURES",
                                side="SELL" if sess.futures_tp_price > sess.futures_entry_price else "BUY",
                                leg_label="FUTURES_TP_EXIT",
                                order_type="MARKET",
                                qty=qty,
                                price=spot,
                                status="FILLED",
                                created_at=now_ist
                            )
                            db.add(call_sell)
                            db.add(put_sell)
                            db.add(fut_close)

                            sess.status = "Completed"
                            sess.futures_status = "Closed"
                            sess.pnl_realized = payout
                            
                            wallet_item = db.query(StraddleConfig).filter(StraddleConfig.key == "PAPER_WALLET_USDT").first()
                            old_bal = float(wallet_item.value) if wallet_item else 100000.0
                            new_bal = old_bal + payout
                            if wallet_item:
                                wallet_item.value = str(new_bal)

                            ledger_entry = StraddleWalletLedger(
                                session_id=sess.id,
                                entry_type="TRADE_CLOSE",
                                amount=payout,
                                balance_after=new_bal,
                                detail=f"Session #{sess.id} Futures TP target hit at ${spot:.2f} - Closed Futures & Options.",
                                created_at=now_ist
                            )
                            db.add(ledger_entry)
                            db.commit()

                            self.active_session_id = None
                            self.state = "COMPLETED"
                            self.current_strike = 0.0
                            self.current_call_mark = 0.0
                            self.current_put_mark = 0.0
                            logger.info("Futures TP Target hit at $%.2f! Closed all positions.", spot)

                # Hard Squareoff at 12:30 PM (Universal Cutoff for both Scenario 1 & 2)
                if now_rel >= sq_end_rel and self.state in ["IN_TRADE", "SQUAREOFF", "ENTRY_WINDOW", "LIMITS_PLACED", "RECOVERY"]:
                    logger.info("Hard Squareoff Time (%s) reached. Force closing all open positions...", sq_end)
                    if self.active_session_id:
                        sess = db.query(StraddleSession).filter(StraddleSession.id == self.active_session_id).first()
                        if sess and sess.status == "Open":
                            qty = float(cfg.get("TRADE_QTY", "10"))
                            now_ist = datetime.now(ist).replace(tzinfo=None)
                            
                            # 1. Record SELL exit trade orders for Call & Put options
                            call_sell = StraddleTradeOrder(
                                session_id=sess.id,
                                symbol=f"BTC-{sess.expiry_sym}-{int(sess.call_strike)}-C",
                                asset_type="OPTION",
                                side="SELL",
                                leg_label="CALL_EXIT",
                                order_type="MARKET",
                                qty=qty,
                                price=self.current_call_mark,
                                status="FILLED",
                                created_at=now_ist
                            )
                            put_sell = StraddleTradeOrder(
                                session_id=sess.id,
                                symbol=f"BTC-{sess.expiry_sym}-{int(sess.put_strike)}-P",
                                asset_type="OPTION",
                                side="SELL",
                                leg_label="PUT_EXIT",
                                order_type="MARKET",
                                qty=qty,
                                price=self.current_put_mark,
                                status="FILLED",
                                created_at=now_ist
                            )
                            db.add(call_sell)
                            db.add(put_sell)

                            # 2. If futures position was active, close futures position & record order
                            fut_realized = 0.0
                            if sess.futures_status == "Open" and sess.futures_entry_price:
                                fut_side_dir = 1 if (sess.futures_tp_price > sess.futures_entry_price) else -1
                                fut_realized = fut_side_dir * (spot - sess.futures_entry_price) * qty
                                fut_close = StraddleTradeOrder(
                                    session_id=sess.id,
                                    symbol="BTC-USDT-FUTURES",
                                    asset_type="FUTURES",
                                    side="SELL" if fut_side_dir == 1 else "BUY",
                                    leg_label="FUTURES_SQ_EXIT",
                                    order_type="MARKET",
                                    qty=qty,
                                    price=spot,
                                    status="FILLED",
                                    created_at=now_ist
                                )
                                db.add(fut_close)

                            # 3. Cancel any untriggered pending limit orders
                            pending_orders = db.query(StraddleTradeOrder).filter(
                                StraddleTradeOrder.session_id == self.active_session_id,
                                StraddleTradeOrder.status == "PENDING"
                            ).all()
                            for p_ord in pending_orders:
                                p_ord.status = "CANCELLED"
                                p_ord.cancel_reason = "HARD_SQUAREOFF"

                            option_recovery = (self.current_call_mark + self.current_put_mark) * qty
                            total_close_credit = option_recovery + fut_realized

                            wallet_item = db.query(StraddleConfig).filter(StraddleConfig.key == "PAPER_WALLET_USDT").first()
                            old_bal = float(wallet_item.value) if wallet_item else 100000.0
                            new_bal = old_bal + total_close_credit
                            if wallet_item:
                                wallet_item.value = str(new_bal)

                            ledger_entry = StraddleWalletLedger(
                                session_id=sess.id,
                                entry_type="TRADE_CLOSE",
                                amount=total_close_credit,
                                balance_after=new_bal,
                                detail=f"Session #{sess.id} Hard Squareoff at {sq_end} - Option recovery ${option_recovery:.2f} + Futures PnL ${fut_realized:.2f}",
                                created_at=now_ist
                            )
                            db.add(ledger_entry)

                            sess.status = "Completed"
                            sess.futures_status = "Closed" if sess.futures_status == "Open" else sess.futures_status
                            sess.pnl_realized = (option_recovery + fut_realized) - (sess.net_straddle_ask * qty)
                            db.commit()

                        self.active_session_id = None
                        self.current_strike = 0.0
                        self.current_call_mark = 0.0
                        self.current_put_mark = 0.0
                        
                    self.state = "COMPLETED"
                    self.flush_pending_config_on_window_close(db)
                    self.state = "IDLE"

                db.close()
            except Exception as e:
                logger.error("Error in Straddle Engine loop: %s", str(e), exc_info=True)

            await asyncio.sleep(2.0)

    def start(self):
        if not self.is_running:
            self._task = asyncio.create_task(self.run_loop())

    def stop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()

straddle_engine = StraddleEngine()
