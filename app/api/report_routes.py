import io
import csv
from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.schema import User, StraddleSession, HedgeSession
from app.core.auth import get_current_user

router = APIRouter(prefix="/api/v1/reports", tags=["Reports"])

@router.get("/trades.csv")
def export_trade_history_csv(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Strategy", "Session ID", "Expiry/Symbol", "Status", 
        "BTC Spot/Entry", "Realized PnL ($)", "Exit Reason", "Timestamp"
    ])

    straddle_sessions = db.query(StraddleSession).order_by(StraddleSession.id.desc()).all()
    for s in straddle_sessions:
        writer.writerow([
            "Straddle Bot", s.id, s.expiry_sym, s.status,
            f"${s.btc_entry_spot:,.2f}", f"${s.pnl_realized:,.2f}",
            s.exit_reason or "", s.created_at.strftime("%Y-%m-%d %H:%M:%S") if s.created_at else ""
        ])

    hedge_sessions = db.query(HedgeSession).order_by(HedgeSession.id.desc()).all()
    for h in hedge_sessions:
        writer.writerow([
            "Hedge Trader", h.id, h.symbol, h.status,
            f"${h.bull_entry:,.2f}", f"${h.realized_pnl:,.2f}",
            h.exit_reason or "", h.created_at.strftime("%Y-%m-%d %H:%M:%S") if h.created_at else ""
        ])

    output.seek(0)
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=Hedgesnstraddle_Trade_History.csv"}
    )
