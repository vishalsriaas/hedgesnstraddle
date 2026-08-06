import asyncio
import logging
from typing import Dict, Any, Optional
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models.schema import (
    HedgeConfig, PendingConfig, ConfigAuditLog, HedgeSession, 
    HedgeTradeOrder, HedgeFill
)

logger = logging.getLogger("hedgesnstraddle.hedge_engine")

class HedgeEngine:
    def __init__(self):
        self.is_running = False
        self.active_session_id: Optional[int] = None
        self.state = "IDLE"
        self._task: Optional[asyncio.Task] = None

    def load_config(self, db: Session) -> Dict[str, str]:
        configs = db.query(HedgeConfig).all()
        return {c.key: c.value for c in configs}

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
