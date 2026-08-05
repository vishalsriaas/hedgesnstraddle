# Hedge Trader

Frappe control-plane app for migrating the existing `hedge_trader - Copy` FastAPI trading platform.

This app now contains the Phase 1 control-plane model:

- Settings and strategy configuration.
- Runtime commands and worker heartbeat/status.
- Trading sessions, orders, open positions, paper ledger entries, and session events.
- Order-block zones, macro events, and system health snapshots.
- Whitelisted bridge methods for a dedicated trading worker.

The live strategy runtime should still run as a dedicated worker process. Keep Binance WebSockets,
tick-sensitive strategy loops, hedge verification, reconnects, and execution out of normal Frappe
request workers and scheduler jobs.

## Local Bench Install

From a real bench environment:

```bash
cd /path/to/frappe-bench
bench get-app hedge_trader /path/to/this/repo/frappe-bench/apps/hedge_trader
bench --site your-site install-app hedge_trader
```

This workspace currently has Frappe source downloaded, but `bench` is not installed on PATH.

## Runtime Worker

Monitor Frappe commands and publish worker heartbeats:

```bash
cd /path/to/frappe-bench
bench --site your-site execute hedge_trader.feeds.worker.main
```

Or run the module directly from an activated bench environment:

```bash
python -m hedge_trader.feeds.worker --site your-site --worker-id local-worker-1
```

To start the embedded legacy API/runtime that is now packaged inside this app:

```bash
python -m hedge_trader.feeds.worker \
  --site your-site \
  --worker-id local-hedge-api \
  --serve-legacy-api \
  --legacy-api-port 8100
```

That serves the old FastAPI panel/API and starts the engine through FastAPI startup. For a
headless runtime with no legacy panel/API:

```bash
python -m hedge_trader.feeds.worker \
  --site your-site \
  --worker-id local-worker-1 \
  --embedded-legacy-engine
```

To start a runtime from an external legacy folder instead:

```bash
python -m hedge_trader.feeds.worker \
  --site your-site \
  --worker-id local-worker-1 \
  --legacy-engine-path "/path/to/hedge_trader - Copy"
```

The embedded runtime uses the selected Frappe site's MariaDB database as its
authoritative state, trade, session and event store. `--site` is mandatory.
Runtime logs remain under the site private files directory.

## Bridge Methods

Useful server methods:

- `hedge_trader.trading.commands.create_command`
- `hedge_trader.trading.commands.get_pending_commands`
- `hedge_trader.trading.commands.claim_pending_commands`
- `hedge_trader.trading.commands.complete_command`
- `hedge_trader.trading.commands.heartbeat`
- `hedge_trader.trading.ingest.upsert_session`
- `hedge_trader.trading.ingest.record_order`
- `hedge_trader.trading.ingest.upsert_position`
- `hedge_trader.trading.ingest.record_ledger_entry`
- `hedge_trader.trading.ingest.record_session_event`

## Native Trading Control Center

Open `/app/trading-control-center` for the combined Hedge and Straddle view.
It reads the authoritative MariaDB ledgers, shows worker freshness, active
sessions, P&L, wallet balance, reconciliation issues and confirmed emergency
square-off controls. Runtime commands remain Pending/Claimed until a worker
acknowledges them; the UI never reports a queued command as executed.

Roles created by the app are `Trading Viewer`, `Trading Operator`, and
`Trading Manager`. Only operators, managers, and System Managers can queue
runtime commands.
