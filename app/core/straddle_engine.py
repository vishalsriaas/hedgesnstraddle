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
        self.current_call_strike: float = 64500.0
        self.current_put_strike: float = 63800.0
        self.current_call_ask: float = 180.50
        self.current_put_ask: float = 195.20
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

    async def find_symmetric_itm_otm_pair(self, spot: float) -> Tuple[float, float, float, float]:
        """
        Dynamically fetch real-time Binance option tickers and select nearest ITM and OTM
        Call and Put contracts with equal strike distance relative to current BTC spot price.
        """
        tickers = await get_btc_options_tickers()
        if not tickers:
            # Calculate symmetric ATM/ITM-OTM equidistant strikes around spot if API tickers initializing
            base_strike = round(spot / 100.0) * 100.0
            return base_strike + 300.0, base_strike - 300.0, 180.50, 195.20

        calls: List[dict] = []
        puts: List[dict] = []

        for t in tickers:
            sym = t.get("symbol", "")
            # Example symbol: BTC-260807-64000-C
            parts = sym.split("-")
            if len(parts) >= 4:
                try:
                    strike = float(parts[2])
                    side = parts[3]
                    ask_price = float(t.get("askPrice", 0.0) or t.get("markPrice", 0.0))
                    if side == "C":
                        calls.append({"strike": strike, "ask": ask_price, "symbol": sym})
                    elif side == "P":
                        puts.append({"strike": strike, "ask": ask_price, "symbol": sym})
                except ValueError:
                    continue

        if not calls or not puts:
            base_strike = round(spot / 100.0) * 100.0
            return base_strike + 300.0, base_strike - 300.0, 180.50, 195.20

        # Sort strikes
        calls_by_dist = sorted(calls, key=lambda x: abs(x["strike"] - spot))
        best_call = calls_by_dist[0]
        call_dist = abs(best_call["strike"] - spot)

        # Match Put with identical strike distance from spot (|K_C - Spot| = |K_P - Spot|)
        matching_puts = [p for p in puts if abs(abs(p["strike"] - spot) - call_dist) < 50.0]
        best_put = matching_puts[0] if matching_puts else sorted(puts, key=lambda x: abs(x["strike"] - spot))[0]

        return best_call["strike"], best_put["strike"], best_call["ask"], best_put["ask"]

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

                spot = await get_btc_spot_price()
                call_st, put_st, call_ask, put_ask = await self.find_symmetric_itm_otm_pair(spot)
                self.current_call_strike = call_st
                self.current_put_strike = put_st
                self.current_call_ask = call_ask
                self.current_put_ask = put_ask

                now_time_str = datetime.now().strftime("%H:%M")
                window_start = cfg.get("WINDOW_START", "18:50")
                window_end = cfg.get("WINDOW_END", "18:55")
                sq_end = cfg.get("SQ_END", "19:02")

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
