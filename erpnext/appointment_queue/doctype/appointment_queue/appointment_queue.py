import frappe
from frappe import _
from frappe.model.document import Document


class AppointmentQueue(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.appointment_queue.doctype.appointment_queue_slot.appointment_queue_slot import (
			AppointmentQueueSlot,
		)

		is_active: DF.Check
		max_capacity_per_slot: DF.Int
		queue_name: DF.Data
		service_type: DF.Literal["General", "Express", "VIP"]
		slots: DF.Table[AppointmentQueueSlot]
	# end: auto-generated types

	def validate(self):
		self.validate_slots()

	def validate_slots(self):
		for slot in self.slots:
			if slot.from_time >= slot.to_time:
				frappe.throw(
					_("Row #{0}: From Time cannot be later than or equal to To Time for {1}").format(
						slot.idx, slot.day_of_week
					)
				)
			if slot.max_capacity <= 0:
				frappe.throw(
					_("Row #{0}: Max Capacity must be greater than 0 for {1}").format(
						slot.idx, slot.day_of_week
					)
				)