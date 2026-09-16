# Offline trading regression tests

Read-only MCP setup and its separate test command are documented in
[MCP_README.md](../MCP_README.md). Its tests use temporary SQLite data; the
protocol test requires `requirements-mcp.txt` and otherwise reports a skip.

From the repository root on Windows:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

These tests use an in-memory SQLite database, a fixed clock, and simulated quotes.
They do not start the application engines against the working database or contact Binance.
The existing `run_test_suite.py` is a separate integration script that changes configuration
and requests square-offs on its target server; run it only against a disposable instance.

Covered behavior includes locked straddle orders and quantities, cutoff and TP exits,
wallet/ledger reconciliation, independent hedge schedules, repeat-safe square-off,
restart recovery, pause/disable controls, quote availability, dashboard serialization,
and the hedge algorithm document's first/second TP phases.

Hedge TP closes the initial futures leg. The first TP creates an option target at twice
its entry premium. The second creates a futures re-entry limit at strike minus premium
(bullish) or strike plus premium (bearish). Scheduled/manual exits cancel pending orders
and settle remaining positions. Each role follows its configured close time. Existing
paper wallet configuration is retained; newly realized PnL is credited once per closing leg.

Missing exact-contract marks defer execution instead of inventing prices. Cached market
data older than 30 seconds is rejected. Missing/expired quotes require a valid mark to
resume closing; this change does not invent an expiry settlement price or rewrite history.

## Hedge entry contract selection

Each scan uses one options response and one BTC spot reference. Eligible contracts have
strictly more than zero and less than 24 hours remaining until their 13:30 IST expiry.
At 14:00 IST the next day's 13:30 expiry qualifies; at exactly 13:30 the expired contract
and the next contract with exactly 24 hours remaining are both excluded. No later expiry
is substituted if there are no eligible contracts.

For each trader, scan the actual listed ITM strikes using spot for both ITM classification
and intrinsic value. Preserve the existing inclusive ATM boundary. Filter by that trader's
maximum premium and time-value limits, then choose the lowest time value. Auto compares
calls and puts together. Exact ties use symbol order only for deterministic output.

There is no strike-clash restriction between traders. Execution and dashboard previews
share the same selector.
Futures fills and TP calculations continue to use futures mark prices.

There is no independent global option-spend restriction. Each trader controls premium,
time value and quantity. Cards show estimated option cost (mark premium x configured
quantity) for information only. Legacy MAX_OPTION_SPEND database values are ignored.

Once Trader 1 has entered for the current expiry session, Trader 2 must select the
opposite direction, overriding its configured/Auto direction. This is determined from
recorded entries, so it survives a restart and Trader 1 closing its position. Trader 2
still selects the lowest-TV qualifying option within that required direction, regardless
of the first trader's strike. No qualifying opposite contract means no entry.
If Trader 1 has not entered in that expiry session, Trader 2 uses its configured direction.
Existing positions are not reversed or replaced by this rule change.
