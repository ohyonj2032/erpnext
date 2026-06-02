# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, now_datetime, add_to_date


class AppointmentQueue(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		assigned_agent: DF.Link | None
		customer_email: DF.Data
		customer_name: DF.Data
		customer_phone: DF.Data | None
		notes: DF.Text | None
		party: DF.DynamicLink | None
		party_type: DF.Link | None
		priority: DF.Literal["Normal", "High", "Urgent"]
		queue_position: DF.Int
		queue_type: DF.Link
		scheduled_date: DF.Date
		scheduled_time_slot: DF.Data
		service_description: DF.LongText | None
		service_duration: DF.Int
		status: DF.Literal["Waiting", "Confirmed", "In Progress", "Completed", "Cancelled", "No Show"]
	# end: auto-generated types

	def validate(self):
		self.validate_time_slot_capacity()
		self.validate_scheduled_date()

	def before_insert(self):
		self.assign_queue_position()

	def on_update(self):
		if self.has_value_changed("status") and self.status == "Cancelled":
			self.recalculate_queue_positions()

	def validate_time_slot_capacity(self):
		if self.is_new():
			settings = frappe.get_doc("Appointment Queue Settings", self.queue_type)
			if not settings.enable_queue:
				frappe.throw(_("Queue type {0} is not enabled").format(self.queue_type))

			existing_count = frappe.db.count(
				"Appointment Queue",
				filters={
					"queue_type": self.queue_type,
					"scheduled_date": self.scheduled_date,
					"scheduled_time_slot": self.scheduled_time_slot,
					"status": ["in", ["Waiting", "Confirmed", "In Progress"]],
				},
			)

			if existing_count >= settings.max_concurrent_per_slot:
				frappe.throw(
					_("Time slot {0} is fully booked. Maximum capacity: {1}").format(
						self.scheduled_time_slot, settings.max_concurrent_per_slot
					)
				)

	def validate_scheduled_date(self):
		settings = frappe.get_doc("Appointment Queue Settings", self.queue_type)
		today = getdate()

		if self.scheduled_date < today:
			frappe.throw(_("Cannot schedule appointment in the past"))

		max_advance_date = add_to_date(today, days=settings.advance_booking_days)
		if self.scheduled_date > max_advance_date:
			frappe.throw(
				_("Cannot schedule appointment beyond {0} days in advance").format(
					settings.advance_booking_days
				)
			)

	def assign_queue_position(self):
		existing_count = frappe.db.count(
			"Appointment Queue",
			filters={
				"queue_type": self.queue_type,
				"scheduled_date": self.scheduled_date,
				"scheduled_time_slot": self.scheduled_time_slot,
				"status": ["in", ["Waiting", "Confirmed"]],
			},
		)
		self.queue_position = existing_count + 1

	def recalculate_queue_positions(self):
		queues = frappe.get_all(
			"Appointment Queue",
			filters={
				"queue_type": self.queue_type,
				"scheduled_date": self.scheduled_date,
				"scheduled_time_slot": self.scheduled_time_slot,
				"status": ["in", ["Waiting", "Confirmed"]],
			},
			fields=["name", "queue_position"],
			order_by="queue_position asc",
		)

		for idx, queue in enumerate(queues, start=1):
			if queue.queue_position != idx:
				frappe.db.set_value("Appointment Queue", queue.name, "queue_position", idx)

	@frappe.whitelist()
	def confirm(self):
		self.db_set("status", "Confirmed")
		self.notify_customer()

	@frappe.whitelist()
	def start_service(self):
		self.db_set("status", "In Progress")

	@frappe.whitelist()
	def complete(self):
		self.db_set("status", "Completed")

	@frappe.whitelist()
	def cancel(self):
		self.db_set("status", "Cancelled")
		self.recalculate_queue_positions()
		self.notify_customer()

	def notify_customer(self):
		if not frappe.db.get_single_value("Appointment Queue Settings", "send_email_notifications"):
			return

		template_map = {
			"Confirmed": "appointment_queue_confirmed",
			"Cancelled": "appointment_queue_cancelled",
		}

		template = template_map.get(self.status)
		if not template:
			return

		frappe.sendmail(
			recipients=[self.customer_email],
			template=template,
			args={
				"customer_name": self.customer_name,
				"queue_type": self.queue_type,
				"scheduled_date": self.scheduled_date,
				"scheduled_time_slot": self.scheduled_time_slot,
				"queue_position": self.queue_position,
			},
			subject=_("Appointment Queue {0}: {1}").format(self.status, self.name),
		)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_available_time_slots(doctype, txt, searchfield, start, page_len, filters):
	if not filters or "queue_type" not in filters or "scheduled_date" not in filters:
		return []

	settings = frappe.get_doc("Appointment Queue Settings", filters.get("queue_type"))
	slots = []

	for slot in settings.time_slots:
		slot_label = f"{slot.from_time} - {slot.to_time}"
		if txt and txt.lower() not in slot_label.lower():
			continue

		booked_count = frappe.db.count(
			"Appointment Queue",
			filters={
				"queue_type": filters.get("queue_type"),
				"scheduled_date": filters.get("scheduled_date"),
				"scheduled_time_slot": slot_label,
				"status": ["in", ["Waiting", "Confirmed", "In Progress"]],
			},
		)

		available = settings.max_concurrent_per_slot - booked_count
		if available > 0:
			slots.append([slot_label, f"{available} slots available"])

	return slots


@frappe.whitelist()
def create_appointment_queue(
	queue_type,
	customer_name,
	customer_email,
	scheduled_date,
	scheduled_time_slot,
	customer_phone=None,
	priority="Normal",
	service_description=None,
	party_type=None,
	party=None,
):
	settings = frappe.get_doc("Appointment Queue Settings", queue_type)
	if not settings.enable_queue:
		frappe.throw(_("Queue type {0} is not enabled").format(queue_type))

	existing_count = frappe.db.count(
		"Appointment Queue",
		filters={
			"queue_type": queue_type,
			"scheduled_date": scheduled_date,
			"scheduled_time_slot": scheduled_time_slot,
			"status": ["in", ["Waiting", "Confirmed", "In Progress"]],
		},
	)

	if existing_count >= settings.max_concurrent_per_slot:
		frappe.throw(
			_("Time slot {0} is fully booked. Maximum capacity: {1}").format(
				scheduled_time_slot, settings.max_concurrent_per_slot
			)
		)

	queue_doc = frappe.get_doc(
		{
			"doctype": "Appointment Queue",
			"queue_type": queue_type,
			"customer_name": customer_name,
			"customer_email": customer_email,
			"customer_phone": customer_phone,
			"scheduled_date": scheduled_date,
			"scheduled_time_slot": scheduled_time_slot,
			"priority": priority,
			"service_description": service_description,
			"party_type": party_type,
			"party": party,
			"service_duration": settings.default_service_duration,
		}
	)

	queue_doc.insert(ignore_permissions=True)

	return {
		"name": queue_doc.name,
		"queue_position": queue_doc.queue_position,
		"status": queue_doc.status,
	}


@frappe.whitelist()
def get_queue_status(queue_name):
	queue_doc = frappe.get_doc("Appointment Queue", queue_name)

	estimated_wait = 0
	if queue_doc.status == "Waiting":
		settings = frappe.get_doc("Appointment Queue Settings", queue_doc.queue_type)
		estimated_wait = queue_doc.queue_position * settings.default_service_duration

	return {
		"name": queue_doc.name,
		"status": queue_doc.status,
		"queue_position": queue_doc.queue_position,
		"estimated_wait_minutes": estimated_wait,
		"scheduled_date": queue_doc.scheduled_date,
		"scheduled_time_slot": queue_doc.scheduled_time_slot,
	}


@frappe.whitelist()
def cancel_appointment_queue(queue_name, reason=None):
	frappe.has_permission("Appointment Queue", doc=queue_name, throw=True)

	queue_doc = frappe.get_doc("Appointment Queue", queue_name)
	if queue_doc.status in ["Completed", "Cancelled"]:
		frappe.throw(_("Cannot cancel appointment with status {0}").format(queue_doc.status))

	queue_doc.status = "Cancelled"
	if reason:
		queue_doc.notes = f"Cancellation reason: {reason}\n{queue_doc.notes or ''}"

	queue_doc.save(ignore_permissions=True)

	return {"status": "Cancelled", "name": queue_doc.name}


@frappe.whitelist()
def get_queue_list(queue_type=None, scheduled_date=None, status=None):
	filters = {}
	if queue_type:
		filters["queue_type"] = queue_type
	if scheduled_date:
		filters["scheduled_date"] = scheduled_date
	if status:
		filters["status"] = status

	queues = frappe.get_all(
		"Appointment Queue",
		filters=filters,
		fields=[
			"name",
			"queue_type",
			"customer_name",
			"customer_email",
			"status",
			"priority",
			"queue_position",
			"scheduled_date",
			"scheduled_time_slot",
			"assigned_agent",
		],
		order_by="scheduled_date asc, scheduled_time_slot asc, queue_position asc",
	)

	return queues
