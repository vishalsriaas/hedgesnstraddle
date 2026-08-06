import asyncio
from typing import Dict, Any, Optional
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.schema import User, StraddleConfig, HedgeConfig, StraddleSession, HedgeSession, StraddleWalletLedger, StraddleTradeOrder, HedgeTradeOrder, StraddleFill, HedgeFill, ConfigAuditLog
from app.core.auth import get_current_user, require_admin, get_client_ip
from app.core.binance_client import get_btc_futures_mark_price, get_btc_spot_price
from app.core.straddle_engine import straddle_engine
from app.core.hedge_engine import hedge_engine

router = APIRouter(prefix="/api/v1/dashboard", tags=["Dashboard"])

@router.get("/snapshot")
async def get_dashboard_snapshot(db: Session = Depends(get_db)):
    mark_price = await get_btc_futures_mark_price()
    spot_price = await get_btc_spot_price()

    # Straddle Details
    straddle_cfg = {c.key: c.value for c in db.query(StraddleConfig).all()}
    straddle_sessions = db.query(StraddleSession).order_by(StraddleSession.id.desc()).limit(10).all()
    latest_straddle = straddle_sessions[0] if straddle_sessions else None

    # Hedge Details
    hedge_cfg = {c.key: c.value for c in db.query(HedgeConfig).all()}
    hedge_sessions = db.query(HedgeSession).order_by(HedgeSession.id.desc()).limit(10).all()
    latest_hedge = hedge_sessions[0] if hedge_sessions else None

    # Fills & Orders
    straddle_orders = db.query(StraddleTradeOrder).order_by(StraddleTradeOrder.id.desc()).limit(10).all()
    hedge_orders = db.query(HedgeTradeOrder).order_by(HedgeTradeOrder.id.desc()).limit(10).all()

    paper_wallet = float(straddle_cfg.get("PAPER_WALLET_USDT", "100000.0"))

    # System Health Checks
    health_issues = []
    if mark_price <= 0:
        health_issues.append({"severity": "Warning", "algo": "System", "type": "Binance API", "detail": "Mark price feed slow"})

    return {
        "market": {
            "btc_mark_price": mark_price,
            "btc_spot_price": spot_price,
            "currency_symbol": "$"
        },
        "straddle": {
            "state": straddle_engine.state,
            "config": straddle_cfg,
            "active_session": latest_straddle,
            "history": straddle_sessions,
            "orders": straddle_orders
        },
        "hedge": {
            "state": hedge_engine.state,
            "config": hedge_cfg,
            "active_session": latest_hedge,
            "history": hedge_sessions,
            "orders": hedge_orders
        },
        "health": {
            "database_healthy": True,
            "straddle_engine_healthy": straddle_engine.is_running,
            "hedge_engine_healthy": hedge_engine.is_running,
            "audit_issues": health_issues
        },
        "wallet": {
            "paper_wallet_usdt": paper_wallet,
            "currency": "USD"
        }
    }

@router.post("/straddle/squareoff")
def trigger_straddle_squareoff(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    logger_msg = f"User {current_user.email} triggered EMERGENCY SQUARE-OFF for Straddle Bot"
    straddle_engine.state = "SQUAREOFF"
    
    # Update active session status if present
    active_session = db.query(StraddleSession).filter(StraddleSession.status == "OPEN").first()
    if active_session:
        active_session.status = "MANUAL_SQUAREOFF"
        active_session.exit_reason = f"Emergency Square-Off triggered by {current_user.email}"
        db.commit()

    return {"status": "SUCCESS", "message": logger_msg}

@router.post("/hedge/squareoff")
def trigger_hedge_squareoff(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    logger_msg = f"User {current_user.email} triggered EMERGENCY SQUARE-OFF for Hedge Trader"
    hedge_engine.state = "SQUAREOFF"
    
    active_session = db.query(HedgeSession).filter(HedgeSession.status == "Open").first()
    if active_session:
        active_session.status = "Manual Square-off"
        active_session.exit_reason = f"Emergency Square-Off triggered by {current_user.email}"
        db.commit()

    return {"status": "SUCCESS", "message": logger_msg}

@router.websocket("/ws/live")
async def websocket_live_feed(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            mark_price = await get_btc_futures_mark_price()
            spot_price = await get_btc_spot_price()

            payload = {
                "btc_mark": mark_price,
                "btc_spot": spot_price,
                "straddle_state": straddle_engine.state,
                "hedge_state": hedge_engine.state
            }

            await websocket.send_json(payload)
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        pass
    except Exception:
        await websocket.close()
