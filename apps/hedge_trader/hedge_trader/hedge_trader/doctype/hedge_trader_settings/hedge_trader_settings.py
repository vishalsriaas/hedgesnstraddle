import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class HedgeTraderSettings(Document):
	def validate(self):
		if self.worker_poll_seconds and cint(self.worker_poll_seconds) < 1:
			frappe.throw(_("Worker Poll Seconds must be at least 1."))
		if self.command_timeout_seconds and cint(self.command_timeout_seconds) < 5:
			frappe.throw(_("Command Timeout Seconds must be at least 5."))
