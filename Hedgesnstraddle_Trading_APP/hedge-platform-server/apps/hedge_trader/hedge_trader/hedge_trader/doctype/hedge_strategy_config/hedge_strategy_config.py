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
		if self.session_pnl_target not in (None, "") and flt(self.session_pnl_target) < 0:
			frappe.throw(_("Session PnL Target cannot be negative."))
