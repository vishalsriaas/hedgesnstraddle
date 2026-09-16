Trading logic review — 2026-09-08

Reviewed the current working-tree straddle and hedge engines, their configuration and dashboard routes, quote helpers, existing regression tests, and the local hedge algorithm document. This is a paper-execution code review. No live engine, exchange order, or working trading database was used.

The existing suite ran 41 tests: 40 passed and one optional MCP protocol test was skipped. Eight additional safety expectations failed, confirming the six findings below. Production code was not changed.

1. **[P1] Straddle forgets overdue deadlines after the daily clock rollover.**

   Locations: `app/core/straddle_engine.py:18`, `:458`, `:467`, `:742`.

   Cutoff and squareoff compare minutes within a repeating 13:31-to-13:30 cycle, without anchoring deadlines to the held session's date. Reproduction: enter at 06:00 with cutoff 11:00 and squareoff 12:30, then resume at 14:00 with a futures mark of 60,300. The old 60,200 short limit becomes FILLED and the session remains Open. Both deadlines have passed, but the relative clock now makes them appear to be in the future. The test supplies exact-contract marks to isolate the scheduling defect; a missing expired quote instead prevents management earlier in the loop.

   Fix direction: persist absolute session deadlines; process overdue exits and cancel overdue orders before evaluating any entry or TP transitions. An expired or overdue session must not acquire a new futures leg.

2. **[P1] Hedge can re-enter futures after its protective option expires.**

   Locations: `app/core/hedge_engine.py:742`, `:880`; default second-trader close is `app/database.py:137`.

   The re-entry branch checks only the futures price and the presence of an option database row. It does not check whether the held option is still unexpired. Reproduction: establish the two slots, trigger their initial futures TPs, then advance to 14:00 with no option quotes and a qualifying futures price. Trader 2's REENTRY_LIMIT fills even though its option expired at the system's 13:30 boundary. Its configured 17:00 close permits this path. The database still lists the expired option as protection.

   Missing exact-contract quotes also cause squareoff to defer the entire session, so simply waiting until 17:00 does not resolve an option that has disappeared from the feed.

   Fix direction: cancel re-entry orders at option expiry, prohibit futures re-entry against expired protection, and implement explicit expiry settlement using a verified settlement source. Do not invent a settlement price or treat an expired option row as an active hedge.

3. **[P1] A spot-feed outage blocks exits that have all required execution prices.**

   Locations: `app/core/straddle_engine.py:270`, `app/core/hedge_engine.py:839`.

   Both loops fetch spot before managing held positions. A spot exception therefore prevents straddle manual squareoff and hedge scheduled squareoff even when futures and exact option marks are available. Both scenarios were reproduced with valid mocked execution marks: sessions remained Open. Hedge's queued manual-squareoff branch runs earlier and is not affected by this particular spot dependency.

   Fix direction: separate entry/preview data dependencies from held-position management. Exit processing should require only prices for the actual held legs.

4. **[P1] Hedge fills an option sell target below its target price.**

   Locations: `app/core/hedge_engine.py:739`, `:763`, `:788`.

   Target detection uses `self.option_quotes`, but `execute_squareoff` fetches a second snapshot and fills without checking the limit again. Reproduction: an option bought for 100 has a sell target of 200; detection sees 220, the execution fetch returns 100, and the target becomes FILLED at 100. The close routine also overwrites the target's original price, obscuring the violation in the order record.

   Fix direction: use a single execution snapshot or revalidate the sell limit against the execution quote. Preserve the limit price separately from the fill price. Never manufacture a favorable fill when the execution quote does not meet the limit.

5. **[P1] Invalid hedge configuration can stop management of both trader slots.**

   Locations: `app/api/config_routes.py:178`, `app/core/hedge_engine.py:880`.

   The configuration route accepts and commits `force_close_h=25`. On the next active tick, `datetime.replace(hour=25)` raises ValueError before the per-slot exception handler. With Trader 1 active, the loop exits before either slot completes management. An isolated route call confirmed that 25 persists; a separate tick reproduction raised `hour must be in 0..23, not 25` and left the session Open.

   Fix direction: validate hours, minutes, finite numeric values, directions and schedule consistency before committing configuration. Isolate management failures by slot and use validated deadlines for existing positions.

6. **[P2] Entry ignores available paper capital and a configured balance guard.**

   Locations: `app/core/straddle_engine.py:428`, `app/core/hedge_engine.py:525`; `MIN_PAPER_BALANCE` default at `app/database.py:91`.

   Straddle buys 200 worth of options with a wallet of 50, leaving -150. Hedge opens a position with wallet 0 and `MIN_PAPER_BALANCE=1000`. Hedge checks quantity limits but never reads this balance guard. These are separate reproductions under the same missing-capital-check finding.

   Fix direction: enforce available cash before straddle option purchases and enforce or explicitly retire the hedge minimum-balance setting. Preserve the documented hedge PnL-based wallet convention; this finding does not ask to reintroduce the deliberately removed global option-spend cap or add futures margin to the testing model.

The additional tests are in [review_trading_edge_cases.py](review_trading_edge_cases.py). Run them explicitly:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p review_trading_edge_cases.py -v
```

Expected result on the reviewed code: eight assertion failures. The filename intentionally falls outside default `test*.py` discovery so the review reproductions do not silently change the existing suite. Once the corresponding defects are fixed, these should pass and can be moved into normal regression coverage.

Scope notes: the newer tests/README rules supersede older document rules on minimum-TV selection, opposite trader direction and removal of the strike-clash/global-spend constraints. Those intentional changes are not reported as bugs. Mark-based simulated fills and zero fees are also documented testing assumptions. This review establishes the failures above; it does not establish profitability or live-exchange execution correctness.
