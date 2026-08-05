import frappe
from hedge_trader.trading.ingest import upsert_position

def run():
    print("=== Syncing active Bearish position into tabHedge Open Position ===")
    pos_data = {
        "position_key": "BearishExecutor_Paper_exp_040826_1330_bear",
        "session": "exp_040826_1330_bear",
        "strategy": "Bearish Hedge",
        "executor": "BearishExecutor_Paper",
        "mode": "Paper",
        "status": "Open",
        "instrument_type": "Combined",
        "symbol": "BTCUSDT",
        "side": "SHORT",
        "qty": 10.0,
        "remaining_qty": 10.0,
        "entry_price": 62642.20,
        "mark_price": 62697.94,
        "hedge_symbol": "BTC-260804-62500-C",
        "hedge_qty": 10.0,
        "hedge_entry_price": 468.95,
        "opened_at": "2026-08-03 16:22:48",
    }
    res = upsert_position(pos_data)
    print("Synced position successfully!")

    print("\n=== Verifying tabHedge Open Position count ===")
    count = frappe.db.sql("SELECT COUNT(*) FROM `tabHedge Open Position`")[0][0]
    print(f"Total rows in tabHedge Open Position: {count}")
    rows = frappe.db.sql("SELECT name, position_key, strategy, symbol, side, qty, entry_price, status FROM `tabHedge Open Position`", as_dict=True)
    for r in rows:
        print(" ", r)
