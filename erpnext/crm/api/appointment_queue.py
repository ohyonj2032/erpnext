# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import get_datetime
from erpnext.crm.doctype.appointment_queue.appointment_queue import (
	create_appointment_queue_entry,
	cancel_appointment_queue_entry,
	get_appointment_status
)


@frappe.whitelist(allow_guest=True)
def get_available_slots(queue_name, date):
	"""Get available time slots for a queue on a specific date"""
	try:
		queue = frappe.get_doc("Appointment Queue", queue_name)
		slots = queue.get_available_slots(date)
		return {
			"success": True,
			"slots": slots,
			"queue_name": queue.queue_name
		}
	except Exception as e:
		frappe.log_error(f"Error getting available slots: {str(e)}")
		return {
			"success": False,
			"message": str(e)
		}


@frappe.whitelist(allow_guest=True)
def create_appointment(queue_name, scheduled_time, customer_name, **kwargs):
	"""Create a new appointment"""
	try:
		entry = create_appointment_queue_entry(
			queue_name=queue_name,
			scheduled_time=scheduled_time,
			customer_name=customer_name,
			**kwargs
		)
		return {
			"success": True,
			"appointment": entry.as_dict(),
			"message": _("Appointment created successfully")
		}
	except Exception as e:
		frappe.log_error(f"Error creating appointment: {str(e)}")
		return {
			"success": False,
			"message": str(e)
		}


@frappe.whitelist(allow_guest=True)
def get_appointment(entry_name):
	"""Get appointment details"""
	try:
		status = get_appointment_status(entry_name)
		return {
			"success": True,
			"appointment": status
		}
	except Exception as e:
		frappe.log_error(f"Error getting appointment: {str(e)}")
		return {
			"success": False,
			"message": str(e)
		}


@frappe.whitelist(allow_guest=True)
def cancel_appointment(entry_name):
	"""Cancel an appointment"""
	try:
		entry = cancel_appointment_queue_entry(entry_name)
		return {
			"success": True,
			"appointment": entry.as_dict(),
			"message": _("Appointment cancelled successfully")
		}
	except Exception as e:
		frappe.log_error(f"Error cancelling appointment: {str(e)}")
		return {
			"success": False,
			"message": str(e)
		}


@frappe.whitelist()
def get_queue_list():
	"""Get list of active appointment queues"""
	try:
		queues = frappe.get_all("Appointment Queue",
			filters={
				"status": "Active",
				"enable_queue": 1
			},
			fields=["name", "queue_name", "service_type", "max_capacity"]
		)
		return {
			"success": True,
			"queues": queues
		}
	except Exception as e:
		frappe.log_error(f"Error getting queue list: {str(e)}")
		return {
			"success": False,
			"message": str(e)
		}


@frappe.whitelist()
def get_queue_appointments(queue_name, date=None):
	"""Get all appointments for a queue"""
	try:
		filters = {"queue": queue_name}
		if date:
			filters["scheduled_time"] = [">=", f"{date} 00:00:00", "<=", f"{date} 23:59:59"]
		
		appointments = frappe.get_all("Appointment Queue Entry",
			filters=filters,
			fields=["*"],
			order_by="scheduled_time asc, creation asc"
		)
		return {
			"success": True,
			"appointments": appointments
		}
	except Exception as e:
		frappe.log_error(f"Error getting queue appointments: {str(e)}")
		return {
			"success": False,
			"message": str(e)
		}
