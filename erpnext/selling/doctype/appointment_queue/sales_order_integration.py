# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _


def validate_sales_order_in_queue(doc, method=None):
	"""Hook into Sales Order validate: prevent closing/editing if linked to an active queue."""
	if not doc.appointment_queue:
		return

	queue_status = frappe.get_cached_value("Appointment Queue", doc.appointment_queue, "status")
	if queue_status in ("Cancelled", "Completed", "Timeout"):
		return

	if doc.docstatus == 2:
		return

	if doc.get("__islocal"):
		return

	queue_link = frappe.utils.get_link_to_form("Appointment Queue", doc.appointment_queue)
	if queue_status == "Queued":
		frappe.msgprint(
			_("This Sales Order is linked to an active queue entry {0}. "
			  "The queue will be updated when processed.").format(queue_link),
			alert=True,
		)


def on_cancel_sales_order(doc, method=None):
	"""Hook into Sales Order on_cancel: release the linked queue entry."""
	if not doc.appointment_queue:
		return

	queue_doc = frappe.get_doc("Appointment Queue", doc.appointment_queue)
	if queue_doc.status in ("Cancelled", "Completed", "Timeout"):
		return

	queue_doc.cancel(reason="Linked Sales Order {0} was cancelled.".format(doc.name))


def on_submit_sales_order(doc, method=None):
	"""Hook into Sales Order on_submit: update queue status if needed."""
	if not doc.appointment_queue:
		return

	queue_doc = frappe.get_doc("Appointment Queue", doc.appointment_queue)
	if queue_doc.status == "Processing":
		queue_doc.complete_processing()


def get_appointment_queue_dashboard_data(data, doctype, docname):
	"""Add queue info to Sales Order dashboard."""
	if doctype != "Sales Order":
		return data

	queue_name = frappe.db.get_value("Sales Order", docname, "appointment_queue")
	if queue_name:
		data["transactions"].append(
			{"label": _("Appointment Queue"), "items": ["Appointment Queue"]}
		)
		data["data"].append(
			{"name": queue_name, "doctype": "Appointment Queue"}
		)

	return data