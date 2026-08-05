import frappe

def run():
    print("=== Cleaning up duplicate records in tabHedge Paper Ledger Entry ===")

    # Keep only the min name (first creation) for each unique group of (executor, symbol, side, qty, fill_price)
    duplicates = frappe.db.sql("""
        SELECT name FROM `tabHedge Paper Ledger Entry`
        WHERE name NOT IN (
            SELECT min_name FROM (
                SELECT MIN(name) as min_name
                FROM `tabHedge Paper Ledger Entry`
                GROUP BY executor, symbol, side, qty, fill_price
            ) as t
        )
    """, as_dict=True)

    print(f"Found {len(duplicates)} duplicate ledger records to remove.")
    for d in duplicates:
        frappe.db.sql("DELETE FROM `tabHedge Paper Ledger Entry` WHERE name = %s", (d['name'],))

    frappe.db.commit()

    remaining_ledger = frappe.db.sql("SELECT name, executor, symbol, side, qty, fill_price FROM `tabHedge Paper Ledger Entry` ORDER BY creation DESC", as_dict=True)
    print(f"Cleaned! Total unique ledger records remaining: {len(remaining_ledger)}")
    for r in remaining_ledger:
        print(" ", r)

    print("\n=== Populating active positions in tabHedge Open Position ===")

    # Get active session from hedge_trading_sessions if any
    active_sessions = frappe.db.sql("SELECT session_id, trader_name, status, entry_price, target_line FROM hedge_trading_sessions WHERE status='running'", as_dict=True)
    print(f"Active running sessions in DB: {len(active_sessions)}")
    for s in active_sessions:
        print(" ", s)

    frappe.destroy()
