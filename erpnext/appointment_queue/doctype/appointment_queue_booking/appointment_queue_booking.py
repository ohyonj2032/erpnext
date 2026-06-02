import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import nowdate, getdate, formatdate


class AppointmentQueueBooking(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		booking_date: DF.Date
		customer_email: DF.Data | None
		customer_name: DF.Data
		customer_phone: DF.Data | None
		notes: DF.SmallText | None
		queue: DF.Link
		queue_number: DF.Int
		service_type: DF.Literal["General", "Express", "VIP"]
		slot_time: DF.Data | None
		status: DF.Literal["Booked", "Checked In", "Cancelled", "Expired", "No Show"]
	# end: auto-generated types

	def validate(self):
		self.validate_queue_active()
		self.validate_booking_date()
		self.validate_slot_availability()

	def validate_queue_active(self):
		queue_doc = frappe.get_cached_doc("Appointment Queue", self.queue)
		if not queue_doc.is_active:
			frappe.throw(_("Appointment Queue '{0}' is not active. Please select an active queue.").format(self.queue))

	def validate_booking_date(self):
		if getdate(self.booking_date) < getdate(nowdate()):
			frappe.throw(_("Booking Date cannot be in the past."))

	def validate_slot_availability(self):
		queue_doc = frappe.get_cached_doc("Appointment Queue", self.queue)
		day_of_week = getdate(self.booking_date).strftime("%A")
		matching_slots = [s for s in queue_doc.slots if s.day_of_week == day_of_week]
		if not matching_slots:
			frappe.throw(
				_("No available slots for {0} on {1}. Please choose another date.").format(
					self.queue, day_of_week
				)
			)

	def before_insert(self):
		self._allocate_slot_and_queue_number()

	def _allocate_slot_and_queue_number(self):
		"""
		Allocate a time slot and queue number for the booking.
		Uses database-level locking (FOR UPDATE) to prevent overselling
		when multiple concurrent bookings hit the same slot.
		"""
		queue_doc = frappe.get_cached_doc("Appointment Queue", self.queue)
		day_of_week = getdate(self.booking_date).strftime("%A")
		matching_slots = [s for s in queue_doc.slots if s.day_of_week == day_of_week]

		best_slot = None
		best_slot_capacity = 0

		for slot_cfg in matching_slots:
			capacity = slot_cfg.max_capacity or queue_doc.max_capacity_per_slot
			slot_key = "{0} - {1}".format(slot_cfg.from_time, slot_cfg.to_time)

			# Use database-level pessimistic locking to prevent race conditions.
			# The SELECT ... FOR UPDATE ensures that concurrent transactions
			# serialize on this count query, preventing overselling.
			booked_count = frappe.db.sql(
				"""
				SELECT COUNT(name) FROM `tabAppointment Queue Booking`
				WHERE queue = %s
				  AND booking_date = %s
				  AND slot_time = %s
				  AND status NOT IN ('Cancelled', 'No Show')
				FOR UPDATE
				""",
				(self.queue, self.booking_date, slot_key),
			)[0][0]

			available = capacity - booked_count
			if available > 0 and available > best_slot_capacity:
				best_slot = (slot_key, booked_count)
				best_slot_capacity = available

		if not best_slot:
			frappe.throw(
				_("All slots for {0} on {1} are fully booked. Please try another date.").format(
					self.queue, formatdate(self.booking_date)
				)
			)

		self.slot_time = best_slot[0]
		self.queue_number = best_slot[1] + 1
		self.service_type = queue_doc.service_type

	def on_update(self):
		if self.has_value_changed("status") and self.status == "Cancelled":
			self._on_cancel()

	def _on_cancel(self):
		pass

	def cancel_booking(self):
		if self.status == "Cancelled":
			frappe.throw(_("This booking is already cancelled."))
		if self.status == "Checked In":
			frappe.throw(_("Cannot cancel a booking that has already been checked in."))
		self.status = "Cancelled"
		self.save(ignore_permissions=True)

	def check_in(self):
		if self.status == "Cancelled":
			frappe.throw(_("Cannot check in a cancelled booking."))
		if self.status == "Checked In":
			frappe.throw(_("This booking is already checked in."))
		self.status = "Checked In"
		self.save(ignore_permissions=True)

	def mark_no_show(self):
		if self.status != "Booked":
			frappe.throw(_("Only 'Booked' bookings can be marked as No Show."))
		self.status = "No Show"
		self.save(ignore_permissions=True)