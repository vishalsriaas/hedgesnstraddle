app_name = "hedge_trader"
app_title = "Hedge Trader"
app_publisher = "Hedge Trader Team"
app_description = "Control-plane app for the Hedge Trader strategy platform."
app_email = "admin@example.com"
app_license = "mit"

required_apps = ["frappe"]

after_install = "hedge_trader.install.after_install"

add_to_apps_screen = [
	{
		"name": "hedge_trader",
		"title": "Hedge Trader",
		"route": "/app/hedge-trader",
	}
]

# Frappe should own slow, durable, operator-facing work.
# Tick-by-tick strategy loops and Binance WebSockets should run in a separate worker.
scheduler_events = {
	"all": [
		"hedge_trader.trading.jobs.minute_heartbeat",
	],
	"daily": [
		"hedge_trader.trading.jobs.daily_housekeeping",
	],
}

doc_events = {
	# Fill once DocTypes are added. Keep side-effecting exchange actions out of validate hooks.
}
