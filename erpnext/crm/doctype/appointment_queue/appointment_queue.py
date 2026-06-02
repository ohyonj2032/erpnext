# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe import _

class AppointmentQueue(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		queue_name: DF.Data
		date: DF.Date
		capacity: DF.Int
		booked_count: DF.Int
		status: DF.Literal["Open", "Closed"]
	# end: auto-generated types

	def validate(self):
		if self.capacity < 0:
			frappe.throw(_("Capacity cannot be negative"))
		if self.booked_count < 0:
			self.booked_count = 0
		if self.booked_count > self.capacity:
			frappe.throw(_("Booked count cannot exceed capacity"))
