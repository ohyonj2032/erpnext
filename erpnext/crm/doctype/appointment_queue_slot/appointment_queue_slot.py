import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, get_time


class AppointmentQueueSlot(Document):
	def validate(self):
		self.capacity = cint(self.capacity)
		self.booked_count = cint(self.booked_count)

		if self.capacity <= 0:
			frappe.throw(_("Capacity must be greater than zero."))

		if self.booked_count < 0:
			frappe.throw(_("Booked count cannot be negative."))

		if self.booked_count > self.capacity:
			frappe.throw(_("Booked count cannot exceed capacity."))

		if self.slot_start and self.slot_end and get_time(self.slot_start) >= get_time(self.slot_end):
			frappe.throw(_("Slot end time must be later than slot start time."))

		self.status = "Full" if self.booked_count >= self.capacity else "Open"



def on_doctype_update():
	frappe.db.add_unique(
		"Appointment Queue Slot",
		["company", "queue_date", "slot_start", "slot_end"],
		constraint_name="unique_company_queue_slot",
	)
	frappe.db.add_index(
		"Appointment Queue Slot",
		["company", "queue_date", "status"],
		index_name="company_queue_date_status",
	)
