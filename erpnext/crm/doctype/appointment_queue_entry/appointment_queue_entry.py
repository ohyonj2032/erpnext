# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime, get_datetime


class AppointmentQueueEntry(Document):
	def before_insert(self):
		"""Validate before inserting"""
		self._validate_queue_status()
		self._validate_slot_availability()
	
	def _validate_queue_status(self):
		"""Validate that the queue is active and enabled"""
		queue = frappe.get_doc("Appointment Queue", self.queue)
		if not queue.enable_queue:
			frappe.throw(_("This queue is currently disabled"))
		if queue.status != "Active":
			frappe.throw(_("This queue is not active"))
	
	def _validate_slot_availability(self):
		"""Validate that the time slot is still available"""
		queue = frappe.get_doc("Appointment Queue", self.queue)
		if not queue._check_slot_availability(get_datetime(self.scheduled_time)):
			frappe.throw(_("This time slot is no longer available"))
	
	def on_update(self):
		"""Update queue positions when status changes"""
		if self.has_value_changed("status"):
			self._recalculate_queue_positions()
	
	def _recalculate_queue_positions(self):
		"""Recalculate positions for all entries in the same slot"""
		entries = frappe.get_all("Appointment Queue Entry",
			filters={
				"queue": self.queue,
				"scheduled_time": self.scheduled_time,
				"status": ["in", ["Pending", "Confirmed"]]
			},
			order_by="creation asc"
		)
		
		for idx, entry in enumerate(entries, 1):
			frappe.db.set_value("Appointment Queue Entry", entry.name, "queue_position", idx)
	
	def cancel(self):
		"""Cancel this appointment"""
		from erpnext.crm.doctype.appointment_queue.appointment_queue import cancel_appointment_queue_entry
		return cancel_appointment_queue_entry(self.name)
