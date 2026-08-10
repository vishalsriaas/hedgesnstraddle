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
        
        window_start = cfg.get("WINDOW_START", "18:50")
        window_end = cfg.get("WINDOW_END", "18:55")
        futures_entry_cutoff = cfg.get("FUTURES_ENTRY_CUTOFF", "18:56")
        sq_end = cfg.get("SQ_END", "19:02")
        max_premium_limit = float(cfg.get("MAX_TOTAL_MARK", "1500.0"))
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
                
                window_start = cfg.get("WINDOW_START", "18:50")
                window_end = cfg.get("WINDOW_END", "18:55")
                cutoff_time = cfg.get("FUTURES_ENTRY_CUTOFF", "18:56")
                sq_end = cfg.get("SQ_END", "19:02")
                
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

                # Handle state transitions and simulated trade punching
                if w_start_rel <= now_rel <= w_end_rel and self.state in ["IDLE", "SQUAREOFF", "COMPLETED"]:
                    self.state = "ENTRY_WINDOW"
                    logger.info("Entering Straddle Entry Window (%s - %s)", window_start, window_end)

                # Punch straddle entry if all conditions met
                if self.state == "ENTRY_WINDOW" and not self.active_session_id:
                    max_premium_limit = float(cfg.get("MAX_TOTAL_MARK", "1500.0"))
                    max_gap_limit = float(cfg.get("MAX_PREMIUM_GAP", "150.0"))
                    
                    premium_ok = (self.combined_premium <= max_premium_limit)
                    gap_ok = (abs(call_ask - put_ask) <= max_gap_limit)
                    
                    last_traded = cfg.get("LAST_TRADED_EXPIRY", "")
                    already_traded = (expiry != "N/A" and last_traded == expiry)

                    if premium_ok and gap_ok and not is_weekend_session and not already_traded:
                        # 1. Create a Straddle Session in database
                        new_sess = StraddleSession(
                            expiry_sym=expiry,
                            expiry_dt=expiry,
                            status="Open",
                            btc_entry_spot=spot,
                            call_strike=strike,
                            call_ask=call_ask,
                            put_strike=strike,
                            put_ask=put_ask,
                            net_straddle_ask=self.combined_premium,
                            pnl_realized=0.0
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
                        
                        # 2. Add simulated BUY fill records for Call and Put options
                        qty = float(cfg.get("TRADE_QTY", "0.1"))
                        call_ord = StraddleTradeOrder(
                            session_id=new_sess.id,
                            symbol=f"BTC-{expiry}-{int(strike)}-C",
                            asset_type="OPTION",
                            side="BUY",
                            leg_label="CALL",
                            order_type="MARKET",
                            qty=qty,
                            price=call_ask,
                            status="FILLED"
                        )
                        put_ord = StraddleTradeOrder(
                            session_id=new_sess.id,
                            symbol=f"BTC-{expiry}-{int(strike)}-P",
                            asset_type="OPTION",
                            side="BUY",
                            leg_label="PUT",
                            order_type="MARKET",
                            qty=qty,
                            price=put_ask,
                            status="FILLED"
                        )
                        db.add(call_ord)
                        db.add(put_ord)
                        db.commit()
                        
                        # Deduct premium cost from simulated margin account
                        total_cost = self.combined_premium * qty
                        wallet_item = db.query(StraddleConfig).filter(StraddleConfig.key == "PAPER_WALLET_USDT").first()
                        if wallet_item:
                            wallet_item.value = str(float(wallet_item.value) - total_cost)
                        db.commit()
                        
                        # Set limits state active
                        self.is_short_limit_active = True
                        self.is_long_limit_active = True
                        self.state = "LIMITS_PLACED"
                        logger.info("Straddle entered! Placed OCO Limits: Short @ $%.2f, Long @ $%.2f", self.short_limit_price, self.long_limit_price)

                # Monitor OCO Limits
                if self.state == "LIMITS_PLACED" and self.active_session_id:
                    tp_multiplier = float(cfg.get("FUTURES_TP_MULTIPLIER", "1.0"))
                    
                    # Check if either limit is triggered
                    if spot >= self.short_limit_price and self.is_short_limit_active:
                        # Short Limit triggered: Cancel Long Limit, fill Short futures order
                        self.is_long_limit_active = False
                        self.target_tp_price = spot - (self.combined_premium * tp_multiplier)
                        
                        sess = db.query(StraddleSession).filter(StraddleSession.id == self.active_session_id).first()
                        if sess:
                            sess.futures_entry_price = spot
                            sess.futures_tp_price = self.target_tp_price
                            sess.futures_status = "Open"
                        db.commit()
                        
                        self.state = "IN_TRADE"
                        logger.info("OCO Short Limit triggered at $%.2f! Target TP set at $%.2f", spot, self.target_tp_price)
                        
                    elif spot <= self.long_limit_price and self.is_long_limit_active:
                        # Long Limit triggered: Cancel Short Limit, fill Long futures order
                        self.is_short_limit_active = False
                        self.target_tp_price = spot + (self.combined_premium * tp_multiplier)
                        
                        sess = db.query(StraddleSession).filter(StraddleSession.id == self.active_session_id).first()
                        if sess:
                            sess.futures_entry_price = spot
                            sess.futures_tp_price = self.target_tp_price
                            sess.futures_status = "Open"
                        db.commit()
                        
                        self.state = "IN_TRADE"
                        logger.info("OCO Long Limit triggered at $%.2f! Target TP set at $%.2f", spot, self.target_tp_price)
                        
                    # Check if Cutoff time reached without triggers
                    elif now_rel >= cutoff_rel:
                        self.is_short_limit_active = False
                        self.is_long_limit_active = False
                        self.state = "RECOVERY"
                        logger.info("OCO Limits expired at cutoff (%s). Entering Premium Recovery mode.", cutoff_time)

                # Monitor In Trade TP Target
                if self.state == "IN_TRADE" and self.active_session_id:
                    sess = db.query(StraddleSession).filter(StraddleSession.id == self.active_session_id).first()
                    if sess and sess.futures_tp_price:
                        # Verify if Futures TP has been hit
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
                            # Close positions, calculate profits
                            sess.status = "Completed"
                            sess.futures_status = "Closed"
                            # Calculate dynamic multiplier payout profit
                            payout = (self.combined_premium * tp_multiplier) * float(cfg.get("TRADE_QTY", "0.1"))
                            sess.pnl_realized = payout
                            
                            # Credit payout back to virtual margin wallet
                            wallet_item = db.query(StraddleConfig).filter(StraddleConfig.key == "PAPER_WALLET_USDT").first()
                            if wallet_item:
                                wallet_item.value = str(float(wallet_item.value) + payout)
                            
                            db.commit()
                            self.active_session_id = None
                            self.state = "COMPLETED"
                            logger.info("Futures TP Target hit at $%.2f! Closed all options.", spot)

                # Hard Squareoff
                if now_rel >= sq_end_rel and self.state in ["IN_TRADE", "SQUAREOFF", "ENTRY_WINDOW", "LIMITS_PLACED", "RECOVERY"]:
                    logger.info("Straddle Window Closed (%s). Executing squareoff & config flush...", sq_end)
                    if self.active_session_id:
                        sess = db.query(StraddleSession).filter(StraddleSession.id == self.active_session_id).first()
                        if sess:
                            sess.status = "Completed"
                            sess.futures_status = "Closed"
                            # Simulated recovery: close options back at current asks (recovery)
                            recovery_val = (self.current_call_mark + self.current_put_mark) * float(cfg.get("TRADE_QTY", "0.1"))
                            sess.pnl_realized = - (self.combined_premium * float(cfg.get("TRADE_QTY", "0.1"))) + recovery_val
                            
                            wallet_item = db.query(StraddleConfig).filter(StraddleConfig.key == "PAPER_WALLET_USDT").first()
                            if wallet_item:
                                wallet_item.value = str(float(wallet_item.value) + recovery_val)
                        db.commit()
                        self.active_session_id = None
                        
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
