import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class HedgeRuntimeCommand(Document):
	def before_insert(self):
		if not self.requested_by:
			self.requested_by = frappe.session.user
		if not self.requested_at:
			self.requested_at = now_datetime()
		if not self.status:
			self.status = "Pending"

	def validate(self):
		if self.status in {"Completed", "Failed", "Cancelled"} and not self.completed_at:
			self.completed_at = now_datetime()
		if self.status == "Claimed" and not self.claimed_at:
			self.claimed_at = now_datetime()
		if self.command == "Force Close" and not self.confirmed:
			frappe.throw(_("Force Close commands must be confirmed."))

