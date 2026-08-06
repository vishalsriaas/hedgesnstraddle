# Server Deployment

This `frappe-bench` folder now carries the two Frappe apps and their embedded runtime code:

- `apps/hedge_trader` includes the Frappe control plane and the legacy Hedge Trader backend under `hedge_trader/runtime/legacy_engine`.
- `apps/straddle_bot` includes the Frappe control plane and the legacy straddle bot/dashboard under `straddle_bot/runtime/legacy_bot`.

The Frappe Desk routes for the embedded panels are:

- `/app/hedge-panel`, which embeds the Hedge legacy panel from port `8100`.
- `/app/straddle-dashboard`, which embeds the Straddle dashboard from port `8080`.

For one browser-facing origin, put Frappe and the two panel services behind the unified proxy:

```bash
FRAPPE_SITE=your.site.name scripts/start_unified_proxy.sh
```

Then open:

- `http://localhost:9100/app/hedge-panel`
- `http://localhost:9100/app/straddle-dashboard`

The proxy maps `/hedge-runtime/*` to the internal Hedge service on `8100`,
`/straddle-runtime/*` to the internal Straddle dashboard on `8080`, and every other path to
Frappe on `8000`. In production, the same path layout can be handled by nginx instead of this
local Python proxy.

The unified proxy does not start trading runtimes by itself. To show live panel content locally,
start the runtimes and the proxy together:

```bash
START_TRADING_WORKERS=1 START_UNIFIED_PROXY=1 FRAPPE_SITE=your.site.name scripts/start_runtime_workers.sh
```

The folder is enough for the application code, DocTypes, workspaces, runtime wrappers, and legacy bot files. A server still needs normal Frappe infrastructure: Python, Node, Redis, MariaDB, bench, and a site database. Do not rely on copying a Windows virtualenv to Linux; recreate the bench environment on the server, then use these apps from this folder.

## Deploy

From the bench root on the server:

```bash
cd /path/to/frappe-bench
FRAPPE_SITE=your.site.name scripts/deploy_server.sh
```

If the site does not exist yet, create it first with normal bench commands, then rerun the deploy script:

```bash
bench new-site your.site.name
FRAPPE_SITE=your.site.name scripts/deploy_server.sh
```

The deploy script installs both apps in editable mode, ensures `sites/apps.txt` includes them, installs them on the site if needed, migrates, builds assets, restarts bench, and runs a health check.

## Credentials

Do not store exchange or Telegram credentials in code. Set them either in the Frappe Settings DocTypes or the process environment:

- `Hedge Trader Settings`
- `Straddle Bot Settings`
- `BINANCE_API_KEY`
- `BINANCE_API_SECRET`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Runtime SQLite files and logs default to site-private storage:

- `sites/<site>/private/files/hedge_trader/hedge_state.db`
- `sites/<site>/private/files/straddle_bot/straddle_paper.db`

## Start Runtime Processes

Normal Frappe web, worker, scheduler, Redis, and MariaDB processes are still managed by bench/supervisor. The trading runtimes are separate processes:

```bash
START_TRADING_WORKERS=1 FRAPPE_SITE=your.site.name scripts/start_runtime_workers.sh
```

This starts:

- Hedge legacy API/runtime on `HEDGE_RUNTIME_PORT`, default `8100`.
- Hedge command monitor.
- Straddle bot runtime.
- Straddle command monitor.
- Straddle dashboard on `STRADDLE_DASHBOARD_PORT`, default `8080`.

Stop them with:

```bash
scripts/stop_runtime_workers.sh
```

The straddle bot also respects `Straddle Bot Settings.bot_enabled`; set it on before expecting the bot loop to trade. Keep `runtime_mode` as `Paper` until server credentials and connectivity are verified.

## Health Check

```bash
FRAPPE_SITE=your.site.name scripts/health_check.sh
```

The health check verifies installed apps, DocTypes, workspaces, runtime status rows, and the Desk routes when the web server is reachable.
