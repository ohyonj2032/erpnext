import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, get_datetime, now_datetime


class AppointmentQueue(Document):
	def validate(self):
		self.sync_slot_details()
		self.validate_status_transition()

	def before_insert(self):
		self.status = self.status or "Queued"
		self.reserve_slot()

	def on_trash(self):
		if self.status != "Cancelled":
			frappe.throw(_("Please cancel the appointment before deleting it."))

	def sync_slot_details(self):
		if not self.slot:
			frappe.throw(_("Queue slot is required."))

		slot = frappe.db.get_value(
			"Appointment Queue Slot",
			self.slot,
			["company", "queue_date", "slot_start"],
			as_dict=True,
		)
		if not slot:
			frappe.throw(_("Appointment Queue Slot {0} was not found.").format(self.slot))

		self.company = slot.company
		self.queue_date = slot.queue_date
		self.scheduled_time = get_datetime(f"{slot.queue_date} {slot.slot_start}")

	def validate_status_transition(self):
		if self.is_new():
			return

		previous_status = self.get_db_value("status")
		if previous_status == self.status:
			return

		if self.status == "Cancelled":
			frappe.throw(_("Please use the queue cancellation API to cancel this appointment."))

		allowed_transitions = {
			"Queued": {"Checked In", "Completed"},
			"Checked In": {"Completed"},
			"Completed": set(),
			"Cancelled": set(),
		}
		if self.status not in allowed_transitions.get(previous_status, set()):
			frappe.throw(
				_("Invalid status transition from {0} to {1}.").format(previous_status, self.status)
			)

	def reserve_slot(self):
		slot = lock_slot(self.slot)
		if slot.booked_count >= slot.capacity:
			frappe.throw(_("The selected slot is fully booked."))

		self.company = slot.company
		self.queue_date = slot.queue_date
		self.scheduled_time = get_datetime(f"{slot.queue_date} {slot.slot_start}")
		self.queue_number = cint(slot.booked_count) + 1
		update_slot_allocation(slot.name, self.queue_number, slot.capacity)

	def cancel_queue(self, reason: str | None = None):
		if self.status == "Cancelled":
			return self

		if self.status not in ("Queued", "Checked In"):
			frappe.throw(_("Only queued or checked-in appointments can be cancelled."))

		slot = lock_slot(self.slot)
		updated_booked_count = max(cint(slot.booked_count) - 1, 0)
		update_slot_allocation(slot.name, updated_booked_count, slot.capacity)

		frappe.db.set_value(
			self.doctype,
			self.name,
			{
				"status": "Cancelled",
				"cancellation_reason": reason,
				"cancelled_by": frappe.session.user,
				"cancelled_on": now_datetime(),
			},
			update_modified=True,
		)
		self.reload()
		return self


@frappe.whitelist()
def create_queue_appointment(
	slot: str,
	customer_name: str,
	customer_phone: str | None = None,
	customer_email: str | None = None,
	notes: str | None = None,
):
	if not frappe.has_permission("Appointment Queue", "create"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	doc = frappe.get_doc(
		{
			"doctype": "Appointment Queue",
			"slot": slot,
			"customer_name": customer_name,
			"customer_phone": customer_phone,
			"customer_email": customer_email,
			"notes": notes,
		}
	)
	doc.insert()
	return get_queue_appointment_status(doc.name)


@frappe.whitelist()
def get_queue_appointment_status(name: str):
	doc = frappe.get_doc("Appointment Queue", name)
	doc.check_permission("read")

	return {
		"name": doc.name,
		"status": doc.status,
		"queue_number": doc.queue_number,
		"slot": doc.slot,
		"queue_date": doc.queue_date,
		"scheduled_time": doc.scheduled_time,
		"customer_name": doc.customer_name,
		"customer_phone": doc.customer_phone,
		"customer_email": doc.customer_email,
		"cancelled_on": doc.cancelled_on,
	}


@frappe.whitelist()
def cancel_queue_appointment(name: str, reason: str | None = None):
	doc = frappe.get_doc("Appointment Queue", name)
	doc.check_permission("write")
	doc.cancel_queue(reason=reason)
	return get_queue_appointment_status(doc.name)



def lock_slot(slot_name: str):
	slot = frappe.db.sql(
		"""
		SELECT name, company, queue_date, slot_start, capacity, booked_count
		FROM `tabAppointment Queue Slot`
		WHERE name = %s
		FOR UPDATE
		""",
		(slot_name,),
		as_dict=True,
	)
	if not slot:
		frappe.throw(_("Appointment Queue Slot {0} was not found.").format(slot_name))
	return slot[0]



def update_slot_allocation(slot_name: str, booked_count: int, capacity: int):
	frappe.db.set_value(
		"Appointment Queue Slot",
		slot_name,
		{
			"booked_count": booked_count,
			"status": "Full" if booked_count >= cint(capacity) else "Open",
		},
		update_modified=True,
	)



def on_doctype_update():
	frappe.db.add_unique(
		"Appointment Queue",
		["slot", "queue_number"],
		constraint_name="unique_slot_queue_number",
	)
	frappe.db.add_index(
		"Appointment Queue",
		["slot", "status"],
		index_name="slot_status",
	)
