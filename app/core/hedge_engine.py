import asyncio
import logging
from typing import Dict, Any, Optional, Tuple
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models.schema import (
    HedgeConfig, HedgeStrategyConfig, PendingConfig, ConfigAuditLog, HedgeSession, 
    HedgeTradeOrder, HedgeFill
)
from app.core.binance_client import get_btc_spot_price, get_btc_futures_mark_price, get_btc_options_tickers

logger = logging.getLogger("hedgesnstraddle.hedge_engine")

class HedgeEngine:
    def __init__(self):
        self.is_running = False
        self.active_session_id: Optional[int] = None
        self.state = "IDLE"
        self.active_role = "1st Trader"
        self._task: Optional[asyncio.Task] = None

    def load_config(self, db: Session) -> Dict[str, str]:
        configs = db.query(HedgeConfig).all()
        return {c.key: c.value for c in configs}

    def get_role_strategy_config(self, db: Session, role_name: str) -> Optional[HedgeStrategyConfig]:
        """Fetch dynamic Hedge Strategy Config parameters by role name ('1st Trader' vs '2nd Trader')."""
        return db.query(HedgeStrategyConfig).filter(HedgeStrategyConfig.strategy_name == role_name).first()

    def validate_option_spend(self, option_ask: float, qty: float, max_option_spend: float) -> bool:
        """Enforces MAX_OPTION_SPEND limit: reject option purchase if (ask * qty) > MAX_OPTION_SPEND."""
        total_cost = option_ask * qty
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

                # Determine active role dynamically based on active session state
                open_session = db.query(HedgeSession).filter(HedgeSession.status == "Open").first()
                if open_session:
                    self.active_role = "2nd Trader"
                else:
                    self.active_role = "1st Trader"

                role_config = self.get_role_strategy_config(db, self.active_role)
                if role_config:
                    # All parameters dynamically evaluated from UI/DB configured values
                    max_premium = role_config.max_premium
                    max_time_val = role_config.max_time_value
                    contract_qty = role_config.contract_qty
                    trade_window_open = f"{role_config.trade_start_h:02d}:{role_config.trade_start_m:02d}"
                    trade_window_close = f"{role_config.trade_end_h:02d}:{role_config.trade_end_m:02d}"
                    force_close = f"{role_config.force_close_h:02d}:{role_config.force_close_m:02d}"
                    
                    max_option_spend = float(cfg.get("MAX_OPTION_SPEND", "400.0"))

                    logger.debug("Hedge Role [%s] limits: Window %s-%s, ForceClose %s, Qty %.2f, MaxPrem $%.1f, MaxTV $%.1f, MaxSpend $%.1f",
                                 self.active_role, trade_window_open, trade_window_close, force_close, contract_qty, max_premium, max_time_val, max_option_spend)

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
