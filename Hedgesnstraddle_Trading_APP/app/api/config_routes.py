from typing import Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.schema import User, StraddleConfig, HedgeConfig, PendingConfig, ConfigAuditLog
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

        if window_active:
            pending = db.query(PendingConfig).filter(
                PendingConfig.config_type == "STRADDLE",
                PendingConfig.field_name == field_name
            ).first()

            if pending:
                pending.pending_value = new_val_str
                pending.user_email = current_user.email
            else:
                db.add(PendingConfig(
                    config_type="STRADDLE",
                    field_name=field_name,
                    pending_value=new_val_str,
                    user_email=current_user.email
                ))
            staged_results.append(field_name)
        else:
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

    db.commit()

    if window_active:
        return {
            "status": "DEFERRED",
            "message": "Trading window active. Changes staged to apply automatically on window close.",
            "staged_fields": staged_results,
            "applied_fields": applied_results
        }

    return {
        "status": "APPLIED",
        "message": "Configuration updated live immediately.",
        "applied_fields": applied_results
    }

@router.get("/hedge")
def get_hedge_config(db: Session = Depends(get_db)):
    configs = db.query(HedgeConfig).all()
    pending = db.query(PendingConfig).filter(PendingConfig.config_type == "HEDGE").all()
    return {
        "active": {c.key: c.value for c in configs},
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
