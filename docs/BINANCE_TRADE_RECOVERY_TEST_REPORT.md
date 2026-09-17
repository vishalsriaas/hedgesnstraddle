# Binance trade recovery audit

**Follow-up:** the two defects described in this historical audit have now been
corrected and added to the default regression suite. See
[entry recovery implementation and validation](STRADDLE_ENTRY_RECOVERY_FIX.md).
The original evidence and results below describe the pre-fix version.

Date: 2026-09-17. Scope: the current local futures target recovery implementation.
Public Binance BTCUSDT USD-M aggregate trades only. No account credentials,
exchange orders, application database changes, bot setting changes, or deployment.

## Verdict

The recovery implementation must not yet be described as guaranteeing no missed
trades. Ordinary recovery and pagination tests pass, but two additional adversarial
tests reproduce missed-crossing defects when trade history is published late.
These defects were demonstrated using controlled responses, not observed Binance
data loss. Execution code was left unchanged during this audit.

## Evidence

The live run compares a continuous independent WebSocket capture against the
actual `replay_target` function and production REST client at five-second intervals.
The target is deliberately unreachable so every trade must be consumed. Each
successfully processed event is counted, not merely the final cursor value.
After capture, a third, independent REST history download checks the same ID range.
Prices, quantities, timestamps, raw-trade range IDs and maker flags are compared.

REST failures are injected for 25 seconds and 45 seconds while the WebSocket
reference continues receiving data. These simulate outages at the application's
data-fetch boundary; they do not disconnect the host's network. The temporary
SQLite engine/session is closed and reopened, and recovery uses the persisted
cursor. A separate fresh Python process also reads the stored cursor successfully.
The application itself was not restarted or deployed.

Recorded artifacts:
`backups/live-binance-audit-20260917-112149/`

- `websocket.jsonl`: independent live reference events.
- `rest_pages.jsonl`: actual polling requests and returned pages.
- `rest_processed.json`: records processed by the recovery loop.
- `independent_history.json`: post-capture independent REST reference.
- `summary.json`: live counts, comparisons, errors, source hashes and timestamps.
- `historical_stress.json`: mature history pagination/catch-up results.
- `historical_target_scenarios.json`: 120 target-crossing comparisons.
- `edge_cases.json`: the additional controlled boundary failures.

## Live comparison results

Window: **2026-09-17 11:21:51.851-11:26:51.851 UTC**
(**16:51:51.851-16:56:51.851 IST**), plus a 15-second drain period.

| Check | Result |
| --- | --- |
| Continuous WebSocket reference | 1,661 events |
| Production REST recovery loop | 1,661 events |
| Independent post-run REST download | 1,661 events |
| Aggregate ID range | 3453785686-3453787346, contiguous |
| Missing / extra / duplicate processed events | 0 / 0 / 0 |
| Price, quantity, timestamp, trade-range or maker-flag mismatches | 0 |
| Events recovered from injected 25-second outage | 347 |
| Events recovered from injected 45-second outage | 172 |
| Persisted-cursor reload | Passed |
| Unexpected WebSocket / REST errors | 0 / 0 |

**All three views matched for this five-minute interval.** The two controlled
late-publication defects below did not occur in the live sample. Source SHA-256
hashes in `summary.json` match the unchanged application files after the run.

## Historical and offline tests

- **7,000 real historical aggregate events:** all recovered, no missing or extra
  IDs and no field mismatches. First pass processed exactly 5,000 and correctly
  reported incomplete coverage; the next pass recovered the remaining 2,000.
- **120 BUY/SELL target scenarios:** replay against the captured historical data
  identified the same first crossing as an independent scan, always at the stored
  limit price. This is deterministic historical replay, not 120 live orders.
- **Existing regression suite:** 66 passed, one optional MCP protocol test skipped.
- **New adversarial suite:** one passed, two failed. A failure on page two correctly
  preserved the previous cursor, but both late-publication boundary cases failed.

## Defect 1: empty initial fetch can skip a late first trade

Location: `app/core/futures_targets.py`, time-window bootstrap and coverage update.

Reproduction:

1. Activate a sell target at time 0.
2. At time 5, history returns an empty array. The cursor advances to time 5 while
   `last_id` remains null.
3. A trade that crossed the target at time 4 becomes available after that response.
4. At time 10, the next request starts at time 5. It cannot retrieve the time-4
   trade, so the target stays pending despite the crossing.

Required correction: establish an ID anchor before activation, or retain/recheck
the unanchored interval until an ID-based sequence is established. An empty fetch
must not permanently discard the initial interval.

## Defect 2: close-deadline coverage prevents late-trade recovery

Location: `app/core/futures_targets.py`, early return when `end_ms <= checked_ms`.

Reproduction:

1. At a time-5 close deadline, history includes a non-crossing trade at time 2.
2. The code marks time 5 covered and allows the scheduled close decision.
3. A crossing at time 4 becomes available afterward.
4. A later replay with the same deadline returns immediately without fetching.

Required correction: do not treat one current-time REST response as final evidence
that all pre-deadline trades have arrived. Define a finalization/reconciliation
policy using server time and ID-based recovery, with a clear late-data outcome.
An arbitrary short sleep alone cannot prove permanent completeness.

## Interpretation and operational limits

These are aggregate events, not individual raw trade ticks. Binance groups fills
at the same price and taking side within 100 milliseconds. Insurance-fund and ADL
trades are excluded from this feed. A passing finite test establishes consistency
for its tested interval; it cannot prove every future event will be recovered.

The local clock was approximately 601 ms behind Binance in the initial midpoint
measurement. This is an approximate network measurement, not an exact clock audit.
Activation timestamps currently use local time, so clock alignment must also be
addressed before claiming precise ordering against exchange timestamps.

The WebSocket and REST views come from the same exchange and are not an independent
exchange matching-engine audit. This test does not establish queue position,
partial-fill realism, or raw-tick completeness.

Official stream description:
https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market#aggregate-trade-streams

## Reproduction commands

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -m unittest discover -s tests -p review_trade_recovery_edges.py -v
.\.venv\Scripts\python.exe tests/manual_binance_trade_audit.py --seconds 300
.\.venv\Scripts\python.exe tests/manual_binance_history_stress.py backups/history-audit
```

The boundary review suite intentionally reports two failures until the defects are
fixed; its `review_` prefix keeps it separate from the existing default suite.
The live audit is opt-in and stores evidence and its own disposable SQLite database
under `backups/`; it never opens the application's database.
