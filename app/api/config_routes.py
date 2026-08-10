from typing import Dict, Any, List
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.schema import User, StraddleConfig, HedgeConfig, HedgeStrategyConfig, PendingConfig, ConfigAuditLog
from app.core.auth import get_current_user, require_admin, get_client_ip
from app.core.straddle_engine import straddle_engine
from app.core.hedge_engine import hedge_engine

router = APIRouter(prefix="/api/v1/config", tags=["Configuration"])

@router.get("/straddle")
def get_straddle_config(db: Session = Depends(get_db)):
    configs = db.query(StraddleConfig).all()
    pending = db.query(PendingConfig).filter(PendingConfig.config_type == "STRADDLE").all()
    return {
        "active": {c.key: c.value for c in configs},
        "pending": {p.field_name: p.pending_value for p in pending}
    }

@router.post("/straddle")
def update_straddle_config(
    payload: Dict[str, Any], 
    request: Request, 
    db: Session = Depends(get_db), 
    current_user: User = Depends(require_admin)
):
    ip_addr = get_client_ip(request)
    window_active = straddle_engine.state in ["ENTRY_WINDOW", "IN_TRADE", "SQUAREOFF"]
    
    staged_results = []
    applied_results = []

    for field_name, new_value in payload.items():
        new_val_str = str(new_value)

        existing = db.query(StraddleConfig).filter(StraddleConfig.key == field_name).first()
        old_val_str = existing.value if existing else ""

        if old_val_str == new_val_str:
            continue

        if existing:
            existing.value = new_val_str
        else:
            db.add(StraddleConfig(key=field_name, value=new_val_str))

        audit = ConfigAuditLog(
            user_email=current_user.email,
            config_type="STRADDLE",
            field_name=field_name,
            old_value=old_val_str,
            new_value=new_val_str,
            apply_mode="IMMEDIATE",
            status="APPLIED",
            ip_address=ip_addr
        )
        db.add(audit)
        applied_results.append(field_name)

    # Clear LAST_TRADED_EXPIRY config value to allow immediate re-trade testing
    db.query(StraddleConfig).filter(StraddleConfig.key == "LAST_TRADED_EXPIRY").delete()
    db.commit()

    return {
        "status": "APPLIED",
        "message": "Configuration updated live immediately.",
        "applied_fields": applied_results
    }

@router.get("/hedge")
def get_hedge_config(db: Session = Depends(get_db)):
    configs = db.query(HedgeConfig).all()
    strategies = db.query(HedgeStrategyConfig).all()
    pending = db.query(PendingConfig).filter(PendingConfig.config_type == "HEDGE").all()
    return {
        "active": {c.key: c.value for c in configs},
        "strategies": [
            {
                "id": s.id,
                "strategy_name": s.strategy_name,
                "strategy_key": s.strategy_key,
                "enabled": s.enabled,
                "direction": s.direction,
                "trade_start": f"{s.trade_start_h:02d}:{s.trade_start_m:02d}",
                "trade_end": f"{s.trade_end_h:02d}:{s.trade_end_m:02d}",
                "force_close": f"{s.force_close_h:02d}:{s.force_close_m:02d}",
                "contract_qty": s.contract_qty,
                "max_premium": s.max_premium,
                "max_time_value": s.max_time_value,
                "price_diff_percent": s.price_diff_percent,
                "partial_profit_ratio": s.partial_profit_ratio,
                "partial_tp_multiplier": s.partial_tp_multiplier,
                "rebuy_mode": s.rebuy_mode
            }
            for s in strategies
        ],
        "pending": {p.field_name: p.pending_value for p in pending}
    }

@router.post("/hedge")
def update_hedge_config(
    payload: Dict[str, Any], 
    request: Request, 
    db: Session = Depends(get_db), 
    current_user: User = Depends(require_admin)
):
    ip_addr = get_client_ip(request)
    session_active = hedge_engine.state in ["RUNNING"]

    staged_results = []
    applied_results = []

    for field_name, new_value in payload.items():
        new_val_str = str(new_value)

        existing = db.query(HedgeConfig).filter(HedgeConfig.key == field_name).first()
        old_val_str = existing.value if existing else ""

        if old_val_str == new_val_str:
            continue

        if session_active:
            pending = db.query(PendingConfig).filter(
                PendingConfig.config_type == "HEDGE",
                PendingConfig.field_name == field_name
            ).first()

            if pending:
                pending.pending_value = new_val_str
                pending.user_email = current_user.email
            else:
                db.add(PendingConfig(
                    config_type="HEDGE",
                    field_name=field_name,
                    pending_value=new_val_str,
                    user_email=current_user.email
                ))
            staged_results.append(field_name)
        else:
            if existing:
                existing.value = new_val_str
            else:
                db.add(HedgeConfig(key=field_name, value=new_val_str))

            audit = ConfigAuditLog(
                user_email=current_user.email,
                config_type="HEDGE",
                field_name=field_name,
                old_value=old_val_str,
                new_value=new_val_str,
                apply_mode="IMMEDIATE",
                status="APPLIED",
                ip_address=ip_addr
            )
            db.add(audit)
            applied_results.append(field_name)

    db.commit()

    if session_active:
        return {
            "status": "DEFERRED",
            "message": "Hedge session active. Changes staged to apply automatically on session close.",
            "staged_fields": staged_results,
            "applied_fields": applied_results
        }

    return {
        "status": "APPLIED",
        "message": "Hedge configuration updated live immediately.",
        "applied_fields": applied_results
    }

@router.post("/hedge/strategies")
def update_hedge_strategy_rules(
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    strat_id = payload.get("id")
    strat_name = payload.get("strategy_name", "Bullish Hedge")

    strategy = None
    if strat_id:
        strategy = db.query(HedgeStrategyConfig).filter(HedgeStrategyConfig.id == strat_id).first()
    if not strategy:
        strategy = db.query(HedgeStrategyConfig).filter(HedgeStrategyConfig.strategy_name == strat_name).first()

    if not strategy:
        strategy = HedgeStrategyConfig(
            strategy_name=strat_name,
            strategy_key=strat_name.lower().replace(" ", "_")
        )
        db.add(strategy)

    if "enabled" in payload: strategy.enabled = bool(payload["enabled"])
    if "direction" in payload: strategy.direction = str(payload["direction"])
    if "trade_start_h" in payload: strategy.trade_start_h = int(payload["trade_start_h"])
    if "trade_start_m" in payload: strategy.trade_start_m = int(payload["trade_start_m"])
    if "trade_end_h" in payload: strategy.trade_end_h = int(payload["trade_end_h"])
    if "trade_end_m" in payload: strategy.trade_end_m = int(payload["trade_end_m"])
    if "force_close_h" in payload: strategy.force_close_h = int(payload["force_close_h"])
    if "force_close_m" in payload: strategy.force_close_m = int(payload["force_close_m"])
    if "contract_qty" in payload: strategy.contract_qty = float(payload["contract_qty"])
    if "max_premium" in payload: strategy.max_premium = float(payload["max_premium"])
    if "max_time_value" in payload: strategy.max_time_value = float(payload["max_time_value"])

    db.commit()
    return {"status": "SUCCESS", "message": f"Strategy rules updated for '{strategy.strategy_name}'"}
