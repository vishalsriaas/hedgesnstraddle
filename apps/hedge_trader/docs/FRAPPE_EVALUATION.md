# Frappe Evaluation For Hedge Trader

This evaluation is based on the actual `hedge_trader - Copy` code, not on any prior migration plan.

## What The Current App Really Is

The current backup is a FastAPI service with:

- Continuous Binance spot, futures, and options WebSocket feeds.
- An in-process async message bus.
- Stateful agents: bullish executor, bearish executor, volatile trader, analyst, manager, and order-block tracker.
- MariaDB persistence for paper trades, sessions, config, state snapshots, and event logs.
- A large static `panel.html` dashboard served by FastAPI.
- Background loops for state persistence, daily DB backup, health monitoring, OB prefetching, and squareoff broadcasts.

That shape matters. This is not a normal CRUD app with occasional jobs. The trading core is a long-lived event-driven runtime.

## Recommendation

Use Frappe as the control plane, not as the live trading engine.

Current implementation status:

- The Frappe app now has Phase 1 control-plane DocTypes for settings, strategy configs, runtime commands/status, sessions, orders, positions, paper ledger, session events, order-block zones, macro events, and health snapshots.
- The app exposes whitelisted bridge methods under `hedge_trader.trading.commands` and `hedge_trader.trading.ingest`.
- The dedicated worker entrypoint is `hedge_trader.feeds.worker`; it can monitor commands or start the current `hedge_trader - Copy` backend from a supplied path.
- The legacy trading engine is not yet rewritten to use the Frappe DocTypes directly.

Best architecture:

1. Frappe app `hedge_trader`
   - Strategy settings.
   - Paper wallet ledger.
   - Sessions, orders, fills, open positions.
   - Order-block snapshots and macro events.
   - Reports, permissions, audit trail, manual controls.
   - Desk dashboard and realtime operator view.

2. External runtime worker
   - Binance WebSocket feeds.
   - Tick cache.
   - Strategy state machines.
   - Paper fills and eventual live execution.
   - Reconnect/watchdog behavior.

3. Bridge between them
   - Redis cache for latest ticks and chain snapshots.
   - Frappe whitelisted methods for commands.
   - Frappe queue jobs only for durable non-latency-critical events.

## Pros Of Using Frappe

- Excellent admin UI for strategy configuration without building forms by hand.
- Built-in MariaDB persistence with migrations, permissions, roles, and audit/version history.
- Strong fit for journals: trading sessions, order logs, paper wallet ledger, PnL reports, macro events, and operator actions.
- Frappe Desk can replace much of `panel.html` with list views, reports, dashboards, and custom pages.
- Better access control than the current open FastAPI/CORS setup.
- Scheduler is good for slow lifecycle tasks: daily snapshots, housekeeping, event calendar checks, reports, and backups.
- Notifications and email/communication primitives can replace some custom alert wiring.
- Standard deployment stack gives Redis, workers, scheduler, socket.io, logs, and process management.

## Cons And Risks

- Frappe scheduler is not suitable for sub-second or tight tick-driven trading logic.
- Persistent Binance WebSocket loops do not belong in request workers or normal scheduler jobs.
- Frappe ORM writes on every tick would be too heavy and may create lock/contention problems.
- Strategy agents currently rely on Python object state; porting that state to DocTypes requires careful transaction design.
- The existing FastAPI WebSocket broadcast model does not map one-to-one to Frappe without redesigning events.
- Debugging a live trading state machine inside Frappe workers would be harder than in a dedicated async service.
- Deployment becomes heavier: MariaDB, Redis, queue workers, scheduler, socket.io, and bench conventions.
- Live exchange actions inside DocType hooks would be dangerous because validation/save retries can repeat side effects.

## What Should Move Into Frappe First

Start with durable records and operator workflows:

- Hedge Trader Settings: API credentials references, runtime mode, global risk flags.
- Strategy Config: one record each for bullish, bearish, volatile, and straddle.
- Executor State: current state, last heartbeat, current session, pause/resume flags.
- Trading Session: one session per strategy cycle.
- Trade Order: every simulated or live order.
- Open Position: active futures/options positions.
- Paper Ledger: immutable wallet debits/credits.
- Order Block Zone: detected zones, lifecycle, daily snapshots.
- Macro Event: event schedule and volatile-trader triggers.
- System Health: feed age, last mark price, options-chain freshness.

Do not start by porting `BaseExecutor` directly into a DocType class. First make the data model stable.

## What Should Stay External At First

- `backend.data.ws_manager`
- `backend.data.spot_feed`
- `backend.data.futures_feed`
- `backend.data.options_feed`
- Tick-by-tick executor loops.
- Hedge verification loops.
- Feed watchdog and reconnect loops.
- Any future live Binance order execution.

These should run in a separate worker process and write concise state/events to Frappe.

## Migration Path I Would Use

### Phase 1: Frappe control-plane shell

- Install this `hedge_trader` app.
- Create DocTypes for settings, strategies, sessions, orders, positions, ledger, zones, and events.
- Seed records from `backend/config.py`.
- Build a basic Desk workspace and reports.

### Phase 2: Read-only integration

- Keep the existing FastAPI engine running.
- Keep session/order state in the Frappe site's MariaDB database.
- Build Frappe dashboard from mirrored records.
- No trading decisions are made by Frappe yet.

### Phase 3: Command bridge

- Add Frappe buttons for pause/resume/reset/force-close.
- Commands write intent records in Frappe.
- The external engine polls or subscribes and executes those commands.
- Every command is acknowledged back into Frappe.

### Phase 4: Move slow logic

- Move macro-event calendar management to Frappe.
- Move reporting, snapshots, and daily housekeeping to Frappe scheduler.
- Keep market feeds and strategy loops external.

### Phase 5: Optional deeper port

- Only after the data model is proven, consider porting parts of paper engine and session accounting into Frappe services.
- Keep live order execution in a dedicated worker even then.

## Bottom Line

Frappe is a good fit for the platform around the strategy: configuration, audit, reports, dashboards, permissions, and operator control.

Frappe is not a good fit for the latency-sensitive strategy runtime itself. The safest implementation is a hybrid: Frappe as the durable brain and cockpit, external async worker as the market-data and execution engine.
