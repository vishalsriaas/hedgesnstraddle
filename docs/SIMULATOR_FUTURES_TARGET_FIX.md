# Futures target execution and missing-trade recovery

This document describes the initial TP-only implementation. It is superseded for
entry recovery, timestamp storage and deadline finalization by
[Straddle entry recovery and execution timestamps](STRADDLE_ENTRY_RECOVERY_FIX.md).

## Backup

Pre-change directory:
`D:\desktop\Testing\Hedgesnstraddle\simulator_fixes_missing_binance_data`

Source:
`D:\desktop\Testing\Hedgesnstraddle\HnS_application_11aug`

Backup includes application files, configuration, documentation, tests, dependency
manifests, and consistent SQLite snapshots verified with `PRAGMA integrity_check`.
Disposable virtual environments, Git internals and Python bytecode are excluded.
Existing historical backup files are retained. No database reset was performed.

## Exact scope

Only futures take-profit simulation in the Straddle and Hedge engines, its trade
history reader, persistence, and tests are changed. Entry selection, entry limits,
hedge re-entry limits, option targets/exits, quantities, fee conventions, and
mark-based unrealized valuation retain their existing behavior. No exchange orders
are submitted. No live-server deployment or Git push is part of this change.

## Behavior

1. When the initial futures position opens, create a pending `LIMIT` target using
   the existing target formula and original position quantity.
2. Map the existing `BTC-USDT-FUTURES` label explicitly to Binance USD-M perpetual
   `BTCUSDT`; reject unknown mappings rather than substitute another contract.
3. Poll `/fapi/v1/aggTrades` at most once per five seconds per pending target.
   Actual cadence also depends on the existing two-second engine loop and request
   latency. Bootstrap with a time range, then continue with `fromId=last_id+1`.
4. Replay all returned records in sequence. Recover multiple pages, up to five
   pages of 1,000 records per pass, then resume from the saved cursor next pass.
   Time windows are at most 30 minutes. A full page never proves completion.
5. First qualifying post-activation trade: SELL at trade price >= target, BUY at
   trade price <= target. Simulate the fill at the target, never at that trade's
   price or the current mark. Trades in the activation millisecond are excluded
   because relative order within that millisecond is ambiguous.
6. Update the existing target order, add a futures fill, and settle session PnL,
   position changes and wallet credit in the same database transaction.
7. Retain the trade ID, contract, observed price, simulated fill price, exchange
   trade timestamp and processing timestamp. Fill timestamps use the crossing;
   linked option actions use available quotes at processing time as before.
8. Manual exits cancel pending targets through the existing path. Before scheduled
   exits, recover target history only through the scheduled deadline. If recovery
   is incomplete, defer automatic close rather than assume no earlier crossing.

Example: entry 76471.88, put premium 81.31, quantity 10, recovered trade 76560.
Target and simulated exit = 76553.19; futures realized PnL = 813.10. The observed
76560 is audit evidence only. Existing rounding conventions remain unchanged.

## Storage and restart

No schema migration or new tables. Existing order tables store pending limits;
existing fill tables store TP fills (fee remains zero under the existing paper
convention). Existing session event tables store one `FUTURES_TP_TRACKING` event
per session. Its JSON contains:

`order_id`, `symbol`, `active_ms`, `checked_ms`, `last_id`, `last_trade_ms`,
`last_poll_ms`, `limit_price`, `qty`, `side`, and, after crossing, `hit`.

`hit` contains `aggregate_trade_id`, `trade_time_ms`, `observed_price`,
`fill_price`, `processed_time_ms`, and `symbol`.

`checked_ms` records fully examined time coverage; `last_id` records individual
pagination progress. They deliberately differ while a full page is incomplete.
Existing reset routes already clear orders, fills and session events together.

For old open positions without target-tracking records, tracking starts on the
first management pass after upgrade, with a warning in the application log. Old
completed trades are not rewritten. Earlier crossings are not reconstructed from
unreliable historical target-activation times.

## Operational limits and issues

- This is a price-crossing simulation, not an exchange matching engine. A touch
  assumes a full paper fill; queue priority, partial fills, tick-size normalization,
  and executable liquidity are not modelled by this change.
- REST history is aggregated, not an individual raw-tick archive. Binance groups
  trades at the same price/taking side within 100 milliseconds.
- API failures, invalid values, wrong contracts and missing aggregate IDs raise
  `DATA_GAP`; no fallback to mark price is permitted. HTTP 418/429 triggers shared
  backoff. Other errors are retried by the existing engine loop.
- Recovery older than 47 hours is blocked for review, leaving a margin inside the
  documented 48-hour API retention. A data gap can delay automatic scheduled close.
  Manual square-off remains available, using its existing quote requirements.
- Option exits still require their exact held-contract quotes. In Straddle, missing
  option quotes can defer the whole close. Recovering futures history does not
  recreate an option quote at the historical crossing time.
- Hedge first/second TP ranking retains the existing engine processing order;
  simultaneous crossings recovered for different roles are not globally reordered.
- Run the existing single application worker; multiple workers competing to settle
  the same paper position are not supported by the existing architecture. Keep the
  server clock synchronized: activation timestamps use application UTC time.
- Paper fee fields staying at zero does not imply Binance maker trading is free.

API reference:
https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Compressed-Aggregate-Trades-List

## Validation and rollout

Run the offline suite from the repository root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The suite uses disposable SQLite databases and mocked exchange data. A separate
read-only live API smoke check successfully retrieved BTCUSDT aggregate trades;
it did not place orders or start application engines.

Deploy the changed code together, including `app/core/futures_targets.py`, and
restart the existing single Uvicorn worker. No new dependency or SQL migration is
required. The local implementation has not been deployed to the Linux server.
