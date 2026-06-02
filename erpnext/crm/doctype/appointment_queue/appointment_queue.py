# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime, get_datetime
import datetime


class AppointmentQueue(Document):
	def validate(self):
		if self.start_date and self.end_date:
			if self.start_date > self.end_date:
				frappe.throw(_("Start Date cannot be after End Date"))
	
	def get_available_slots(self, date):
		"""Get available time slots for a given date"""
		available_slots = []
		
		if not self.operation_hours:
			return available_slots
		
		date_obj = get_datetime(date).date()
		weekday = date_obj.strftime("%A")
		
		for hours in self.operation_hours:
			if hours.day_of_week == weekday:
				start_time = datetime.datetime.combine(date_obj, hours.from_time)
				end_time = datetime.datetime.combine(date_obj, hours.to_time)
				
				while start_time < end_time:
					slot_end = start_time + datetime.timedelta(minutes=self.time_slot_duration)
					if slot_end > end_time:
						break
					
					# Check slot availability with concurrency control
					is_available = self._check_slot_availability(start_time)
					available_slots.append({
						"time": start_time,
						"available": is_available
					})
					
					start_time = slot_end
		
		return available_slots
	
	def _check_slot_availability(self, slot_time):
		"""Check if a slot is available with proper concurrency control"""
		# Use FOR UPDATE to lock the rows for concurrency control
		entries = frappe.db.sql("""
			SELECT COUNT(*) as count
			FROM `tabAppointment Queue Entry`
			WHERE queue = %s
			AND scheduled_time = %s
			AND status IN ('Pending', 'Confirmed')
			FOR UPDATE
		""", (self.name, slot_time), as_dict=True)
		
		count = entries[0].count if entries else 0
		return count < self.max_capacity


def create_appointment_queue_entry(queue_name, scheduled_time, customer_name, **kwargs):
	"""Create a new appointment queue entry with concurrency control"""
	queue = frappe.get_doc("Appointment Queue", queue_name)
	
	if not queue.enable_queue:
		frappe.throw(_("This queue is currently disabled"))
	
	if queue.status != "Active":
		frappe.throw(_("This queue is not active"))
	
	# Start a transaction for concurrency control
	try:
		frappe.db.begin()
		
		# Lock the queue record to prevent concurrent modifications
		queue = frappe.get_doc("Appointment Queue", queue_name, for_update=True)
		
		# Check slot availability again in transaction
		slot_available = queue._check_slot_availability(get_datetime(scheduled_time))
		if not slot_available:
			frappe.db.rollback()
			frappe.throw(_("This time slot is no longer available"))
		
		# Create the entry
		entry = frappe.new_doc("Appointment Queue Entry")
		entry.queue = queue_name
		entry.scheduled_time = scheduled_time
		entry.customer_name = customer_name
		entry.customer_email = kwargs.get("customer_email")
		entry.customer_phone = kwargs.get("customer_phone")
		entry.party_type = kwargs.get("party_type")
		entry.party = kwargs.get("party")
		entry.notes = kwargs.get("notes")
		
		# Set status based on queue settings
		if queue.auto_confirm:
			entry.status = "Confirmed"
		else:
			entry.status = "Pending"
		
		# Calculate queue position
		entry.queue_position = _calculate_queue_position(queue_name, scheduled_time)
		
		entry.insert(ignore_permissions=True)
		
		frappe.db.commit()
		return entry
		
	except Exception as e:
		frappe.db.rollback()
		raise e


def _calculate_queue_position(queue_name, scheduled_time):
	"""Calculate the position in the queue for a given slot"""
	count = frappe.db.count("Appointment Queue Entry", {
		"queue": queue_name,
		"scheduled_time": scheduled_time,
		"status": ["in", ["Pending", "Confirmed"]]
	})
	return count + 1


def cancel_appointment_queue_entry(entry_name):
	"""Cancel an appointment queue entry"""
	entry = frappe.get_doc("Appointment Queue Entry", entry_name)
	
	if entry.status in ["Cancelled", "Completed"]:
		frappe.throw(_("This appointment cannot be cancelled"))
	
	# Check cancellation policy
	queue = frappe.get_doc("Appointment Queue", entry.queue)
	time_diff = (get_datetime(entry.scheduled_time) - now_datetime()).total_seconds() / 60
	
	if queue.cancellation_allowed_before > 0 and time_diff < queue.cancellation_allowed_before:
		frappe.throw(_("Cancellation is not allowed less than {0} minutes before the appointment").format(
			queue.cancellation_allowed_before
		))
	
	entry.status = "Cancelled"
	entry.save(ignore_permissions=True)
	return entry


def get_appointment_status(entry_name):
	"""Get the status of an appointment"""
	entry = frappe.get_doc("Appointment Queue Entry", entry_name)
	return {
		"name": entry.name,
		"status": entry.status,
		"queue_position": entry.queue_position,
		"scheduled_time": entry.scheduled_time,
		"queue": entry.queue
	}
