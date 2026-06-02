import json

import frappe
from frappe import _
from frappe.utils import getdate, nowdate, formatdate


@frappe.whitelist(allow_guest=True)
def create_booking(queue, booking_date, customer_name, customer_phone=None, customer_email=None, notes=None):
	"""
	Create a new appointment queue booking.

	Args:
	    queue (str): Name of the Appointment Queue
	    booking_date (str): Date in YYYY-MM-DD format
	    customer_name (str): Customer's full name
	    customer_phone (str, optional): Customer's phone number
	    customer_email (str, optional): Customer's email address
	    notes (str, optional): Additional notes

	Returns:
	    dict: Booking details including booking_number, queue_number, slot_time, status
	"""
	if isinstance(queue, str) and queue.startswith("{"):
		args = json.loads(queue)
		queue = args.get("queue")
		booking_date = args.get("booking_date")
		customer_name = args.get("customer_name")
		customer_phone = args.get("customer_phone")
		customer_email = args.get("customer_email")
		notes = args.get("notes")

	if not queue or not booking_date or not customer_name:
		frappe.throw(_("queue, booking_date, and customer_name are required."))

	booking = frappe.get_doc(
		{
			"doctype": "Appointment Queue Booking",
			"queue": queue,
			"booking_date": booking_date,
			"customer_name": customer_name,
			"customer_phone": customer_phone,
			"customer_email": customer_email,
			"notes": notes,
			"status": "Booked",
		}
	)

	booking.insert(ignore_permissions=True)

	frappe.db.commit()

	return {
		"booking_number": booking.name,
		"queue": booking.queue,
		"queue_number": booking.queue_number,
		"slot_time": booking.slot_time,
		"booking_date": str(booking.booking_date),
		"status": booking.status,
	}


@frappe.whitelist(allow_guest=True)
def get_booking_status(booking_number):
	"""
	Get the current status of a booking.

	Args:
	    booking_number (str): The booking number (e.g., AQB-25-0001)

	Returns:
	    dict: Booking details including status, queue_number, slot_time
	"""
	if not booking_number:
		frappe.throw(_("booking_number is required."))

	if not frappe.db.exists("Appointment Queue Booking", booking_number):
		frappe.throw(_("Booking {0} not found.").format(booking_number))

	booking = frappe.get_doc("Appointment Queue Booking", booking_number)

	return {
		"booking_number": booking.name,
		"queue": booking.queue,
		"queue_number": booking.queue_number,
		"slot_time": booking.slot_time,
		"booking_date": str(booking.booking_date),
		"status": booking.status,
		"customer_name": booking.customer_name,
	}


@frappe.whitelist(allow_guest=True)
def cancel_booking(booking_number):
	"""
	Cancel an existing booking.

	Args:
	    booking_number (str): The booking number to cancel

	Returns:
	    dict: Result with status
	"""
	if not booking_number:
		frappe.throw(_("booking_number is required."))

	if not frappe.db.exists("Appointment Queue Booking", booking_number):
		frappe.throw(_("Booking {0} not found.").format(booking_number))

	booking = frappe.get_doc("Appointment Queue Booking", booking_number)
	booking.cancel_booking()
	frappe.db.commit()

	return {
		"booking_number": booking.name,
		"status": booking.status,
	}


@frappe.whitelist(allow_guest=True)
def get_available_slots(queue, booking_date):
	"""
	Get available time slots for a given queue and date.

	Args:
	    queue (str): Name of the Appointment Queue
	    booking_date (str): Date in YYYY-MM-DD format

	Returns:
	    list: Available slots with remaining capacity
	"""
	if isinstance(queue, str) and queue.startswith("{"):
		args = json.loads(queue)
		queue = args.get("queue")
		booking_date = args.get("booking_date")

	if not queue or not booking_date:
		frappe.throw(_("queue and booking_date are required."))

	if not frappe.db.exists("Appointment Queue", queue):
		frappe.throw(_("Queue {0} not found.").format(queue))

	queue_doc = frappe.get_cached_doc("Appointment Queue", queue)
	day_of_week = getdate(booking_date).strftime("%A")
	matching_slots = [s for s in queue_doc.slots if s.day_of_week == day_of_week]

	if not matching_slots:
		return []

	available_slots = []
	for slot_cfg in matching_slots:
		capacity = slot_cfg.max_capacity or queue_doc.max_capacity_per_slot
		slot_key = "{0} - {1}".format(slot_cfg.from_time, slot_cfg.to_time)

		booked_count = frappe.db.count(
			"Appointment Queue Booking",
			{
				"queue": queue,
				"booking_date": booking_date,
				"slot_time": slot_key,
				"status": ("not in", ["Cancelled", "No Show"]),
			},
		)

		available = capacity - booked_count
		available_slots.append(
			{
				"slot_time": slot_key,
				"from_time": str(slot_cfg.from_time),
				"to_time": str(slot_cfg.to_time),
				"total_capacity": capacity,
				"booked": booked_count,
				"available": max(available, 0),
			}
		)

	return available_slots


@frappe.whitelist()
def check_in_booking(booking_number):
	"""
	Check in a booking (mark as arrived).

	Args:
	    booking_number (str): The booking number to check in

	Returns:
	    dict: Result with status
	"""
	if not booking_number:
		frappe.throw(_("booking_number is required."))

	if not frappe.db.exists("Appointment Queue Booking", booking_number):
		frappe.throw(_("Booking {0} not found.").format(booking_number))

	booking = frappe.get_doc("Appointment Queue Booking", booking_number)
	booking.check_in()
	frappe.db.commit()

	return {
		"booking_number": booking.name,
		"status": booking.status,
	}


@frappe.whitelist()
def get_todays_bookings(queue=None):
	"""
	Get all bookings for today, optionally filtered by queue.

	Args:
	    queue (str, optional): Filter by queue name

	Returns:
	    list: Today's bookings
	"""
	filters = {"booking_date": nowdate(), "status": ("not in", ["Cancelled", "No Show"])}
	if queue:
		filters["queue"] = queue

	bookings = frappe.get_all(
		"Appointment Queue Booking",
		filters=filters,
		fields=["name", "queue", "queue_number", "slot_time", "customer_name", "status"],
		order_by="queue_number asc",
	)

	return bookings