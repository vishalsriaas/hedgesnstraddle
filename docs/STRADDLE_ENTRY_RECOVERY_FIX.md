# Straddle entry recovery and execution timestamps

Implemented 2026-09-17. Scope: Straddle OCO futures entries, futures target recovery,
and execution timestamp display for both panels. Strategy selection, premiums,
quantity, fee conventions, configured schedules, hedge entry/re-entry calculations,
and option-exit pricing remain unchanged.

## Execution changes

- Before a new Straddle OCO pair is activated, capture a Binance aggregate-trade ID
  and server-time baseline. Record the exchange/local clock offset for replay.
  If this fails, entry is deferred without partially recording orders or premiums.
- Both entry limits share one durable `FUTURES_ENTRY_TRACKING` event. Replay exact
  BTCUSDT futures trades in ID order; the first eligible side wins, fills at the
  stored limit price, and cancels the other side in the same transaction.
- Record an entry fill linked to the order. Anchor the new target to that entry's
  trade ID and exchange timestamp. Continue recovery immediately; later IDs in the
  same millisecond or polling interval can fill the target.
- Entry cutoff is exclusive. Replay preceding events before expiring the OCO pair;
  a trade exactly at cutoff cannot open the futures position.
- An empty bootstrap response retains its original interval. Existing unanchored
  cursors are also reset to their original activation interval when retried.
- Deadline finalization requires a contiguous next trade with a timestamp beyond
  the boundary. Empty/short responses or elapsed time alone do not finalize it.
  Missing IDs or unavailable history defer automatic decisions. This relies on
  Binance's trade-ID sequence; it is not an arbitrary sleep-based finality claim.
- A limit activated after a cutoff has no eligible lifetime before that cutoff.
- Existing pending OCO pairs without tracking start tracking at upgrade time and
  log a warning. Their original placement times are preserved. No past fills are
  invented, and filled historical entries are not replayed or rewritten.

## Timestamps and database migration

Startup adds nullable fields to both `straddle_trade_orders` and
`hedge_trade_orders`: `filled_at TIMESTAMP`, `processed_at TIMESTAMP`, and
`aggregate_trade_id BIGINT`. Both fill tables gain nullable `order_id INTEGER`.
The additive migration is repeatable and does not overwrite historical values.

- `created_at`: original placement time. A target placed by historical replay uses
  the recovered entry time as its simulated placement time.
- `filled_at`: qualifying Binance aggregate trade timestamp, stored as naive IST
  to match the existing application convention.
- `processed_at`: processing time aligned using the saved Binance clock offset.
- `aggregate_trade_id`: supporting exchange event ID.
- Fill `created_at`: the execution time; `order_id` associates it with its order.

Both order tables display **Fill time (IST)** for filled rows and preserve placement
and processing details. Milliseconds are displayed for execution timestamps.
Pending rows show placement time. Missing historical execution timestamps display
**Not recorded**, never placement time disguised as fill time. The new fields are
populated for replay-backed futures entries/targets; other existing order paths
are not reworked by this change.

## Validation

- Offline suite: 77 passed, one optional MCP protocol test skipped (78 total).
- The two previously failing late-publication audit cases now pass and are included
  in the default suite as `test_trade_recovery_boundaries.py`.
- Regression reproduces the user's target 76563.8468: qualifying trade at
  17:40:12.758, processing at 17:41:22, placement retained separately.
- Actual Binance history replay from 17:05:02.260 identifies aggregate trade
  **3453819633**, timestamp **17:40:12.758 IST**, market trade price **76563.90**,
  simulated entry price **76563.8468**. The approximately 30,000-event backlog
  spans seven bounded recovery passes; this is historical verification, not a
  modification of the existing trade record.
- Separate 7,024-event historical replay: no missing IDs, extra IDs or field
  mismatches; caught up over two passes with the 5,000-record per-pass cap.
- Tests cover opposite OCO ordering, entry plus TP in the same millisecond,
  pre-cutoff late arrivals, exact-cutoff exclusion, restart/repeat safety, and
  linked fills and wallet settlement.
- Migration tested twice on an old-schema fixture and on a copy of the local
  trading database; historical sessions, settings and ledgers were unchanged.
- JavaScript syntax and timestamp-rendering checks passed.

Evidence: `backups/entry-recovery-history-audit/`.
Pre-change backup: `backups/before-entry-recovery-20260917-185017/`.

## Remaining simulation limits

This remains paper simulation using aggregate trades, not individual raw ticks or
exchange-confirmed fills. Queue priority, partial fills and price tick normalization
are outside this change. Option exits still use processing-time quotes. Missing
option quotes can delay processing; recovery resumes when required data returns.
Very large backlogs require multiple passes. History older than the configured
47-hour recovery limit is flagged for review. New configuration controls, funding,
and maker-fee modelling are not introduced.

Only the local test instance at port 8085 is updated. No Git push or Linux-server
deployment is included.
