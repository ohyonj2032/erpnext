# Copyright (c) 2019, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import json
from collections import Counter
from datetime import timedelta

import frappe
from frappe import _
from frappe.desk.form.assign_to import add as add_assignment
from frappe.model.document import Document
from frappe.share import add_docshare
from frappe.utils import flt, get_url, getdate, now, now_datetime
from frappe.utils.verified_command import get_signed_params


class Appointment(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		appointment_with: DF.Link | None
		calendar_event: DF.Link | None
		customer_details: DF.LongText | None
		customer_email: DF.Data
		customer_name: DF.Data
		customer_phone_number: DF.Data | None
		customer_skype: DF.Data | None
		party: DF.DynamicLink | None
		queue_position: DF.Int | None
		queue_status: DF.Literal["Waiting", "Confirmed", "In Progress", "Completed", "Cancelled", "Timed Out", "Rescheduled"]
		scheduled_time: DF.Datetime
		status: DF.Literal["Open", "Unverified", "Closed"]
		stock_reserved: DF.Check
		linked_sales_order: DF.Link | None
		timeout_at: DF.Datetime | None
	# end: auto-generated types

	def find_lead_by_email(self):
		lead_list = frappe.get_list(
			"Lead", filters={"email_id": self.customer_email}, ignore_permissions=True
		)
		if lead_list:
			return lead_list[0].name
		return None

	def find_customer_by_email(self):
		customer_list = frappe.get_list(
			"Customer", filters={"email_id": self.customer_email}, ignore_permissions=True
		)
		if customer_list:
			return customer_list[0].name
		return None

	def before_insert(self):
		number_of_appointments_in_same_slot = frappe.db.count(
			"Appointment", filters={"scheduled_time": self.scheduled_time}
		)
		number_of_agents = frappe.db.get_single_value("Appointment Booking Settings", "number_of_agents")
		if number_of_agents != 0:
			if number_of_appointments_in_same_slot >= number_of_agents:
				frappe.throw(_("Time slot is not available"))
		if not self.party:
			lead = self.find_lead_by_email()
			customer = self.find_customer_by_email()
			if customer:
				self.appointment_with = "Customer"
				self.party = customer
			else:
				self.appointment_with = "Lead"
				self.party = lead

		queue_settings = frappe.get_single("Appointment Queue Settings")
		if queue_settings.enable_queue_management:
			self.queue_status = "Waiting"
			self.queue_position = self._calculate_queue_position()
			timeout_minutes = queue_settings.default_queue_timeout_minutes or 30
			self.timeout_at = now_datetime() + timedelta(minutes=timeout_minutes)

	def after_insert(self):
		if self.party:
			self.auto_assign()
			self.create_calendar_event()
		else:
			self.db_set("status", "Unverified")
			self.send_confirmation_email()

		queue_settings = frappe.get_single("Appointment Queue Settings")
		if queue_settings.enable_queue_management and self.queue_status == "Waiting":
			self._enqueue_appointment()

	def send_confirmation_email(self):
		verify_url = self._get_verify_url()
		template = "confirm_appointment"
		args = {
			"link": verify_url,
			"site_url": frappe.utils.get_url(),
			"full_name": self.customer_name,
		}
		frappe.sendmail(
			recipients=[self.customer_email],
			template=template,
			args=args,
			subject=_("Appointment Confirmation"),
		)
		if frappe.session.user == "Guest":
			frappe.msgprint(_("Please check your email to confirm the appointment"))
		else:
			frappe.msgprint(
				_("Appointment was created. But no lead was found. Please check the email to confirm")
			)

	def on_change(self):
		if not self.calendar_event:
			return
		cal_event = frappe.get_doc("Event", self.calendar_event)
		cal_event.starts_on = self.scheduled_time
		cal_event.save(ignore_permissions=True)

	def set_verified(self, email):
		if email != self.customer_email:
			frappe.throw(_("Email verification failed."))
		self.create_lead_and_link()
		self.status = "Open"
		self.auto_assign()
		self.create_calendar_event()
		self.save(ignore_permissions=True)
		if not frappe.in_test:
			frappe.db.commit()

	def create_lead_and_link(self):
		if self.party:
			return

		lead = frappe.get_doc(
			{
				"doctype": "Lead",
				"lead_name": self.customer_name,
				"email_id": self.customer_email,
				"phone": self.customer_phone_number,
			}
		)

		if self.customer_details:
			lead.append(
				"notes",
				{
					"note": self.customer_details,
					"added_by": frappe.session.user,
					"added_on": now(),
				},
			)

		lead.insert(ignore_permissions=True)
		self.party = lead.name

	def auto_assign(self):
		existing_assignee = self.get_assignee_from_latest_opportunity()
		if existing_assignee:
			self.assign_agent(existing_assignee)
			return
		if self._assign:
			return
		available_agents = _get_agents_sorted_by_asc_workload(getdate(self.scheduled_time))
		for agent in available_agents:
			if _check_agent_availability(agent, self.scheduled_time):
				self.assign_agent(agent[0])
			break

	def get_assignee_from_latest_opportunity(self):
		if not self.party:
			return None
		if not frappe.db.exists("Lead", self.party):
			return None
		opporutnities = frappe.get_list(
			"Opportunity",
			filters={
				"party_name": self.party,
			},
			ignore_permissions=True,
			order_by="creation desc",
		)
		if not opporutnities:
			return None
		latest_opportunity = frappe.get_doc("Opportunity", opporutnities[0].name)
		assignee = latest_opportunity._assign
		if not assignee:
			return None
		assignee = frappe.parse_json(assignee)[0]
		return assignee

	def create_calendar_event(self):
		if self.calendar_event:
			return
		appointment_event = frappe.get_doc(
			{
				"doctype": "Event",
				"subject": " ".join(["Appointment with", self.customer_name]),
				"starts_on": self.scheduled_time,
				"status": "Open",
				"type": "Public",
				"send_reminder": frappe.db.get_single_value(
					"Appointment Booking Settings", "email_reminders"
				),
				"event_participants": [
					dict(reference_doctype=self.appointment_with, reference_docname=self.party)
				],
			}
		)
		employee = _get_employee_from_user(self._assign)
		if employee:
			appointment_event.append(
				"event_participants", dict(reference_doctype="Employee", reference_docname=employee.name)
			)
		appointment_event.insert(ignore_permissions=True)
		self.calendar_event = appointment_event.name
		self.save(ignore_permissions=True)

	def _get_verify_url(self):
		verify_route = "/book_appointment/verify"
		params = {"email": self.customer_email, "appointment": self.name}
		return get_url(verify_route + "?" + get_signed_params(params))

	def assign_agent(self, agent):
		if not frappe.has_permission(doc=self, user=agent):
			add_docshare(self.doctype, self.name, agent, flags={"ignore_share_permission": True})

		add_assignment({"doctype": self.doctype, "name": self.name, "assign_to": [agent]})

	def _calculate_queue_position(self):
		waiting_count = frappe.db.count(
			"Appointment",
			filters={
				"queue_status": "Waiting",
				"scheduled_time": ("lte", self.scheduled_time),
			},
		)
		return waiting_count + 1

	def _enqueue_appointment(self):
		queue_log = frappe.get_doc(
			{
				"doctype": "Appointment Queue Log",
				"appointment": self.name,
				"action": "Enqueued",
				"queue_position": self.queue_position,
				"timestamp": now(),
			}
		)
		queue_log.insert(ignore_permissions=True)

	@frappe.whitelist()
	def confirm_appointment(self, items=None):
		queue_settings = frappe.get_single("Appointment Queue Settings")
		if not queue_settings.enable_queue_management:
			frappe.throw(_("Queue management is not enabled"))

		try:
			self.queue_status = "Confirmed"
			self.timeout_at = now_datetime() + timedelta(
				minutes=queue_settings.reservation_timeout_minutes or 60
			)
			self.save(ignore_permissions=True)

			_queue_log_action(self.name, "Confirmed", self.queue_position)

			if queue_settings.enable_stock_reservation and items:
				_reserve_stock_for_appointment(self, items)

			if queue_settings.enable_auto_convert_to_order:
				_delayed_order_creation(self.name, queue_settings.order_creation_delay_minutes)

			frappe.db.commit()

		except Exception:
			frappe.db.rollback()
			frappe.throw(_("Failed to confirm appointment"))

	@frappe.whitelist()
	def cancel_appointment(self, reason=None):
		try:
			old_status = self.queue_status
			self.queue_status = "Cancelled"
			self.status = "Closed"
			self.save(ignore_permissions=True)

			if self.stock_reserved:
				_release_stock_reservation(self.name)

			_queue_log_action(self.name, "Cancelled", self.queue_position, reason)
			_recalculate_queue_positions(self.scheduled_time)

			_apply_rules_for_event(self, "Cancellation", {"reason": reason, "old_status": old_status})

			frappe.db.commit()

		except Exception:
			frappe.db.rollback()
			frappe.throw(_("Failed to cancel appointment"))

	@frappe.whitelist()
	def reschedule_appointment(self, new_scheduled_time, reason=None):
		try:
			old_time = self.scheduled_time
			old_status = self.queue_status

			self.scheduled_time = new_scheduled_time
			self.queue_status = "Rescheduled"
			self.queue_position = self._calculate_queue_position()
			timeout_minutes = frappe.db.get_single_value(
				"Appointment Queue Settings", "default_queue_timeout_minutes"
			) or 30
			self.timeout_at = now_datetime() + timedelta(minutes=timeout_minutes)
			self.save(ignore_permissions=True)

			if self.stock_reserved:
				_release_stock_reservation(self.name)

			_queue_log_action(
				self.name,
				"Rescheduled",
				self.queue_position,
				f"From {old_time} to {new_scheduled_time}. Reason: {reason or 'N/A'}",
			)
			_recalculate_queue_positions(old_time)

			_apply_rules_for_event(
				self,
				"Manual Reschedule",
				{"old_time": old_time, "new_time": new_scheduled_time, "reason": reason},
			)

			frappe.db.commit()

		except Exception:
			frappe.db.rollback()
			frappe.throw(_("Failed to reschedule appointment"))


def _queue_log_action(appointment_name, action, position, details=None):
	queue_log = frappe.get_doc(
		{
			"doctype": "Appointment Queue Log",
			"appointment": appointment_name,
			"action": action,
			"queue_position": position,
			"details": details,
			"timestamp": now(),
		}
	)
	queue_log.insert(ignore_permissions=True)


def _recalculate_queue_positions(scheduled_time):
	waiting_appointments = frappe.get_all(
		"Appointment",
		filters={"queue_status": "Waiting", "scheduled_time": scheduled_time},
		order_by="creation asc",
		pluck="name",
	)
	for idx, appointment_name in enumerate(waiting_appointments, start=1):
		frappe.db.set_value("Appointment", appointment_name, "queue_position", idx)


def _reserve_stock_for_appointment(appointment, items):
	if not isinstance(items, list):
		items = json.loads(items)

	for item in items:
		item_code = item.get("item_code")
		warehouse = item.get("warehouse")
		qty = flt(item.get("qty", 1))

		if not item_code or not warehouse:
			continue

		reservation = frappe.get_doc(
			{
				"doctype": "Stock Reservation Entry",
				"item_code": item_code,
				"warehouse": warehouse,
				"qty": qty,
				"voucher_type": "Appointment",
				"voucher_no": appointment.name,
				"status": "Reserved",
			}
		)
		reservation.insert(ignore_permissions=True)

	appointment.db_set("stock_reserved", 1)
	_queue_log_action(appointment.name, "Stock Reserved", appointment.queue_position)


def _release_stock_reservation(appointment_name):
	reservations = frappe.get_all(
		"Stock Reservation Entry",
		filters={"voucher_type": "Appointment", "voucher_no": appointment_name, "status": "Reserved"},
		pluck="name",
	)
	for reservation_name in reservations:
		reservation = frappe.get_doc("Stock Reservation Entry", reservation_name)
		reservation.status = "Cancelled"
		reservation.save(ignore_permissions=True)

	frappe.db.set_value("Appointment", appointment_name, "stock_reserved", 0)
	_queue_log_action(appointment_name, "Stock Released", None)


def _delayed_order_creation(appointment_name, delay_minutes):
	frappe.enqueue(
		"erpnext.crm.doctype.appointment.appointment._create_sales_order_from_appointment",
		appointment_name=appointment_name,
		delay_minutes=delay_minutes,
		queue="long",
		job_name=f"Create Sales Order from Appointment {appointment_name}",
	)


def _create_sales_order_from_appointment(appointment_name, delay_minutes):
	import time
	time.sleep(delay_minutes * 60)

	appointment = frappe.get_doc("Appointment", appointment_name)
	if appointment.queue_status != "Confirmed":
		return

	customer = appointment.party
	if not customer or appointment.appointment_with != "Customer":
		return

	sales_order = frappe.get_doc(
		{
			"doctype": "Sales Order",
			"customer": customer,
			"appointment_reference": appointment_name,
			"transaction_date": now(),
		}
	)
	sales_order.insert(ignore_permissions=True)
	sales_order.submit()

	appointment.db_set("linked_sales_order", sales_order.name)
	_queue_log_action(
		appointment_name,
		"Sales Order Created",
		appointment.queue_position,
		f"SO: {sales_order.name}",
	)


def _apply_rules_for_event(appointment, event_type, context):
	queue_settings = frappe.get_single("Appointment Queue Settings")
	if not queue_settings.rules:
		return

	active_rules = [
		r for r in queue_settings.rules
		if r.rule_type == event_type and r.is_active
	]
	active_rules.sort(key=lambda r: r.priority)

	for rule in active_rules:
		_execute_rule(rule, appointment, context)


def _execute_rule(rule, appointment, context):
	try:
		if rule.conditions:
			conditions = json.loads(rule.conditions)
			if not _evaluate_conditions(conditions, appointment, context):
				return

		if rule.actions:
			actions = json.loads(rule.actions)
			_perform_actions(actions, appointment, context)

		_queue_log_action(
			appointment.name,
			f"Rule Executed: {rule.rule_name}",
			appointment.queue_position,
		)

	except Exception:
		frappe.log_error(
			title=f"Appointment Rule Execution Failed: {rule.rule_name}",
			message=frappe.get_traceback(),
		)


def _evaluate_conditions(conditions, appointment, context):
	for key, expected_value in conditions.items():
		actual_value = getattr(appointment, key, None)
		if actual_value != expected_value:
			return False
	return True


def _perform_actions(actions, appointment, context):
	for action in actions:
		action_type = action.get("type")
		if action_type == "update_status":
			appointment.queue_status = action.get("status")
			appointment.save(ignore_permissions=True)
		elif action_type == "send_notification":
			_send_appointment_notification(appointment, action)
		elif action_type == "release_stock":
			_release_stock_reservation(appointment.name)
		elif action_type == "create_order":
			_create_sales_order_from_appointment(appointment.name, 0)


def _send_appointment_notification(appointment, action_config):
	recipients = action.get("recipients", [appointment.customer_email])
	subject = action.get("subject", "Appointment Update")
	message = action.get("message", "Your appointment status has been updated.")

	frappe.sendmail(
		recipients=recipients,
		subject=subject,
		message=message,
	)


@frappe.whitelist()
def process_timeout_appointments():
	expired_appointments = frappe.get_all(
		"Appointment",
		filters={
			"queue_status": ("in", ["Waiting", "Confirmed"]),
			"timeout_at": ("<", now_datetime()),
		},
		pluck="name",
	)

	for appointment_name in expired_appointments:
		appointment = frappe.get_doc("Appointment", appointment_name)
		appointment.queue_status = "Timed Out"
		appointment.save(ignore_permissions=True)

		if appointment.stock_reserved:
			_release_stock_reservation(appointment_name)

		_recalculate_queue_positions(appointment.scheduled_time)

		_apply_rules_for_event(
			appointment,
			"Timeout Release",
			{"timeout_at": appointment.timeout_at},
		)

		_queue_log_action(appointment_name, "Timed Out", appointment.queue_position)

	return len(expired_appointments)
