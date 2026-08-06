"""Runtime boundary notes for the migration.

The existing app is async-first:
- Binance spot/futures/options WebSockets run continuously.
- Strategy agents react to ticks and bus events.
- Paper state is persisted frequently for crash recovery.

Frappe is sync-first and best used here as a durable control plane. Keep live
feed handling and tick-sensitive entry/exit logic in an external worker.
"""

from __future__ import annotations

CONTROL_PLANE_RESPONSIBILITIES = {
	"config": "Strategy and risk settings stored as DocTypes or Single DocTypes.",
	"audit": "Sessions, orders, fills, ledger entries, and operator actions.",
	"manual_controls": "Pause, resume, force close, reset paper account.",
	"dashboard": "Desk pages, reports, realtime UI events, and health views.",
}

RUNTIME_WORKER_RESPONSIBILITIES = {
	"feeds": "Long-lived Binance WebSocket connections.",
	"strategy_loop": "Tick-sensitive state machines and hedge verification.",
	"execution": "Paper fills first, live exchange actions only after hardening.",
	"reconnects": "Feed watchdog and reconnect policy.",
}
