# Implementation Plan: Fix Recovery Window Logic and Missing SELL Orders

This document outlines the proposed code changes to fix the broken Recovery Window logic and ensure all closed positions correctly log opposing SELL trade orders in the database.

## User Review Required

> [!IMPORTANT]
> **Review Required**: Please review this plan to confirm it perfectly aligns with your trading workflow. **No code has been changed yet**. Execution will begin once you click "Proceed" or explicitly approve.

## Open Questions
- For the Recovery target (80%), should the reference be the `sess.net_straddle_ask` (the exact entry combined premium recorded in the DB) or the live `self.combined_premium` calculation? I will use `sess.net_straddle_ask` as the entry reference baseline to compute the 80% mark.
- Should we add a frontend UI log or notification specifically for "Recovery Squareoff" so it's visually distinct from a "Hard Squareoff"? For now, it will simply populate the database with `exit_reason="Recovery Target Hit"`.

## Proposed Changes

### 1. Core Engine: Straddle Engine ([straddle_engine.py](file:///d:/desktop/Testing/Hedgesnstraddle/HnS_application_11aug/app/core/straddle_engine.py))

#### [MODIFY] [straddle_engine.py](file:///d:/desktop/Testing/Hedgesnstraddle/HnS_application_11aug/app/core/straddle_engine.py)

**A. Implement the Missing Recovery State Logic**
- Add an `if self.state == "RECOVERY" and self.active_session_id:` code block inside `run_loop()`.
- Inside this block, fetch the active session and evaluate the current combined premium using the locked `active_call_mark` and `active_put_mark` (or live marks if missing).
- Condition: `current_recovery_value >= 0.8 * sess.net_straddle_ask`.
- If the condition is met, execute a position squareoff:
  - Update `sess.status = "Completed"`.
  - Add `sess.exit_reason = "Recovery Target Hit (>= 80%)"`.
  - Calculate recovery payout, update the virtual wallet, and write the ledger entry.
  - **Create SELL `StraddleTradeOrder` records for the Call and Put options** to properly close out the database records.
  - Set `self.state = "COMPLETED"` and `self.active_session_id = None`.

**B. Add Missing SELL Orders to Hard Squareoff**
- Locate the Hard Squareoff logic block (`if now_rel >= sq_end_rel`).
- After updating the session to "Completed" and logging the ledger entry, add database insertions for two new `StraddleTradeOrder` objects.
- These will log `side="SELL"`, `status="FILLED"`, `price=rec_call` (and `rec_put`), and match the original session and symbol parameters.

**C. Add Missing SELL Orders to Take Profit (TP) Hit**
- Locate the TP monitoring block (`if tp_hit:`).
- Just like the hard squareoff, this block closes the positions but forgets to write SELL orders.
- Add the `StraddleTradeOrder` inserts for `side="SELL"` here as well, using the active option marks at the time the TP was hit.

## Verification Plan

### Automated Verification
- Re-run `run_test_suite.py` to ensure the core logic and database writes haven't broken any of the existing 11/11 end-to-end verifications.

### Manual Verification
- Simulate an entry into a straddle session, force the time to `cutoff_rel` to enter `RECOVERY` mode, and manually adjust mock prices so the premium hits >80% of entry. Verify the bot triggers the recovery exit and properly generates SELL orders in the dashboard.
- Allow a session to hit `sq_end_rel` and confirm SELL orders populate the "Straddle Trade Order & Fill Records" table on the frontend.
