Binance live trading readiness and implementation report

Assessment date: 2026-09-09. Scope: current HnS straddle and hedge application, BTC USDT perpetual futures and premium-paid BTC options. This report does not enable live trading, install credentials, transfer funds or submit exchange orders.

**1. Decision and readiness rating**

**Current live-money readiness: 2/10 — do not enable unattended real-money trading in the current implementation.** This is an engineering assessment, not a probability of profit or a prediction of investment performance.

The application currently fetches public market prices and simulates executions by inserting/updating local FILLED orders. Adding an API key or changing a paper-trading setting is insufficient. There is no implemented end-to-end exchange order lifecycle, fill reconciliation, or production account-risk control in the reviewed engines.

| Capability | Maximum points | Current points | Evidence |
|---|---:|---:|---|
| Authenticated live order submission and account synchronization | 25 | 0 | binance_client.py implements public price requests, not authenticated trading |
| Execution state, partial fills, retries and OCO recovery | 20 | 4 | Local order states and restart logic exist; exchange execution lifecycle is absent |
| Contract identity and coherent market valuation | 15 | 7 | Held options use exact symbols; display snapshots and fallback inputs still disagree |
| Fill-based accounting and reconciliation | 15 | 3 | Local PnL/ledgers exist; Float arithmetic and unused fill tables remain |
| Account margin, exposure limits and expiry handling | 15 | 3 | Some quantity/deadline controls; material expiry and cash-validation gaps |
| Security, deployment, monitoring and acceptance evidence | 10 | 3 | Offline regressions exist; live failure recovery and production security are unproven |
| Total | 100 | 20 | All critical release gates below remain mandatory |

Recent offline checks passed 36 trading regression tests. Separate review reproductions demonstrated failures outside that suite. Passing offline tests establishes only the behaviors they cover; it does not certify real executions or profitability.

**2. Current code evidence and blockers**

| Area | Current implementation | Required before live |
|---|---|---|
| Execution | app/core/hedge_engine.py:573 and :589 record option/futures orders as FILLED immediately; straddle_engine.py:381/:393 does the same for options | Exchange acknowledgment must not create a filled position; only confirmed executions do |
| Futures limits | straddle_engine.py:471/:504 checks sampled futures marks and changes order status | Submit actual orders and consume execution reports; a chart touch is not a fill |
| Hedge target | hedge_engine.py:739 checks one options snapshot; :761/:762 fetches another for execution | Preserve the order limit and use confirmed fill prices; never overwrite the limit |
| Valuation | dashboard_routes.py:18 and hedge_engine.py:241 use different futures mark snapshots | One immutable snapshot per response and exact instrument routing |
| Straddle display | app.js:253 falls back to preview marks; latest session is returned as active | Separate held positions from entry candidates and completed history |
| Cost/quantity | Hedge card can replace invalid entry with memory and reuse option quantity for futures PnL | Per-leg fill-derived quantity/cost, no silent substitutions |
| Restart/expiry | Clock-relative straddle deadline rollover and hedge re-entry against expired protection were reproduced | Persist absolute UTC deadlines and prohibit new exposure against expired protection |
| Trade history | Engines import fill models but do not populate them | Durable exchange fill IDs, actual fees and FIFO close allocations |
| Wallet | Paper balance stored in configuration; absent balance can default to 100,000 | Exchange-reconciled cash journal; missing data must block new exposure |
| Reset | Dashboard reset routes delete trading history | Paper-only reset; live history is append-only and live positions cannot be erased locally |

The [accounting blueprint](TRADING_ACCOUNTING_BLUEPRINT.md) and [reference schema](trading_accounting_schema.sql) are design artifacts, not completed upgrades. Do not count them as deployed capabilities.

**3. Limit, market and conditional orders**

| Order | Meaning | Fee behavior | Main execution tradeoff |
|---|---|---|---|
| Market | Seek immediate execution against available liquidity | Normally taker | Execution price is not fixed; fills can span levels |
| Limit | BUY at your limit or lower; SELL at your limit or higher | Maker if it rests and later supplies liquidity; taker if it immediately takes liquidity | Price bounded, execution not assured |
| Post-only limit | Limit order that must not immediately take liquidity | Maker execution if accepted and later filled | A crossing order is rejected/cancelled according to endpoint rules; may remain unfilled |
| Conditional stop-market / take-profit-market | A specified reference price activates a market order | Trigger alone is not a trade; resulting execution is normally taker | Trigger price is not the guaranteed execution price |
| Conditional stop-limit / take-profit-limit | Trigger activates a limit order | Child execution can be maker or taker | May trigger and still never fill |

Maker/taker classification depends on what the execution does to liquidity, not merely whether the submitted order was called LIMIT. An ordinary marketable limit can incur taker fees. Applicable rates depend on the account/product. [Binance futures fee explanation](https://www.binance.com/en/support/faq/detail/360033544231).

Example with futures best bid 70,000 and best ask 70,001:

- BUY LIMIT 69,990 waits unless eligible sellers become available.
- BUY LIMIT 70,001 can immediately take the ask and incur a taker fee.
- BUY POST-ONLY 70,001 cannot be relied on for an immediate fill.
- A stop-market activated at 69,500 may execute below 69,500 when selling into a falling market.

These are order-mechanics examples, not proposed BTC trading levels.

**4. Binance API distinctions that matter to this application**

The current USD-M documentation separates ordinary orders (`POST /fapi/v1/order`) from conditional algo orders (`POST /fapi/v1/algoOrder`). Query/cancel ordinary and algo orders separately. GTX is available for relevant post-only futures limit paths. Position-side and reduce-only parameters depend on position mode; the documented reduceOnly field cannot simply be sent in Hedge Mode. [USD-M trade API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade).

The documented options order endpoint is `POST /eapi/v1/order`; its order-type enumeration is LIMIT, with IOC/FOK/GTC/GTX and postOnly controls documented. Therefore the program's simulated options MARKET records cannot be forwarded unchanged. For urgent options execution, design a marketable IOC limit with a bounded price; it may partially fill or leave a residual. Endpoint order responses can show avgPrice=0 when executedQty=0: display that as pending, not a zero-cost filled position. [Options trade API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-options/api/rest-api/trade).

Pin adapter behavior to the exact account product and current endpoint specification. Do not borrow Spot OCO or futures stop-order parameters for the options API. Store which contract/order capabilities were validated at startup.

**5. Which order should you pick to reduce Binance charges?**

Default policy proposal: **post-only limits for discretionary entries and patient profit-taking, bounded aggressive execution for time-critical hedging/exits, and exchange-hosted conditional protection where supported.** This is a proposed execution policy, not an implemented setting.

| Strategy operation | Proposed order policy | Required handling |
|---|---|---|
| Hedge option purchase | Post-only limit when the entry deadline permits | Do not open full futures exposure before corresponding option fills are confirmed |
| Hedge initial futures leg | Bounded IOC limit; market only under an explicit urgency policy | Hedge only confirmed option quantity; accept taker fees when needed to control legging risk |
| Straddle option purchases | Limit orders with a bounded completion window | Do not assume the two legs fill atomically; match quantities or unwind the unmatched portion |
| Straddle futures upper sell / lower buy | Resting limit orders, post-only where compatible | These are reversal entries in the current strategy, not breakout stop entries; preserve that distinction |
| Futures profit target | Resting limit if price/market conditions allow | Close only the intended allocated position; use mode-compatible protection against reversal |
| Hedge first-TP option target | Resting sell limit at the recorded target | Post-only if supported and appropriate; an already marketable target may rationally take liquidity |
| Hedge second-TP futures re-entry | Limit order at the strategy's recorded level | Cancel at deadline/option expiry; verify actual protection and remaining allowed quantity |
| Futures emergency exit | Mode-correct market or aggressive IOC limit | Market prioritizes execution; IOC bounds price but can leave exposure |
| Option emergency exit | Marketable IOC limit on the exact option | Reconcile partial fills, refresh book and handle residual explicitly; no guaranteed exit in an empty book |

Post-only on every leg is not an appropriate universal rule. Waiting for a small fee saving while the other leg is exposed can cost more than the fee. A conditional order is a trigger mechanism, not a cheaper fee class. If the account's options maker and taker rates are equal, maker-only execution may have no commission benefit for that product.

Proposed total-cost metric:

```text
execution_cost = commissions + spread/slippage + signed_funding_cost
                + applicable settlement/exercise fees
```

Estimate missed-fill and legging costs separately when comparing execution policies; they are not automatically booked cash expenses. Choose the lower expected total cost subject to the strategy's exposure and deadline constraints.

**6. Fees: obtain actual account rates, do not hardcode examples**

For BTCUSDT futures, obtain maker/taker rates through the signed `GET /fapi/v1/commissionRate` endpoint. Documentation response examples are not proof of your account's rates. Persist the rate snapshot for estimates and the actual commission/commission asset from every execution for accounting. [Commission-rate API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account).

For linear futures:

```text
fill_notional_USDT = filled_BTC_quantity * execution_price
estimated_fee = fill_notional_USDT * applicable_fee_rate
round_trip_fee = sum(all entry and exit fill commissions)
```

Illustration only: at a hypothetical maker rate of 0.02% and taker rate of 0.05%, a 10,000 USDT fill costs 2 versus 5 USDT. Two equal-notional fills cost 4 versus 10 USDT. Actual entry and exit notionals may differ. Fee calculations use traded notional, not only the margin deposited; increasing leverage does not reduce commission for a fixed notional.

Binance's options fee documentation uses an index-based calculation with a premium cap, rather than applying the futures formula to option premium. Its published form is:

```text
option_trading_fee = min(rate * index_price * contract_unit,
                        10% * option_traded_price) * option_traded_size
```

Map the documented units to the actual instrument before applying this formula; do not multiply the contract unit twice. Fetch the applicable rate for the account/product and reconcile with actual charged fees. Exercise/settlement charges, when applicable, are separate. [Options fee formula](https://www.binance.com/en/support/faq/detail/5326e5de61c34fed98abe28d2f175a23).

The public options rate page includes product-specific promotional schedules; do not apply a commodity-options promotion to BTC options. No logged-in fee tier, regional product entitlement, discount eligibility or account commission schedule was inspected in this review. [Options rate page](https://www.binance.com/en-IN/fee/optionsTrading).

If evaluating BNB fee payment or a discount program, confirm its applicability to each product and record actual fee-asset inventory/conversion. The reference single-USDT accounting schema needs extension before non-USDT fee deductions can be reconciled correctly.

**7. Required architecture and implementation sequence**

| Stage | Concrete implementation | Release evidence |
|---|---|---|
| A. Repair current computation | Remove preview/zero/current-price substitutions; per-leg quantities; coherent snapshots; correct TP/re-entry display and absolute deadlines | Previously failing cases pass with independently calculated expected results |
| B. Separate execution modes | PAPER / READ_ONLY / DEMO / LIVE adapters, distinct databases and account identities; strategies emit OrderIntent rather than fake FILLED rows | Production credentials and endpoints cannot be reached in paper/demo tests |
| C. Exchange integration | Signing, clock synchronization, account discovery, instrument filters, balance/position queries, order submit/query/cancel, execution stream | Contract tests cover success, rejects, rate limits, timeouts and unknown order status |
| D. Durable execution | Unique client IDs, submission outbox, fills, partial fills, order events, OCO ownership, independent ordinary/algo reconciliation | Retry/reconnect/crash tests create no duplicate exposure or duplicate money postings |
| E. Accounting | Exact decimals, fill-derived inventory, FIFO allocations, commission/funding/settlement/deposit events | Internal accounting differences equal zero; exchange differences are explained or quarantined |
| F. Risk engine | Account/strategy limits, cash reservations, margin checks, legging deadlines, expiry gates and kill switch | Failure injection cannot silently create unbounded/unprotected new exposure |
| G. Deployment and security | Dedicated execution worker, durable storage, monitored reconnect/recovery, least-privilege keys and protected API | Restart, backup restore and unauthorized-access tests pass |
| H. Controlled rollout | Read-only reconciliation, supported demo validation, shadow execution, then a separately approved limited live pilot | Signed-off release checklist and reconciled pilot evidence |

Suggested module interfaces:

```text
Strategy -> OrderIntent -> RiskCheck -> DurableOutbox -> ExchangeAdapter
ExchangeExecution -> IdempotentFillIngest -> PositionLots + Journal
ExactInstrumentQuotes + PositionLots -> ValuationSnapshot -> UI
ExchangeOrders/Positions/Cash -> Reconciler -> Health / ExposureGate
```

No API/UI request may change an execution quote cache or independently compute a different portfolio PnL.

**8. Multi-leg and multi-strategy account behavior**

The two hedge traders can hold opposite futures directions, and straddle also trades BTC futures. Design the actual exchange account layout before writing the adapter. Proposed approach: dedicated account/subaccount boundaries where available; otherwise a single execution allocator owns shared exchange inventory and keeps strategy-level lots. Never let independent bots blindly close an account-level position.

One-way netting can offset positions that the current paper database treats as independent. Hedge Mode distinguishes LONG/SHORT but does not by itself segregate two strategies holding the same direction. Validate the account mode and map every opening/closing intent to it. The exchange documents position mode as an account-wide setting for relevant symbols. [Position-mode and order parameters](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade).

Proposed paired-entry protocol:

1. Freeze symbol identities, quantities, maximum premium/cost and absolute deadlines.
2. Check balances, margin and available depth for the planned hedge and its contingency unwind.
3. Reserve capital; submit uniquely identified option order(s).
4. On each confirmed option fill, hedge only the corresponding filled exposure under the selected execution policy.
5. If a sibling leg or hedge cannot complete by its deadline, cancel remaining orders, reconcile cancellations/fills and execute the predefined unwind or residual-position policy.
6. Keep recording late real fills. A cancellation request does not prove the order is cancelled.
7. Activate the next strategy phase from actual fills, not from order acknowledgment or a price touch.

For straddle, track unmatched call/put quantities explicitly; do not arm full-size futures OCO orders based on requested option quantities. For synthetic OCO, handle the possibility that both futures orders fill before cancellation; no cross-product atomicity is assumed.

**9. Risk, expiry and operational controls**

Mandatory explicit parameters before any live session:

```text
allowed_accounts, allowed_instruments, max_order_notional
max_strategy_notional, max_account_gross_exposure
max_unhedged_BTC, max_legging_duration
max_daily_loss, max_margin_utilization, minimum_available_balance
max_spread, max_quote_age, max_execution_slippage
entry_deadline, order_expiry, protection_expiry, force_close_deadline
max_retry_count, max_unknown_order_duration
```

Validate finite/range values and freeze applicable parameters per session. These limits must be selected against the actual account and strategy; this report does not invent a safe leverage or deposit amount.

A bought option does not automatically prevent a separately margined futures position from liquidation. Account collateral and margin treatment must be verified independently. Equal BTC quantities do not necessarily make a futures-plus-option position delta-neutral. Do not use the old document's premium-only loss description as an enforced account loss limit.

At option expiry: block new hedges/re-entry using that option, cancel associated outstanding exposure-increasing orders, ingest verified settlement, and manage any surviving futures position. A vanished quote is not a settlement price.

Separate controls:

- Pause entries: reject new exposure, keep managing existing positions.
- Cancel orders: request and confirm cancellations in both ordinary and algo services.
- Flatten: close verified remaining exposure using the configured urgency policy; monitor until reconciled.
- Reset simulation: permitted only against a paper database. Never present a live history deletion as squareoff.

A process crash must not remove exchange-hosted protection. Reconnect starts in reconciliation mode; no new trades until unknown orders and account exposure are resolved. Use alerts for stale data, orphan fills, failed cancels, approaching expiry, low margin, reconciliation differences and worker heartbeat failure.

**10. Security and production deployment**

The current application includes development-style default secrets/seed credentials and a basic password hashing implementation. Replace these for production; do not expose their values in logs or reports. Authenticate and authorize financial read endpoints as well as mutations. Protect sessions, configure TLS and restrict network access.

Store exchange credentials in an OS secret store or secrets manager. Use a dedicated trading key, withdrawal permission disabled, IP restrictions where supported, and rotation/revocation procedures. Restrict product/account scope to what the adapter requires. Verify product access in the actual account and jurisdiction; location in an IDE is not evidence of exchange eligibility.

Run one execution owner with a lease/fencing mechanism, separate from web workers. main.py currently starts engines inside application startup; multiple server workers could therefore start competing engines. Disable development auto-reload for production. Use durable transactional storage, consistent backups and tested restoration. Neither a notebook nor a browser tab is the execution supervisor.

**11. Accounting acceptance and live release gates**

Use the linked accounting blueprint, including its distinction between cash, option cost basis and equity:

```text
net_realized = gross_realized - fees + signed_funding_income
book_balance = cash + remaining_long_option_cost_basis
ending_book = starting_book + period_net_realized + period_net_deposits
equity = book_balance + unrealized_pnl
```

Live account reconciliation additionally covers collateral/margin transfers, settlement, fee assets and shared-position allocation. Book-balance equality alone is not proof that every exchange position has been captured.

Hard release gates:

- All reported contract, quantity, entry-price, snapshot, deadline and target-fill failures are addressed.
- No locally synthesized live fills; positive ordinary fill prices and exact instrument identity validated.
- Partial entries/exits, duplicate messages, out-of-order events and restart replay reconcile exactly.
- An HTTP timeout after submission queries the original client ID; it does not create a replacement order blindly.
- Forced disconnect, API ban, rejected protection, missing option liquidity and simultaneous OCO fills have tested outcomes.
- Exchange balances, outstanding orders, executed trades and allocated positions agree after recovery.
- Actual maker/taker fees, fee assets, funding and settlement are accounted for without double counting.
- Live reset is impossible; risk-reducing operation remains available when entries are halted.
- The configured maximum exposure and account loss controls are independently exercised.
- A separate performance evaluation demonstrates positive net expectancy after realistic costs on unseen periods. Operational readiness does not establish profitability.

Use Binance-supported demo/test environments for the applicable products. Futures demo support does not establish equivalent options coverage or realistic options liquidity; confirm each adapter's supported environment. If an options sandbox is unavailable, use deterministic adapter simulation plus read-only production observation, and disclose the remaining validation gap before any funded pilot. [Binance testing guidance](https://www.binance.com/en-AU/support/faq/detail/ab78f9a1b8824cf0a106b4229c76496d).

Proposed rollout: read-only account reconciliation -> demo/failure tests -> production-market shadow run -> separately authorized minimum-size live pilot within explicit limits -> increase only after reconciliation and execution-quality review. The live pilot is a release stage, not an action authorized or performed by this report.

**12. Information still required for implementation**

Before configuring live operation, record the eligible Binance account/product, position and margin mode, actual fee schedule, whether subaccounts are available, allowed instruments, initial capital allocation, maximum tolerable exposure/loss, and the residual-leg/urgent-exit policy. No authenticated Binance account was inspected, so these remain unverified.

Recommended first implementation milestone: correct the existing computation gaps and build a fill-driven exchange adapter with independent reconciliation. Adding credentials before those pieces exist would not turn this simulator into a reliable live trading system.
