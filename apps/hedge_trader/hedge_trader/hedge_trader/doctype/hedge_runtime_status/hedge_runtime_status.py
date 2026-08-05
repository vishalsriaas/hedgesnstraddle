from frappe.model.document import Document
from frappe.utils import now_datetime


class HedgeRuntimeStatus(Document):
	def validate(self):
		if not self.last_heartbeat:
			self.last_heartbeat = now_datetime()

