import asyncio
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.schema import StraddleConfig, HedgeConfig, StraddleSession, HedgeSession
from app.core.binance_client import get_btc_futures_mark_price, get_btc_spot_price
from app.core.straddle_engine import straddle_engine
from app.core.hedge_engine import hedge_engine

router = APIRouter(prefix="/api/v1/dashboard", tags=["Dashboard"])

@router.get("/snapshot")
async def get_dashboard_snapshot(db: Session = Depends(get_db)):
    mark_price = await get_btc_futures_mark_price()
    spot_price = await get_btc_spot_price()

    straddle_cfg = {c.key: c.value for c in db.query(StraddleConfig).all()}
    latest_straddle_session = db.query(StraddleSession).order_by(StraddleSession.id.desc()).first()

    hedge_cfg = {c.key: c.value for c in db.query(HedgeConfig).all()}
    latest_hedge_session = db.query(HedgeSession).order_by(HedgeSession.id.desc()).first()

    paper_wallet = float(straddle_cfg.get("PAPER_WALLET_USDT", "100000.0"))

    return {
        "market": {
            "btc_mark_price": mark_price,
            "btc_spot_price": spot_price,
            "currency_symbol": "$"
        },
        "straddle": {
            "state": straddle_engine.state,
            "config": straddle_cfg,
            "active_session": latest_straddle_session
        },
        "hedge": {
            "state": hedge_engine.state,
            "config": hedge_cfg,
            "active_session": latest_hedge_session
        },
        "wallet": {
            "paper_wallet_usdt": paper_wallet,
            "currency": "USD"
        }
    }

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
