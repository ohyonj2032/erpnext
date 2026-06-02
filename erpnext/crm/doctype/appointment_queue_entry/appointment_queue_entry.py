# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe import _

class AppointmentQueueEntry(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		appointment_queue: DF.Link
		customer_email: DF.Data
		customer_name: DF.Data
		status: DF.Literal["Queued", "Active", "Completed", "Cancelled"]
	# end: auto-generated types

	def validate(self):
		if not frappe.db.exists("Appointment Queue", self.appointment_queue):
			frappe.throw(_("Linked Appointment Queue does not exist"))
