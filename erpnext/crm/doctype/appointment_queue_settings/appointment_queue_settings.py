# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class AppointmentQueueSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.crm.doctype.appointment_queue_time_slot.appointment_queue_time_slot import (
			AppointmentQueueTimeSlot,
		)
		from erpnext.crm.doctype.appointment_queue_agent.appointment_queue_agent import (
			AppointmentQueueAgent,
		)

		advance_booking_days: DF.Int
		agent_list: DF.Table[AppointmentQueueAgent]
		auto_assign_agents: DF.Check
		default_service_duration: DF.Int
		description: DF.SmallText | None
		enable_queue: DF.Check
		max_concurrent_per_slot: DF.Int
		queue_name: DF.Data
		send_email_notifications: DF.Check
		notify_before_minutes: DF.Int
		time_slots: DF.Table[AppointmentQueueTimeSlot]
	# end: auto-generated types

	def validate(self):
		self.validate_time_slots()
		self.validate_max_concurrent()

	def validate_time_slots(self):
		if not self.time_slots:
			frappe.throw(_("Please add at least one time slot"))

		for slot in self.time_slots:
			if slot.from_time >= slot.to_time:
				frappe.throw(
					_("From Time must be before To Time in time slot {0}").format(slot.idx)
				)

	def validate_max_concurrent(self):
		if self.max_concurrent_per_slot <= 0:
			frappe.throw(_("Max Concurrent Per Time Slot must be greater than 0"))
