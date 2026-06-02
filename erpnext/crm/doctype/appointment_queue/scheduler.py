# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""
Scheduler functions for Appointment Queue module.

These functions are called by the Frappe scheduler at regular intervals
to handle automated queue operations.
"""

import frappe
from frappe.utils import now_datetime, time_diff_in_minutes, getdate


def process_queue_notifications():
	"""
	Send notifications for upcoming appointments based on notify_before_minutes setting.
	"""
	queue_types = frappe.get_all(
		"Appointment Queue Settings",
		filters={"enable_queue": 1, "send_email_notifications": 1},
		fields=["name", "notify_before_minutes"],
	)

	for queue_type in queue_types:
		notify_minutes = queue_type.notify_before_minutes or 60

		queues = frappe.get_all(
			"Appointment Queue",
			filters={
				"queue_type": queue_type.name,
				"status": "Confirmed",
			},
			fields=["name", "customer_email", "customer_name", "scheduled_date", "scheduled_time_slot"],
		)

		for queue in queues:
			scheduled_datetime = frappe.utils.get_datetime(
				f"{queue.scheduled_date} {queue.scheduled_time_slot.split(' - ')[0]}"
			)
			time_diff = time_diff_in_minutes(scheduled_datetime, now_datetime())

			if 0 < time_diff <= notify_minutes:
				_send_notification(queue)


def _send_notification(queue):
	"""Send reminder notification to customer."""
	frappe.sendmail(
		recipients=[queue.customer_email],
		template="appointment_queue_reminder",
		args={
			"customer_name": queue.customer_name,
			"scheduled_date": queue.scheduled_date,
			"scheduled_time_slot": queue.scheduled_time_slot,
		},
		subject=f"Appointment Reminder: {queue.name}",
	)


def auto_cancel_no_shows():
	"""
	Automatically mark appointments as 'No Show' if the scheduled time has passed
	and the status is still 'Waiting' or 'Confirmed'.
	"""
	today = getdate()

	queues = frappe.get_all(
		"Appointment Queue",
		filters={
			"scheduled_date": ("<", today),
			"status": ["in", ["Waiting", "Confirmed"]],
		},
		fields=["name"],
	)

	for queue in queues:
		frappe.db.set_value("Appointment Queue", queue.name, "status", "No Show")

	if queues:
		frappe.db.commit()
