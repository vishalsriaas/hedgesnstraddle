# Straddle Bot

Frappe control-plane app for the existing `straddle_bot` BTC ITM straddle strategy.

This app models the durable records from the standalone SQLite bot:

- Bot settings and configurable key/value items.
- Trading sessions, orders, fills, PnL snapshots, events, and wallet ledger entries.
- Runtime status and operator commands.
- A `Straddle Bot` workspace with shortcuts for every DocType.

The market-data feeds and tick-sensitive strategy loop should continue to run outside normal Frappe request workers. Use this app as the cockpit: configuration, audit trail, reports, and operator commands.

## Local Bench Install

From a real bench environment:

```bash
cd /path/to/frappe-bench
bench get-app straddle_bot /path/to/this/repo/frappe-bench/apps/straddle_bot
bench --site your-site install-app straddle_bot
```

This backup currently has Frappe source and a virtual environment, but no `sites` directory.

## Runtime Worker

Run the embedded paper/live bot from the app package:

```bash
python -m straddle_bot.feeds.worker --site your-site --worker-id local-straddle --mode bot
```

Run the embedded dashboard:

```bash
python -m straddle_bot.feeds.worker \
  --site your-site \
  --worker-id local-straddle-dashboard \
  --mode dashboard \
  --dashboard-port 8080
```

Credentials and strategy defaults are read from `Straddle Bot Settings` or process environment
variables. The worker stores its SQLite DB under the site private files directory unless `db_path`
is set in settings.

## Bridge Methods

Useful server methods for a dedicated worker:

- `straddle_bot.trading.commands.create_command`
- `straddle_bot.trading.commands.get_pending_commands`
- `straddle_bot.trading.commands.claim_pending_commands`
- `straddle_bot.trading.commands.complete_command`
- `straddle_bot.trading.commands.heartbeat`
- `straddle_bot.trading.ingest.upsert_session`
- `straddle_bot.trading.ingest.record_order`
- `straddle_bot.trading.ingest.record_pnl_snapshot`
- `straddle_bot.trading.ingest.record_session_event`
- `straddle_bot.trading.ingest.record_wallet_ledger_entry`
- `straddle_bot.trading.ingest.record_fill`
