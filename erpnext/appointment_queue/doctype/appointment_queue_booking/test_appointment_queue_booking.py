import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate, getdate


class TestAppointmentQueueBooking(IntegrationTestCase):
	def setUp(self):
		self.create_test_queue()

	def tearDown(self):
		frappe.db.rollback()

	def create_test_queue(self):
		if not frappe.db.exists("Appointment Queue", "Test General Queue"):
			queue = frappe.get_doc(
				{
					"doctype": "Appointment Queue",
					"queue_name": "Test General Queue",
					"service_type": "General",
					"is_active": 1,
					"max_capacity_per_slot": 5,
					"slots": [
						{
							"day_of_week": "Monday",
							"from_time": "09:00:00",
							"to_time": "10:00:00",
							"max_capacity": 3,
						},
						{
							"day_of_week": "Monday",
							"from_time": "10:00:00",
							"to_time": "11:00:00",
							"max_capacity": 3,
						},
					],
				}
			)
			queue.insert()
			frappe.db.commit()

	def _get_next_monday(self):
		today = getdate(nowdate())
		days_until_monday = (7 - today.weekday()) % 7
		if days_until_monday == 0:
			days_until_monday = 7
		return add_days(today, days_until_monday)

	def test_create_booking_success(self):
		booking_date = self._get_next_monday()
		booking = frappe.get_doc(
			{
				"doctype": "Appointment Queue Booking",
				"queue": "Test General Queue",
				"booking_date": booking_date,
				"customer_name": "Test Customer",
				"customer_phone": "1234567890",
				"status": "Booked",
			}
		)
		booking.insert()

		self.assertEqual(booking.status, "Booked")
		self.assertIsNotNone(booking.slot_time)
		self.assertIsNotNone(booking.queue_number)
		self.assertGreater(booking.queue_number, 0)

	def test_create_booking_full_slot(self):
		booking_date = self._get_next_monday()

		# Fill up the first slot (capacity 3)
		for i in range(3):
			booking = frappe.get_doc(
				{
					"doctype": "Appointment Queue Booking",
					"queue": "Test General Queue",
					"booking_date": booking_date,
					"customer_name": "Customer {0}".format(i),
					"status": "Booked",
				}
			)
			booking.insert()

		# The 4th booking should go to the second slot (10:00-11:00)
		booking4 = frappe.get_doc(
			{
				"doctype": "Appointment Queue Booking",
				"queue": "Test General Queue",
				"booking_date": booking_date,
				"customer_name": "Customer Overflow",
				"status": "Booked",
			}
		)
		booking4.insert()

		self.assertEqual(booking4.queue_number, 1)
		self.assertIn("10:00:00", booking4.slot_time)

	def test_cancel_booking(self):
		booking_date = self._get_next_monday()
		booking = frappe.get_doc(
			{
				"doctype": "Appointment Queue Booking",
				"queue": "Test General Queue",
				"booking_date": booking_date,
				"customer_name": "Cancel Test Customer",
				"status": "Booked",
			}
		)
		booking.insert()

		booking.cancel_booking()
		self.assertEqual(booking.status, "Cancelled")

	def test_cancel_already_cancelled(self):
		booking_date = self._get_next_monday()
		booking = frappe.get_doc(
			{
				"doctype": "Appointment Queue Booking",
				"queue": "Test General Queue",
				"booking_date": booking_date,
				"customer_name": "Double Cancel Test",
				"status": "Booked",
			}
		)
		booking.insert()
		booking.cancel_booking()

		with self.assertRaises(frappe.ValidationError):
			booking.cancel_booking()

	def test_booking_inactive_queue(self):
		queue = frappe.get_doc("Appointment Queue", "Test General Queue")
		queue.is_active = 0
		queue.save()
		frappe.db.commit()

		booking_date = self._get_next_monday()
		booking = frappe.get_doc(
			{
				"doctype": "Appointment Queue Booking",
				"queue": "Test General Queue",
				"booking_date": booking_date,
				"customer_name": "Inactive Queue Test",
				"status": "Booked",
			}
		)

		with self.assertRaises(frappe.ValidationError):
			booking.insert()

		queue.is_active = 1
		queue.save()
		frappe.db.commit()

	def test_booking_past_date(self):
		booking = frappe.get_doc(
			{
				"doctype": "Appointment Queue Booking",
				"queue": "Test General Queue",
				"booking_date": "2020-01-01",
				"customer_name": "Past Date Test",
				"status": "Booked",
			}
		)

		with self.assertRaises(frappe.ValidationError):
			booking.insert()

	def test_get_available_slots(self):
		from erpnext.appointment_queue.api import get_available_slots

		booking_date = self._get_next_monday()
		slots = get_available_slots("Test General Queue", str(booking_date))

		self.assertIsInstance(slots, list)
		if slots:
			self.assertIn("slot_time", slots[0])
			self.assertIn("available", slots[0])
			self.assertGreaterEqual(slots[0]["available"], 0)

	def test_check_in(self):
		booking_date = self._get_next_monday()
		booking = frappe.get_doc(
			{
				"doctype": "Appointment Queue Booking",
				"queue": "Test General Queue",
				"booking_date": booking_date,
				"customer_name": "Check In Test",
				"status": "Booked",
			}
		)
		booking.insert()

		booking.check_in()
		self.assertEqual(booking.status, "Checked In")

	def test_no_show(self):
		booking_date = self._get_next_monday()
		booking = frappe.get_doc(
			{
				"doctype": "Appointment Queue Booking",
				"queue": "Test General Queue",
				"booking_date": booking_date,
				"customer_name": "No Show Test",
				"status": "Booked",
			}
		)
		booking.insert()

		booking.mark_no_show()
		self.assertEqual(booking.status, "No Show")