app_name = "straddle_bot"
app_title = "Straddle Bot"
app_publisher = "Straddle Bot Team"
app_description = "Control-plane app for the BTC ITM Straddle Bot."
app_email = "admin@example.com"
app_license = "mit"

required_apps = ["frappe"]

after_install = "straddle_bot.install.after_install"

add_to_apps_screen = [
	{
		"name": "straddle_bot",
		"title": "Straddle Bot",
		"route": "/app/straddle-bot",
	}
]

doc_events = {
	# Live exchange actions must stay out of DocType hooks.
}
