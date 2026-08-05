import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt


class HedgeStrategyConfig(Document):
	def validate(self):
		for fieldname in (
			"trade_start_h",
			"trade_start_m",
			"trade_end_h",
			"trade_end_m",
			"force_close_h",
			"force_close_m",
		):
			value = self.get(fieldname)
			if value in (None, ""):
				continue
			value = cint(value)
			limit = 23 if fieldname.endswith("_h") else 59
			if value < 0 or value > limit:
				frappe.throw(_("{0} must be between 0 and {1}.").format(self.meta.get_label(fieldname), limit))

		if self.contract_qty not in (None, "") and flt(self.contract_qty) < 0:
			frappe.throw(_("Contract Quantity cannot be negative."))

		# Cross-field: window close must be ≤ squareoff (force close)
		# Use session-relative minutes: session starts at 13:31 IST, wraps at 13:30 next day
		_SESSION_START_MINS = 13 * 60 + 31  # 811 minutes from midnight

		def _to_session_mins(h, m):
			t = cint(h) * 60 + cint(m)
			return t - _SESSION_START_MINS if t >= _SESSION_START_MINS else t + (1440 - _SESSION_START_MINS)

		te = _to_session_mins(self.trade_end_h, self.trade_end_m)
		fc = _to_session_mins(self.force_close_h, self.force_close_m)
		if fc < te:
			frappe.throw(_(
				"Squareoff time ({0}:{1}) must be at or after Window Close time ({2}:{3})."
			).format(
				str(cint(self.force_close_h)).zfill(2), str(cint(self.force_close_m)).zfill(2),
				str(cint(self.trade_end_h)).zfill(2),   str(cint(self.trade_end_m)).zfill(2),
			))

	def after_save(self):
		self.sync_to_legacy_engine()

	def on_update(self):
		self.sync_to_legacy_engine()

	def sync_to_legacy_engine(self):
		import urllib.request
		import json
		import os

		side = "bull" if self.direction == "Bullish" else "bear" if self.direction == "Bearish" else None
		if not side:
			return

		payload = {
			f"{side}_force_close_h": cint(self.force_close_h),
			f"{side}_force_close_m": cint(self.force_close_m),
			f"{side}_trade_start_h": cint(self.trade_start_h),
			f"{side}_trade_start_m": cint(self.trade_start_m),
			f"{side}_trade_end_h": cint(self.trade_end_h),
			f"{side}_trade_end_m": cint(self.trade_end_m),
			f"{side}_max_premium": flt(self.max_premium),
			f"{side}_max_time_value": flt(self.max_time_value),
			f"{side}_contract_qty": flt(self.contract_qty),
			f"{side}_partial_profit_ratio": flt(self.partial_profit_ratio),
			f"{side}_partial_tp_multiplier": flt(self.partial_tp_multiplier),
		}

		payload.update({
			f"{side}_first_trader_max_premium": flt(self.max_premium),
			f"{side}_first_trader_max_time_value": flt(self.max_time_value),
			f"{side}_second_trader_max_premium": flt(self.max_premium) - 30.0 if self.max_premium else 190.0,
			f"{side}_second_trader_max_time_value": flt(self.max_time_value) - 30.0 if self.max_time_value else 189.0,
			f"{side}_first_trader_contract_qty": flt(self.contract_qty),
			f"{side}_second_trader_contract_qty": flt(self.contract_qty),
		})

		if self.rebuy_mode:
			payload[f"{side}_rebuy_mode"] = self.rebuy_mode

		url = f"http://127.0.0.1:{os.environ.get('HEDGE_RUNTIME_PORT', '8100')}/api/config"
		try:
			data = json.dumps(payload).encode("utf-8")
			request = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
			with urllib.request.urlopen(request, timeout=3.0) as response:
				pass
		except Exception as e:
			frappe.logger("hedge_trader").warning(f"Could not propagate config to legacy engine: {e}")
