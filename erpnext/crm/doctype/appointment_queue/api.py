# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""
Appointment Queue API Endpoints

This module provides REST API endpoints for the Appointment Queue module.
All endpoints are whitelisted and follow Frappe's API conventions.
"""

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import getdate, now_datetime, time_diff_in_seconds


@frappe.whitelist(allow_guest=True)
@rate_limit(limit=10, seconds=60)
def create_queue(**kwargs):
	"""
	Create a new appointment queue entry.

	Required parameters:
	- queue_type: Link to Appointment Queue Settings
	- customer_name: Customer name
	- customer_email: Customer email
	- scheduled_date: Date in YYYY-MM-DD format
	- scheduled_time_slot: Time slot string (e.g., "09:00:00 - 10:00:00")

	Optional parameters:
	- customer_phone: Phone number
	- priority: Normal/High/Urgent (default: Normal)
	- service_description: Service description text
	- party_type: Party type (e.g., "Customer", "Lead")
	- party: Party name

	Returns:
	- name: Queue document name
	- queue_position: Position in queue
	- status: Current status
	"""
	required_fields = ["queue_type", "customer_name", "customer_email", "scheduled_date", "scheduled_time_slot"]
	for field in required_fields:
		if not kwargs.get(field):
			frappe.throw(_("{0} is required").format(field))

	from erpnext.crm.doctype.appointment_queue.appointment_queue import create_appointment_queue

	return create_appointment_queue(
		queue_type=kwargs.get("queue_type"),
		customer_name=kwargs.get("customer_name"),
		customer_email=kwargs.get("customer_email"),
		scheduled_date=kwargs.get("scheduled_date"),
		scheduled_time_slot=kwargs.get("scheduled_time_slot"),
		customer_phone=kwargs.get("customer_phone"),
		priority=kwargs.get("priority", "Normal"),
		service_description=kwargs.get("service_description"),
		party_type=kwargs.get("party_type"),
		party=kwargs.get("party"),
	)


@frappe.whitelist()
def get_status(queue_name):
	"""
	Get the current status of an appointment queue entry.

	Parameters:
	- queue_name: Name of the Appointment Queue document

	Returns:
	- name: Queue document name
	- status: Current status
	- queue_position: Position in queue
	- estimated_wait_minutes: Estimated wait time in minutes
	- scheduled_date: Scheduled date
	- scheduled_time_slot: Scheduled time slot
	"""
	if not queue_name:
		frappe.throw(_("queue_name is required"))

	from erpnext.crm.doctype.appointment_queue.appointment_queue import get_queue_status

	return get_queue_status(queue_name)


@frappe.whitelist()
def cancel_queue(queue_name, reason=None):
	"""
	Cancel an appointment queue entry.

	Parameters:
	- queue_name: Name of the Appointment Queue document
	- reason: Optional cancellation reason

	Returns:
	- status: "Cancelled"
	- name: Queue document name
	"""
	if not queue_name:
		frappe.throw(_("queue_name is required"))

	from erpnext.crm.doctype.appointment_queue.appointment_queue import cancel_appointment_queue

	return cancel_appointment_queue(queue_name, reason)


@frappe.whitelist()
def list_queues(queue_type=None, scheduled_date=None, status=None):
	"""
	List appointment queue entries with optional filters.

	Parameters:
	- queue_type: Filter by queue type
	- scheduled_date: Filter by date (YYYY-MM-DD)
	- status: Filter by status (Waiting/Confirmed/In Progress/Completed/Cancelled/No Show)

	Returns:
	- List of queue entries with basic information
	"""
	from erpnext.crm.doctype.appointment_queue.appointment_queue import get_queue_list

	return get_queue_list(
		queue_type=queue_type,
		scheduled_date=scheduled_date,
		status=status,
	)


@frappe.whitelist()
def get_available_slots(queue_type, scheduled_date):
	"""
	Get available time slots for a given queue type and date.

	Parameters:
	- queue_type: Appointment Queue Settings name
	- scheduled_date: Date in YYYY-MM-DD format

	Returns:
	- List of available slots with remaining capacity
	"""
	if not queue_type or not scheduled_date:
		frappe.throw(_("queue_type and scheduled_date are required"))

	settings = frappe.get_doc("Appointment Queue Settings", queue_type)
	available_slots = []

	for slot in settings.time_slots:
		slot_label = f"{slot.from_time} - {slot.to_time}"

		booked_count = frappe.db.count(
			"Appointment Queue",
			filters={
				"queue_type": queue_type,
				"scheduled_date": scheduled_date,
				"scheduled_time_slot": slot_label,
				"status": ["in", ["Waiting", "Confirmed", "In Progress"]],
			},
		)

		remaining = settings.max_concurrent_per_slot - booked_count
		if remaining > 0:
			available_slots.append(
				{
					"time_slot": slot_label,
					"from_time": slot.from_time,
					"to_time": slot.to_time,
					"remaining_capacity": remaining,
					"total_capacity": settings.max_concurrent_per_slot,
				}
			)

	return available_slots


@frappe.whitelist()
def get_queue_types():
	"""
	Get all enabled appointment queue types.

	Returns:
	- List of enabled Appointment Queue Settings
	"""
	queue_types = frappe.get_all(
		"Appointment Queue Settings",
		filters={"enable_queue": 1},
		fields=["name", "queue_name", "description", "default_service_duration"],
		order_by="queue_name asc",
	)

	return queue_types


@frappe.whitelist()
def confirm_queue(queue_name):
	"""
	Confirm an appointment queue entry.

	Parameters:
	- queue_name: Name of the Appointment Queue document

	Returns:
	- status: "Confirmed"
	- name: Queue document name
	"""
	frappe.has_permission("Appointment Queue", doc=queue_name, throw=True)

	queue_doc = frappe.get_doc("Appointment Queue", queue_name)
	queue_doc.confirm()

	return {"status": "Confirmed", "name": queue_doc.name}


@frappe.whitelist()
def start_service(queue_name):
	"""
	Start service for an appointment queue entry.

	Parameters:
	- queue_name: Name of the Appointment Queue document

	Returns:
	- status: "In Progress"
	- name: Queue document name
	"""
	frappe.has_permission("Appointment Queue", doc=queue_name, throw=True)

	queue_doc = frappe.get_doc("Appointment Queue", queue_name)
	queue_doc.start_service()

	return {"status": "In Progress", "name": queue_doc.name}


@frappe.whitelist()
def complete_queue(queue_name):
	"""
	Complete an appointment queue entry.

	Parameters:
	- queue_name: Name of the Appointment Queue document

	Returns:
	- status: "Completed"
	- name: Queue document name
	"""
	frappe.has_permission("Appointment Queue", doc=queue_name, throw=True)

	queue_doc = frappe.get_doc("Appointment Queue", queue_name)
	queue_doc.complete()

	return {"status": "Completed", "name": queue_doc.name}


@frappe.whitelist()
def get_queue_statistics(queue_type, scheduled_date):
	"""
	Get statistics for a queue type on a specific date.

	Parameters:
	- queue_type: Appointment Queue Settings name
	- scheduled_date: Date in YYYY-MM-DD format

	Returns:
	- total: Total appointments
	- waiting: Count of waiting appointments
	- confirmed: Count of confirmed appointments
	- in_progress: Count of in-progress appointments
	- completed: Count of completed appointments
	- cancelled: Count of cancelled appointments
	- no_show: Count of no-show appointments
	"""
	if not queue_type or not scheduled_date:
		frappe.throw(_("queue_type and scheduled_date are required"))

	filters = {"queue_type": queue_type, "scheduled_date": scheduled_date}

	stats = {
		"total": frappe.db.count("Appointment Queue", filters=filters),
		"waiting": frappe.db.count("Appointment Queue", {**filters, "status": "Waiting"}),
		"confirmed": frappe.db.count("Appointment Queue", {**filters, "status": "Confirmed"}),
		"in_progress": frappe.db.count("Appointment Queue", {**filters, "status": "In Progress"}),
		"completed": frappe.db.count("Appointment Queue", {**filters, "status": "Completed"}),
		"cancelled": frappe.db.count("Appointment Queue", {**filters, "status": "Cancelled"}),
		"no_show": frappe.db.count("Appointment Queue", {**filters, "status": "No Show"}),
	}

	return stats
