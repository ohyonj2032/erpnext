# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, getdate, nowdate


def create_queue_settings():
	if frappe.db.exists("Appointment Queue Settings", "Test Queue"):
		return frappe.get_doc("Appointment Queue Settings", "Test Queue")

	settings = frappe.get_doc(
		{
			"doctype": "Appointment Queue Settings",
			"queue_name": "Test Queue",
			"description": "Test queue for unit tests",
			"enable_queue": 1,
			"max_concurrent_per_slot": 3,
			"default_service_duration": 30,
			"advance_booking_days": 7,
			"auto_assign_agents": 0,
			"send_email_notifications": 0,
			"notify_before_minutes": 60,
			"time_slots": [
				{"from_time": "09:00:00", "to_time": "10:00:00"},
				{"from_time": "10:00:00", "to_time": "11:00:00"},
				{"from_time": "11:00:00", "to_time": "12:00:00"},
			],
		}
	)
	settings.insert()
	return settings


def create_appointment_queue(settings, date=None, time_slot="09:00:00 - 10:00:00"):
	queue = frappe.get_doc(
		{
			"doctype": "Appointment Queue",
			"queue_type": settings.name,
			"customer_name": "Test Customer",
			"customer_email": "test@example.com",
			"customer_phone": "1234567890",
			"scheduled_date": date or nowdate(),
			"scheduled_time_slot": time_slot,
			"priority": "Normal",
			"service_description": "Test service",
		}
	)
	queue.insert()
	return queue


class TestAppointmentQueue(FrappeTestCase):
	def setUp(self):
		self.settings = create_queue_settings()

		existing = frappe.get_all(
			"Appointment Queue",
			filters={"queue_type": self.settings.name},
			pluck="name",
		)
		for name in existing:
			frappe.delete_doc("Appointment Queue", name, force=True)

	def test_queue_creation(self):
		queue = create_appointment_queue(self.settings)
		self.assertEqual(queue.queue_type, self.settings.name)
		self.assertEqual(queue.status, "Waiting")
		self.assertEqual(queue.queue_position, 1)

	def test_queue_position_increment(self):
		queue1 = create_appointment_queue(self.settings)
		queue2 = create_appointment_queue(self.settings)

		self.assertEqual(queue1.queue_position, 1)
		self.assertEqual(queue2.queue_position, 2)

	def test_concurrency_control(self):
		for i in range(3):
			create_appointment_queue(self.settings)

		self.assertRaises(
			frappe.ValidationError,
			create_appointment_queue,
			self.settings,
		)

	def test_cancel_recalculate_positions(self):
		queue1 = create_appointment_queue(self.settings)
		queue2 = create_appointment_queue(self.settings)

		queue1.cancel()

		queue2.reload()
		self.assertEqual(queue2.queue_position, 1)

	def test_cannot_schedule_in_past(self):
		queue = create_appointment_queue(self.settings)
		queue.scheduled_date = add_days(nowdate(), -1)
		self.assertRaises(frappe.ValidationError, queue.save)

	def test_api_create_queue(self):
		from erpnext.crm.doctype.appointment_queue.api import create_queue

		result = create_queue(
			queue_type=self.settings.name,
			customer_name="API Customer",
			customer_email="api@example.com",
			scheduled_date=nowdate(),
			scheduled_time_slot="10:00:00 - 11:00:00",
		)

		self.assertIn("name", result)
		self.assertEqual(result["queue_position"], 1)

	def test_api_get_status(self):
		queue = create_appointment_queue(self.settings)

		from erpnext.crm.doctype.appointment_queue.api import get_status

		status = get_status(queue.name)
		self.assertEqual(status["status"], "Waiting")
		self.assertEqual(status["queue_position"], 1)

	def test_api_cancel_queue(self):
		queue = create_appointment_queue(self.settings)

		from erpnext.crm.doctype.appointment_queue.api import cancel_queue

		result = cancel_queue(queue.name, reason="Customer request")
		self.assertEqual(result["status"], "Cancelled")

		queue.reload()
		self.assertEqual(queue.status, "Cancelled")

	def test_api_get_available_slots(self):
		create_appointment_queue(self.settings)

		from erpnext.crm.doctype.appointment_queue.api import get_available_slots

		slots = get_available_slots(self.settings.name, nowdate())

		self.assertTrue(len(slots) > 0)

		for slot in slots:
			if slot["time_slot"] == "09:00:00 - 10:00:00":
				self.assertEqual(slot["remaining_capacity"], 2)

	def test_api_list_queues(self):
		create_appointment_queue(self.settings)
		create_appointment_queue(self.settings)

		from erpnext.crm.doctype.appointment_queue.api import list_queues

		queues = list_queues(queue_type=self.settings.name)
		self.assertEqual(len(queues), 2)

	def test_api_get_queue_statistics(self):
		create_appointment_queue(self.settings)
		create_appointment_queue(self.settings)

		from erpnext.crm.doctype.appointment_queue.api import get_queue_statistics

		stats = get_queue_statistics(self.settings.name, nowdate())
		self.assertEqual(stats["total"], 2)
		self.assertEqual(stats["waiting"], 2)
