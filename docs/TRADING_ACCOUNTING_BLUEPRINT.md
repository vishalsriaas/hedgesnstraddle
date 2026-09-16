Trading execution, contract identity and accounting specification — 2026-09-08

Deliverables: [exact PostgreSQL schema](trading_accounting_schema.sql), this specification, and the existing [isolated defect reproductions](../tests/review_trading_edge_cases.py). This is a design and root-cause investigation, not an applied production migration.

1. Scope and mandatory definitions

Implement one shared order/fill/valuation/accounting service for STRADDLE and HEDGE. Keep strategy decisions separate from execution and accounting. Supported instruments: USDT-settled linear futures and premium-paid long BTC options. Short options, inverse futures and other settlement currencies are rejected until separate accounting rules are implemented. Quantity is exchange contract quantity; effective BTC quantity is quantity multiplied by contract_multiplier. Legacy configuration described as BTC must be normalized once at order creation, not multiplied twice.

Use three distinct balances:

```text
cash_balance: actual USDT cash after option premium payments and receipts
open_option_basis: remaining historical purchase cost of open long options, excluding fees
book_balance = cash_balance + open_option_basis
equity = book_balance + total_unrealized_pnl
available_cash = cash_balance - reserved_cash
```

The requested invariant is exact for book_balance. It is also exact for cash over a period where opening and closing option basis are equal, including when all positions start and end flat. It is not generally true for cash alone while opening or closing options. Never label cash, book balance and equity interchangeably. Reserved margin is not a trading loss; reservations change available cash, not book balance.

2. Root causes and evidence

| ID | Finding | Evidence and current status |
|---|---|---|
| RC01 | Historical hedge PnL used the newly selected ITM contract | `app/core/hedge_engine_backup_20260826.py:223` and `:257` assign option current mark from preview marks. The older `backup_app_before_fixes/core/hedge_engine.py` contains the same defect. Current `app/core/hedge_engine.py:238` and `:283` instead look up the held full symbol. Current `dashboard_routes.py:57` does the same. The old bug is proven in source; whether a running process still serves that version requires the incident ID/time and its startup path/build identity. |
| RC02 | Current hedge cards and position table disagree within one response | `dashboard_routes.py:18` fetches a fresh futures mark for position rows; `hedge_engine.py:241` uses the engine's earlier `last_futures_mark` for cards. Reproduced entry 60,000, fresh mark 60,050, unchanged option: table +50; card 0. The route also mutates `hedge_engine.option_quotes` at `:44`. |
| RC03 | Straddle can display current-preview PnL for a completed session | `dashboard_routes.py:24/:111` returns the latest session as active regardless of status. `app.js:148/:256` treats any returned session as active. Its `:253/:254` falls back from held marks to live preview marks. Once a closed session remains on screen and strike/expiry previews move, those quotes can be applied to the old entry. Zero is also treated as missing by the `>0` fallback. |
| RC04 | Straddle can miss a price touch | `straddle_engine.py:471/:504` samples the futures mark, not executable order-book prices. The loop sleeps at `:860`; `binance_client.py` caches futures marks for two seconds. Sequential HTTP requests extend the gap. A touch between observations is never recorded. The displayed spot/last price is also not necessarily the futures mark used for the condition. No persisted market-event history proves an individual missed touch. |
| RC05 | Unrelated missing data prevents limit management | `straddle_engine.py:270/:282` requires spot and both option marks before checking working futures orders. Even a valid crossing futures quote cannot fill the order if an earlier fetch raises. The previously reproduced relative-time rollover also permits overdue orders to fill after the daily boundary. |
| RC06 | Order records are being used as fills | Both engines insert/update FILLED order rows directly but never instantiate their imported fill models. Read-only inspection found 19 straddle orders and 9 hedge orders, with zero rows in both fill tables. Current fill schemas lack order foreign keys; hedge fills do not even identify the instrument. Original limit price, execution price and execution time are not reliably separable. |
| RC07 | Session summaries cannot reconstruct multiple futures lifecycles | `HedgeSession` has only bull_entry/bear_entry and bull_exit/bear_exit. Re-entry adds another position/fill but does not create a new historical entry field; subsequent close overwrites the exit summary. Use FIFO fill matches for realized PnL and re-entry history, never these summary columns. |
| RC08 | Zero means several incompatible things | Numeric fields default to 0; UI expressions use `value || 0`. A straddle futures entry of 0 before any futures fill means no position, not a zero-cost position. `report_routes.py:37` always exports bull_entry, so a bearish session exports 0 despite a valid bear_entry. Current database inspection found no FILLED order with nonpositive price and no open hedge position. The particular reported zero average-cost incident is not reproduced from the available rows. |
| RC09 | Monetary rounding and balance models differ | SQLAlchemy Float is used throughout; straddle ledger totals may remain unrounded while session PnL is rounded to two decimals, whereas hedge rounds newly realized amounts at closing events. Example: straddle session 1 stores PnL -2269.93, versus signed filled-order total about -2269.933 and ledger total about -2269.933. All five available completed-session order totals match their stored session PnL when rounded to cents; a larger historical discrepancy is not established by this database. |
| RC10 | Entry valuations can temporarily omit held assets | Straddle initializes held option marks before it creates the session. Reproduced immediately after buying 200 of options: cash 99,800, active option marks both 0. The dashboard uses those marks for wallet equity, understating equity until another successful tick. |

3. Contract identity and quote contract

Persist an immutable instrument_id at order submission; every fill, lot, matching allocation and price observation references it. For the example, the canonical instrument is:

```json
{"venue":"BINANCE","market":"USDT_PREMIUM_OPTION",
 "exchange_symbol":"BTC-260909-70500-C","underlying":"BTCUSDT",
 "option_right":"CALL","strike":"70500","quote_currency":"USDT",
 "settlement_currency":"USDT","expiry_at":"2026-09-09T08:00:00Z"}
```

Verify the actual listed expiry timestamp, multiplier and exchange symbol from instrument metadata; this timestamp matches the repository's stated 13:30 IST expiry convention. Never infer contract identity from a display label such as `70500CE`, a strike alone or a session date. The Binance exchange-information response exposes expiry, symbol, strike, unit and price/quantity filters: [official metadata documentation](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-options/api/rest-api/market-data).

Valuation operation:

```text
mark_position(lot, snapshot):
  instrument = instruments[lot.instrument_id]
  quote = snapshot.latest_valid_mark_by_instrument_id[lot.instrument_id]
  require quote.instrument_id == lot.instrument_id
  require exact venue + market + exchange_symbol match
  require finite mark >= 0; futures mark > 0
  require exchange_at <= snapshot.as_of
  require snapshot.as_of - exchange_at <= max_mark_age
  require no source-sequence rollback or unresolved stream gap
  if expired: require settlement workflow, not a new expiry's live mark
  if any requirement fails: return mark=null, upnl=null, reason
```

Initial configured freshness limits: futures marks 3 seconds, option marks 30 seconds, execution-book events 1 second; persist limits in the session configuration. A fresh HTTP response does not refresh an old exchange quote. Where the source has no per-contract exchange timestamp, record that limitation, use receipt freshness explicitly, and do not claim exchange-time freshness. All components in one response use one snapshot_id, one immutable set of observations and one database read version; reject/retry valuation if wallet_version changes before publication. A snapshot may include different quote timestamps, but each must satisfy its age limit; return those timestamps.

Nearest-ITM selection is callable only when creating a new entry candidate. It is never callable from mark_position, close_position, settlement, reporting or reconciliation. A missing held quote means unavailable, not zero and not another contract. A genuine zero option mark is valid for valuation; a zero expiry payout is valid settlement. An ordinary opening fill at zero is rejected.

4. Exact schema and field semantics

Execute the supplied SQL only as a versioned migration into a new target database. It defines every column, primary key, type, foreign key, check and core index for:

| Table | Purpose |
|---|---|
| wallets | Paper/live wallet identity and settlement currency |
| instruments | Immutable full contract identity, multiplier, expiry and tick/quantity rules |
| engine_runs | Source hash, source path, process and database identity for incident attribution |
| strategy_sessions | Trader role, frozen config, dated absolute deadlines and durable state |
| session_events | State changes, TP ranks, commands, expiry and recovery events |
| market_events | Exact-contract marks, order-book/trade evidence and settlement observations |
| orders | Immutable submitted quantity, limit/stop prices and durable order identity |
| order_events | Submission, acknowledgment, fills, cancel requests/acks and rejection history |
| fills | One execution per idempotency key with order/instrument link, price, quantity and fees |
| position_lots | One opening fill per lot; remaining quantity and cost are rebuildable projections |
| lot_matches | FIFO allocation of each closing fill to opening lots and realized PnL |
| journal | Balanced cash/basis/realized/capital events, with independent economic provenance |
| wallet_balances | Rebuildable cached balances, reservations and version |
| valuation_snapshots | One coherent wallet valuation and quality state |
| position_marks | Exact lot/contract/quote linkage used by each valuation |
| daily_states | Versioned daily opening/closing balances and reconciliation proof |
| reconciliation_issues | Unresolved differences with evidence; no silent adjustment |

Database roles: the writer may append to source tables and update projections through transactional procedures only; API/UI roles are read-only. Revoke UPDATE/DELETE on instruments, market_events, fills, lot_matches, journal, order_events and session_events from ordinary application roles. Migration/correction procedures use separately controlled privileges. Mutable order/session states and remaining-lot quantities are projections of immutable events, not the sole source of truth.

Mandatory cross-row validations in the writer transaction, in addition to DDL constraints:

```text
fill.order.instrument == fill.instrument
opening lot session/instrument/side == opening fill's order session/instrument/side
closing match session/instrument/side == opening lot's session/instrument/side
sum(effective fill qty for order) <= submitted order qty
sum(matches for closing fill) == closing fill qty
sum(matches for opening lot) <= opened lot qty
filled order status <=> effective filled quantity == requested quantity
remaining quantity == opened quantity - matched closed quantity
remaining option basis == initial basis - released basis
all lot basis belongs to the same wallet as the journal event
every effective fill has exactly one FILL journal event, including a zero-delta futures opening
every FILL journal gross realized == sum(lot matches gross realized for that fill)
no opening lot on CLOSE/SETTLE; no closing allocation to another session/contract
futures lots have zero option basis; options in this version are LONG only
no ordinary fill price <= 0; settlement zero allowed
tick and quantity-step compliance; valid side/position-side/intent combination
```

An order is not a fill. Filled quantity and average execution price are views of executions, not fields populated from a preview or submitted limit. Re-entry is a new order and a new lot. Multiple partial fills create separate opening lots. Original limit_price is never overwritten by a fill.

5. Decimal and rounding contract

Use PostgreSQL NUMERIC and Python Decimal initialized from source strings; Decimal context precision at least 100 for the declared numeric ranges. JSON monetary fields are strings. Reject nonfinite values and precision overflow before database insertion. Prices and quantities have up to 12 decimals; posted USDT amounts have 8 decimals. Round posted money once using ROUND_HALF_EVEN to 0.00000001. UI two-decimal rounding is display-only. Validate that instrument precision fits the schema; otherwise migrate precision before enabling the instrument. PostgreSQL documents NUMERIC as exact, unlike floating-point types: [numeric types](https://www.postgresql.org/docs/16/datatype-numeric.html).

Use FIFO cost allocation. Allocate a fill's rounded total cash proceeds/cost across matched lots in FIFO order; put the final atomic rounding residual on the last allocation. For a partially closed option lot, allocate its original recorded cost proportionately; on its final close release the exact remaining recorded basis. Never repeatedly round a weighted average and use it as the cost source. Store the rounded allocation actually posted; recompute by the same deterministic policy.

In this version accept only USDT-settled fees/rebates; preserve fee source values, with fee_conversion_rate=1. Non-USDT fees require additional currency subledgers and verified conversion observations before enabling that route. Do not invent a USDT debit while ignoring the actual fee asset.

6. PnL formulas

Let q be exchange contract quantity, m the instrument's immutable BTC-per-contract multiplier, E the opening execution price, X the closing execution price, M the exact held-contract mark, and d=+1 for LONG or -1 for SHORT.

```text
effective BTC quantity = q * m
gross realized futures PnL = d * q_closed * m * (X - E)
unrealized futures PnL = sum_over_open_lots[d * q_remaining * m * (M - E)]
```

For long CALL and PUT options, price-difference PnL uses the same LONG sign. A put does not reverse the PnL sign merely because its underlying exposure is bearish.

```text
opening option basis = round_money(q * m * E)
option close cash proceeds = round_money(q_closed * m * X)
gross realized option PnL = allocated close proceeds - released option basis
unrealized option PnL = round_money(sum(q_remaining * m * M)) - remaining option basis
```

Compute the marked value at the displayed position aggregation level; distribute atomic residuals to lots deterministically so lot, position, session and wallet totals agree. For an unrounded, single-fill example this reduces to (M-E)*q*m.

```text
net realized PnL = gross realized futures + gross realized options
                 - fees + signed funding income
session total PnL = session net realized PnL + session unrealized PnL
portfolio total PnL = sum(session totals), excluding deposits/withdrawals
average entry price = sum(remaining lot qty * multiplier * entry price)
                      / sum(remaining lot qty * multiplier)
average order fill price = sum(fill qty * fill price) / sum(fill qty)
```

No fills means average fill price NULL. No remaining position means average entry NULL. Show `Pending` or `Closed`, not 0. Fees are realized expenses when incurred, including entry fees; do not include them again in option basis or deduct them again on closing. Do not multiply PnL by leverage. Funding is the signed actual funding cash event, linked to the appropriate exposure/time; never add an estimated funding payment to realized PnL.

At expiry, use the verified exchange settlement value for this instrument. If computing a payout from a verified underlying settlement index under the instrument's contract rules:

```text
call payout per underlying unit = max(settlement_index - strike, 0)
put payout per underlying unit = max(strike - settlement_index, 0)
settlement cash = round_money(payout * remaining_qty * multiplier)
option realized PnL = settlement cash - remaining option basis
```

Record a SETTLEMENT order/fill tied to the exact instrument and settlement evidence, including a valid zero payout. Until verified, state is AWAITING_SETTLEMENT and valuation is unavailable; no futures re-entry against the expired option.

Example: buy 1 BTC-equivalent `BTC-260909-70500-C` at 450. If that exact contract marks 510, unrealized PnL is +60 USDT. A 70,750 call or a September 10 70,500 call is inadmissible even if its mark is available. Close 0.4 at 500: released basis 180, proceeds 200, realized +20; remaining basis 270. Remaining 0.6 marked at 510 contributes +36 unrealized. Total PnL is +56 before fees. Opening cash 100,000 becomes 99,750, book balance 100,020, equity 100,056.

7. Journal postings and reconciliation proof

Each row in the journal obeys:

```text
cash_delta + option_basis_delta = net_realized_pnl + net_deposit
net_realized_pnl = gross_realized_pnl - fee_expense + funding_income
```

| Event | Cash delta | Option basis delta | Net realized | Net deposits |
|---|---:|---:|---:|---:|
| Long option buy, cost C, fee f | -C-f | +C | -f | 0 |
| Long option sell/settle, proceeds V, basis C, fee f | V-f | -C | V-C-f | 0 |
| Futures opening, fee f | -f | 0 | -f | 0 |
| Futures close, gross PnL P, fee f | P-f | 0 | P-f | 0 |
| Funding receipt/payment F, signed | F | 0 | F | 0 |
| Deposit/withdrawal D, signed | D | 0 | 0 | D |

Create each wallet at zero and append a genesis DEPOSIT for its verified initial capital. Never seed a balance without a corresponding capital event. Internal transfers have two journal rows in one database transaction and opposite net_deposit amounts; consolidate them to zero across the two wallets.

For any interval [t0,t1), sum the per-event invariant:

```text
EndingBookBalance = StartingBookBalance + sum(NetRealizedPnL) + sum(NetDeposits)

EndingCash = StartingCash + sum(NetRealizedPnL) + sum(NetDeposits)
             - (EndingOptionBasis - StartingOptionBasis)

EndingEquity - StartingEquity
  = sum(NetRealizedPnL) + sum(NetDeposits)
    + (EndingUnrealizedPnL - StartingUnrealizedPnL)
```

Reconciliation transaction, after every execution and at daily close:

```text
1. Freeze a consistent database snapshot/watermark; lock the wallet for writes.
2. Deduplicate source fills, funding and external cash events by their unique IDs.
3. Rebuild executed quantities and FIFO matches from immutable effective fills.
4. Recompute realized PnL independently from fill prices, quantities and matching policy.
5. Check lot residual quantities/basis against position projections.
6. Recompute journal cash/basis/PnL/deposit totals from zero/genesis.
7. Compare recomputed cash and basis separately with wallet_balances.
8. Compare every fill's gross PnL and fees to its journal row; require one row per fill.
9. Compare session realized totals with journal attribution; rebuild session summaries.
10. Verify EndingBook - StartingBook - NetRealized - NetDeposits == Decimal('0').
11. Build one exact-contract valuation snapshot; verify equity = book + unrealized.
12. For live mode, separately match exchange orders/fills, cash movements, inventory
    and balances at a common completed watermark; account for pending events explicitly.
13. Commit new balances, daily proof and source-event links only after internal checks pass.
```

No floating tolerance such as abs(error)<0.01; equality is exact in posted USDT atoms. A nonzero difference produces QUARANTINED and a reconciliation_issues record with expected/actual values. Block new exposure while preserving the ability to cancel orders and record verified risk-reducing fills. Missing prices invalidate unrealized PnL/equity but do not prevent book-balance reconciliation. Never turn an unexplained difference into a deposit or adjust a prior trade to force a zero. Correct a proven error with a referenced reversal and replacement, replay affected projections, and issue a new daily revision. Late exchange events are included in a new revision of their effective trading day.

8. Execution and limit-order state machine

Use an event-driven execution worker; UI polling is not an execution clock. For paper execution, conservative marketable-limit rules are:

```text
BUY LIMIT L: eligible when executable best_ask <= L
SELL LIMIT L: eligible when executable best_bid >= L
execution price must be <= L for BUY, >= L for SELL
fill quantity cannot exceed remaining order quantity or available eligible depth
```

A mark/last price touching the limit is not itself evidence of executable liquidity. Consume fresh book events continuously, store each event used for a fill and consume simulated available depth once across competing paper orders. Resting maker orders require an explicit trade/queue simulation; book-touch alone cannot prove their real fill. In live mode only exchange execution reports create fills; market observations never fabricate them. A stop order has a separate stop price/reference; stop activation is not a fill.

```text
NEW -> ACKNOWLEDGED -> PARTIALLY_FILLED -> FILLED
ACKNOWLEDGED/PARTIALLY_FILLED -> CANCEL_PENDING -> CANCELLED
ACKNOWLEDGED/PARTIALLY_FILLED -> EXPIRED
NEW -> REJECTED
```

Process fills occurring before a confirmed cancellation even if delivered later. CANCEL_PENDING is not CANCELLED. Deadline comparisons use absolute UTC timestamps frozen for the held session, never clock-only modulo arithmetic. No synthetic paper fill at/after expires_at. Before any decision after restart, rebuild pending orders and lots, cancel overdue orders, process verified missing fills, and handle overdue exits. Do not backfill simulated fills from candles or invent missed events.

On the first partial OCO fill, atomically prevent the sibling from accepting further simulated exposure. In live mode send its cancellation and continue accepting real fills until cancellation is confirmed; if both actually fill, record both and handle exposure instead of discarding an inconvenient fill. OCO state changes, fill, position updates and journal posting use the same writer transaction. An OPEN command cannot be retried with a new client order ID; retry the existing idempotent intent.

Futures limit execution depends on that futures instrument's executable quote, order status, risk reservation and deadline. It does not depend on the entry candidate's spot price or option preview. Existing hedge protection expiry is a separate risk gate: never initiate or re-enter futures after that protection expires. Missing option prices must not block recording a genuine futures execution or a verified reduce-only futures close.

9. Physical storage and folder layout

Production reference: PostgreSQL with the supplied NUMERIC schema. One writer service owns execution and accounting; it locks a wallet row with SELECT FOR UPDATE before posting and serializes OCO/session decisions. Multiple API workers may read. Use database transactions for multi-leg records; real exchange orders remain independent external events and are reconciled rather than assumed atomic.

```text
HnS_application_11aug/
  app/
    execution/       # orders, exchange adapters, OCO state, event ingestion
    accounting/      # Decimal formulas, FIFO, journal posting, reconciliation
    valuation/       # instrument-ID marks and immutable snapshot API
  migrations/       # versioned PostgreSQL migrations
  docs/
    trading_accounting_schema.sql
    TRADING_ACCOUNTING_BLUEPRINT.md
  tests/
    execution/
    accounting/
    valuation/

D:/HnSData/
  raw/venue=BINANCE/date=YYYY-MM-DD/stream=<name>/part-<uuid>.jsonl.zst
  market/venue=BINANCE/date=YYYY-MM-DD/hour=HH/part-<uuid>.parquet
  exports/fills/date=YYYY-MM-DD/part-<uuid>.parquet
  exports/journal/date=YYYY-MM-DD/part-<uuid>.parquet
  daily/wallet=<uuid>/date=YYYY-MM-DD/revision=0001/proof.json
  daily/wallet=<uuid>/date=YYYY-MM-DD/revision=0001/positions.parquet
  backups/postgres/base/<timestamp>/
  backups/postgres/wal/
  incidents/<issue-uuid>/evidence.json
  manifests/<archive-uuid>.json
```

PostgreSQL owns its data directory outside the repository. Raw JSONL preserves exchange payloads and decimal strings. Parquet stores DECIMAL(38,12) prices/quantities and DECIMAL(38,8) money, UTC timestamps and full instrument_id. CSV is human export only. Manifests contain schema version, row count, event-time range and SHA-256. Write exports to a temporary file, fsync, then atomically rename; publish the manifest only after finalization. Export failures must not roll back already committed trades. Retain database references/evidence needed by fills and valuation snapshots even when bulk market events are archived.

Current SQLite can remain a single-writer paper implementation only with an adapted schema: STRICT integer monetary atoms or canonical decimal strings, Python Decimal arithmetic, foreign_keys=ON, WAL, synchronous=FULL and BEGIN IMMEDIATE for posting. Do not assume declaring DECIMAL in SQLite produces PostgreSQL-style exact arithmetic. Avoid SQLite REAL and SUM over financial text casts. Do not run the supplied PostgreSQL DDL directly in SQLite. Use SQLite's consistent backup API for migration snapshots; do not copy only an active .db while ignoring its WAL.

Daily accounting boundaries are 00:00 Asia/Kolkata to the next midnight, converted to UTC and stored explicitly. Expiry-session dates and execution deadlines are separate fields. Raw archives partition by UTC date, while daily accounting proof uses the stated IST trading date.

10. API and UI contract

`GET /snapshot` returns versioned, backend-computed decimal strings. Each position includes session_id, instrument_id, full exchange_symbol, expiry_at, strike, right, side, remaining_qty, average_entry, mark, mark_event_id, quote_exchange_at, quote_received_at, realized_net, unrealized, total_pnl and quality. Every aggregate carries snapshot_id and as_of. Preview candidates live under `entry_candidates` and cannot populate `positions`.

Return active sessions only when positions or working orders remain; return completed sessions under history. Closed sessions show their settled PnL and execution prices, not live option marks. The frontend formats strings only; it cannot calculate an alternative PnL, pick another strike, infer futures direction from TP, replace null with 0, or infer average cost from submitted limit prices. Show option-only, futures-only, pending and awaiting-settlement phases explicitly. Stale/missing quotes display unavailable with the last observation timestamp, never a fresh-looking profit.

11. Migration and acceptance gates

1. Take a consistent read-only source snapshot and record source database hash/build identity. Do not erase or reset histories.
2. Map every recorded full option symbol to verified instrument metadata. Quarantine ambiguous/missing identities.
3. Import historical FILLED orders as explicitly labelled LEGACY_INFERRED executions only where price, quantity, side and contract are unambiguous. Order creation time is not proof of fill time; preserve that uncertainty in source evidence. Do not invent missing partial fills or fees.
4. Reconstruct FIFO lots and realized PnL; compare against existing session and ledger totals. Preserve original values and classify decimal rounding differences separately from economic differences.
5. Establish verified opening capital and external cash movements; unresolved opening balances prevent a certified historical reconciliation. A verified cutover balance can start a new accounting epoch without falsely certifying older history.
6. Replay into the new schema, produce independent reconciliation proofs, and run paper shadow valuation against identical immutable quotes before switching the UI.

Required automated acceptance cases:

```text
held 70500 CALL Sep 9 ignores nearer strikes and Sep 10 prices
missing exact quote yields NULL UPNL; valid zero option mark yields a real loss
same snapshot gives identical card/table/session/wallet totals
pending order has NULL average cost; positive fills produce weighted average cost
bearish report uses actual matched futures entry, not bull_entry=0
partial entry/close, FIFO across prices, re-entry and final settlement reconcile exactly
entry fees, exit fees, rebates, funding, deposits and withdrawals reconcile exactly
duplicate/out-of-order fills and crash/restart replay never duplicate positions or money
BUY/SELL limits obey bid/ask executable-side inequalities and depth limits
inter-poll book crossing is processed; mark-only touch does not manufacture a fill
deadline rollover, stale quotes, OCO race and spot outage cannot create late exposure
option purchase moves cash to basis with unchanged book balance before fees
unknown legacy entry or settlement is quarantined, never replaced by zero
every daily proof equals zero in USDT atoms; deliberately corrupted data fails certification
```
