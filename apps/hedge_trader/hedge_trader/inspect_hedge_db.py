import frappe

def run():
    print("=== 1. hedge_paper_trades count ===")
    count = frappe.db.sql("SELECT COUNT(*) FROM hedge_paper_trades")[0][0]
    print(f"Total rows in hedge_paper_trades: {count}")

    print("\n=== 2. Latest 10 rows in hedge_paper_trades ===")
    rows = frappe.db.sql("SELECT id, trader_name, action, symbol, side, qty, fill_price, ts_ist FROM hedge_paper_trades ORDER BY id DESC LIMIT 10", as_dict=True)
    for r in rows:
        print(r)

    print("\n=== 3. hedge_frappe_sync_cursor ===")
    cursors = frappe.db.sql("SELECT * FROM hedge_frappe_sync_cursor", as_dict=True)
    for c in cursors:
        print(c)

    print("\n=== 4. Hedge Open Position DocType count ===")
    pos_count = frappe.db.sql("SELECT COUNT(*) FROM `tabHedge Open Position`")[0][0]
    print(f"Total rows in tabHedge Open Position: {pos_count}")

    print("\n=== 5. Hedge Paper Ledger Entry DocType count ===")
    ledger_count = frappe.db.sql("SELECT COUNT(*) FROM `tabHedge Paper Ledger Entry`")[0][0]
    print(f"Total rows in tabHedge Paper Ledger Entry: {ledger_count}")
