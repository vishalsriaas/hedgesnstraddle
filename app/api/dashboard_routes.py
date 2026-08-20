import asyncio
from typing import Dict, Any, Optional
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.schema import User, StraddleConfig, HedgeConfig, StraddleSession, HedgeSession, StraddleWalletLedger, StraddleTradeOrder, HedgeTradeOrder, HedgeOpenPosition, StraddleFill, HedgeFill, ConfigAuditLog
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

    # Fills, Orders & Ledger
    straddle_orders = db.query(StraddleTradeOrder).order_by(StraddleTradeOrder.id.desc()).limit(50).all()
    straddle_ledger = db.query(StraddleWalletLedger).order_by(StraddleWalletLedger.id.desc()).limit(50).all()
    hedge_orders = db.query(HedgeTradeOrder).order_by(HedgeTradeOrder.id.desc()).limit(50).all()
    hedge_positions = db.query(HedgeOpenPosition).all()

    cash_balance = float(straddle_cfg.get("PAPER_WALLET_USDT", "100000.0"))

    # Calculate Mark-to-Market Total Wallet Valuation (Broker Standard Equity Formula)
    open_positions_val = 0.0
    if straddle_engine.active_session_id:
        active_ord = db.query(StraddleTradeOrder).filter(StraddleTradeOrder.session_id == straddle_engine.active_session_id).first()
        qty = active_ord.qty if active_ord else float(straddle_cfg.get("TRADE_QTY", "10"))
        # 1. Option legs current mark valuation
        active_c_mark = straddle_engine.active_call_mark if straddle_engine.active_call_mark > 0 else straddle_engine.current_call_mark
        active_p_mark = straddle_engine.active_put_mark if straddle_engine.active_put_mark > 0 else straddle_engine.current_put_mark
        open_positions_val += (active_c_mark + active_p_mark) * qty
        
        # 2. Futures leg floating PnL if in trade
        if straddle_engine.state == "IN_TRADE":
            latest_sess = db.query(StraddleSession).filter(StraddleSession.id == straddle_engine.active_session_id).first()
            if latest_sess and latest_sess.futures_entry_price:
                fut_side = 1 if (latest_sess.futures_tp_price > latest_sess.futures_entry_price) else -1
                fut_pnl = fut_side * (mark_price - latest_sess.futures_entry_price) * qty
                open_positions_val += fut_pnl

    total_wallet_valuation = round(cash_balance + open_positions_val, 2)

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
            "trade_qty": float(straddle_cfg.get("TRADE_QTY", "10")),   # explicit field for JS PnL calc
            "active_session": latest_straddle,
            "live_futures_mark": straddle_engine.last_futures_mark,
            "live_call_strike": straddle_engine.current_strike,
            "live_put_strike": straddle_engine.current_strike,
            "live_call_mark": straddle_engine.current_call_mark,
            "live_put_mark": straddle_engine.current_put_mark,
            "active_call_mark": straddle_engine.active_call_mark,
            "active_put_mark": straddle_engine.active_put_mark,
            "history": straddle_sessions,
            "orders": straddle_orders,
            "ledger": straddle_ledger,
            "live_monitoring": straddle_engine.get_live_monitoring_snapshot(db)
        },
        "hedge": {
            "state": hedge_engine.state,
            "config": hedge_cfg,
            "active_session": latest_hedge,
            "history": hedge_sessions,
            "orders": hedge_orders,
            "positions": hedge_positions,
            "live_monitoring": hedge_engine.get_live_monitoring_snapshot(db)
        },
        "health": {
            "database_healthy": True,
            "straddle_engine_healthy": straddle_engine.is_running,
            "hedge_engine_healthy": hedge_engine.is_running,
            "audit_issues": health_issues
        },
        "wallet": {
            "paper_wallet_usdt": total_wallet_valuation,
            "cash_balance": cash_balance,
            "open_positions_val": open_positions_val,
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
    active_session = db.query(StraddleSession).filter(StraddleSession.status.in_(["Open", "OPEN"])).first()
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

@router.post("/hedge/reset")
def trigger_hedge_reset(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    """Clears all Hedge sessions, orders, positions, fills, and paper ledger entries to start fresh without affecting Straddle."""
    from app.models.schema import HedgeSession, HedgeOpenPosition, HedgeTradeOrder, HedgeFill, HedgePaperLedgerEntry, HedgeSessionEvent

    db.query(HedgeSessionEvent).delete()
    db.query(HedgePaperLedgerEntry).delete()
    db.query(HedgeFill).delete()
    db.query(HedgeTradeOrder).delete()
    db.query(HedgeOpenPosition).delete()
    db.query(HedgeSession).delete()

    hw = db.query(HedgeConfig).filter(HedgeConfig.key == "PAPER_WALLET_USDT").first()
    if hw:
        hw.value = "100000.0"
    else:
        db.add(HedgeConfig(key="PAPER_WALLET_USDT", value="100000.0"))

    db.commit()

    hedge_engine.slot1_session_id = None
    hedge_engine.slot2_session_id = None
    hedge_engine.slot1_strike = None
    hedge_engine.slot2_strike = None
    hedge_engine.slot1_completed = False
    hedge_engine.slot2_completed = False
    hedge_engine.state = "IDLE"

    return {"status": "SUCCESS", "message": "Hedge Trader records cleared for a fresh start. Straddle Panel remains untouched."}

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
