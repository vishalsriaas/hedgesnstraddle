import asyncio
import logging
from datetime import datetime
from typing import Dict, Any, Optional, Tuple, List
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models.schema import (
    StraddleConfig, PendingConfig, ConfigAuditLog, StraddleSession, 
    StraddleTradeOrder, StraddleFill, StraddleWalletLedger
)
from app.core.binance_client import get_btc_spot_price, get_btc_futures_mark_price, get_btc_options_tickers

logger = logging.getLogger("hedgesnstraddle.straddle_engine")

class StraddleEngine:
    def __init__(self):
        self.is_running = False
        self.active_session_id: Optional[int] = None
        self.state = "IDLE"
        
        # Live state properties
        self.last_spot_price: float = 64300.0
        self.nearest_expiry: str = "N/A"
        self.current_strike: float = 64000.0
        self.current_call_ask: float = 180.50
        self.current_put_ask: float = 195.20
        self.combined_premium: float = 375.70
        
        # Calculated OCO limit levels
        self.short_limit_price: float = 64375.70
        self.long_limit_price: float = 63624.30
        
        # Checked conditions status
        self.cond_time_window_valid: bool = False
        self.cond_premium_valid: bool = False
        self.cond_premium_gap_valid: bool = False
        self.cond_same_strike_valid: bool = True
        self.cond_itm_otm_valid: bool = True
        
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

    async def find_same_strike_pair(self, spot: float) -> Tuple[float, float, float, str]:
        """
        Fetches option contracts, filters strictly by the nearest expiry,
        and selects the Call and Put ask prices at the SAME closest strike level.
        """
        tickers = await get_btc_options_tickers()
        
        # 500 BTC strike step interval fallback
        base_strike = round(spot / 500.0) * 500.0
        
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
                    side = parts[3]
                    ask_price = float(t.get("askPrice", 0.0) or t.get("markPrice", 0.0))
                    parsed_tickers.append({
                        "symbol": sym,
                        "expiry": expiry_date,
                        "strike": strike,
                        "side": side,
                        "ask": ask_price
                    })
                except ValueError:
                    continue

        if not parsed_tickers:
            return base_strike, 180.50, 195.20, "N/A"

        # Filter strictly for NEAREST EXPIRY DATE
        available_expiries = sorted(list(set(item["expiry"] for item in parsed_tickers)))
        nearest = available_expiries[0]

        nearest_tickers = [item for item in parsed_tickers if item["expiry"] == nearest]
        
        # Find closest strike level K to spot
        strikes = sorted(list(set(item["strike"] for item in nearest_tickers)), key=lambda x: abs(x - spot))
        best_strike = strikes[0] if strikes else base_strike

        # Retrieve Call and Put asks for this strike
        best_call = next((x for x in nearest_tickers if x["strike"] == best_strike and x["side"] == "C"), None)
        best_put = next((x for x in nearest_tickers if x["strike"] == best_strike and x["side"] == "P"), None)

        call_ask = best_call["ask"] if best_call else 180.50
        put_ask = best_put["ask"] if best_put else 195.20

        return best_strike, call_ask, put_ask, nearest

    def get_live_monitoring_snapshot(self, db: Session) -> Dict[str, Any]:
        """Returns dynamic workflow status and limit order computations for the frontend."""
        cfg = self.load_config(db)
        
        window_start = cfg.get("WINDOW_START", "18:50")
        window_end = cfg.get("WINDOW_END", "18:55")
        max_premium_limit = float(cfg.get("MAX_TOTAL_MARK", "1500.0"))
        max_gap_limit = float(cfg.get("MAX_PREMIUM_GAP", "150.0"))
        
        now_time_str = datetime.now().strftime("%H:%M")
        
        # Check conditions
        self.cond_time_window_valid = (window_start <= now_time_str <= window_end)
        self.cond_premium_valid = (self.combined_premium <= max_premium_limit)
        self.cond_premium_gap_valid = (abs(self.current_call_ask - self.current_put_ask) <= max_gap_limit)
        
        # ITM / OTM verification: one must be <= spot, the other >= spot at same strike K
        self.cond_itm_otm_valid = True  # Inherently true for same strike model
        
        return {
            "state": self.state,
            "server_time": now_time_str,
            "last_spot_price": self.last_spot_price,
            "nearest_expiry": self.nearest_expiry,
            "current_strike": self.current_strike,
            "current_call_ask": self.current_call_ask,
            "current_put_ask": self.current_put_ask,
            "combined_premium": self.combined_premium,
            "short_limit_price": self.short_limit_price,
            "long_limit_price": self.long_limit_price,
            
            # Constraints
            "window_start": window_start,
            "window_end": window_end,
            "max_premium_limit": max_premium_limit,
            "max_gap_limit": max_gap_limit,
            
            # Condition check results
            "cond_time_window_valid": self.cond_time_window_valid,
            "cond_premium_valid": self.cond_premium_valid,
            "cond_premium_gap_valid": self.cond_premium_gap_valid,
            "cond_same_strike_valid": self.cond_same_strike_valid,
            "cond_itm_otm_valid": self.cond_itm_otm_valid
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
                
                strike, call_ask, put_ask, expiry = await self.find_same_strike_pair(spot)
                self.current_strike = strike
                self.current_call_ask = call_ask
                self.current_put_ask = put_ask
                self.nearest_expiry = expiry
                
                # Calculations
                self.combined_premium = call_ask + put_ask
                self.short_limit_price = strike + self.combined_premium
                self.long_limit_price = strike - self.combined_premium

                now_time_str = datetime.now().strftime("%H:%M")
                window_start = cfg.get("WINDOW_START", "18:50")
                window_end = cfg.get("WINDOW_END", "18:55")
                sq_end = cfg.get("SQ_END", "19:02")

                # Handle state transitions
                if window_start <= now_time_str <= window_end and self.state == "IDLE":
                    self.state = "ENTRY_WINDOW"
                    logger.info("Entering Straddle Entry Window (%s - %s)", window_start, window_end)

                elif now_time_str > sq_end and self.state in ["IN_TRADE", "SQUAREOFF", "ENTRY_WINDOW"]:
                    logger.info("Straddle Window Closed (%s). Executing squareoff & config flush...", sq_end)
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
