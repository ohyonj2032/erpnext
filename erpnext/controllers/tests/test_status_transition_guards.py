import time
from unittest.mock import patch

import frappe
from frappe.utils import flt

from erpnext.buying.doctype.purchase_order.test_purchase_order import create_purchase_order
from erpnext.buying.utils import check_on_hold_or_closed_status
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
from erpnext.selling.doctype.sales_order.test_sales_order import make_sales_order
from erpnext.stock.doctype.delivery_note.delivery_note import DeliveryNote
from erpnext.tests.utils import ERPNextTestSuite


class TestStatusTransitionGuards(ERPNextTestSuite):
	def test_delivery_note_validates_sales_order_status_with_row_lock(self):
		so = make_sales_order(qty=1)
		dn = make_delivery_note(so.name)
		original_get_value = frappe.db.get_value
		captured_calls = []

		def recording_get_value(*args, **kwargs):
			if len(args) >= 3 and args[:3] == ("Sales Order", so.name, "status"):
				captured_calls.append(kwargs.copy())
			return original_get_value(*args, **kwargs)

		with patch.object(frappe.db, "get_value", side_effect=recording_get_value):
			dn.validate()

		self.assertTrue(any(call.get("for_update") for call in captured_calls))

	def test_purchase_status_validation_uses_row_lock(self):
		po = create_purchase_order(do_not_submit=True)
		po.insert()
		original_get_value = frappe.db.get_value
		captured_calls = []

		def recording_get_value(*args, **kwargs):
			if len(args) >= 3 and args[:3] == ("Purchase Order", po.name, "status"):
				captured_calls.append(kwargs.copy())
			return original_get_value(*args, **kwargs)

		with patch.object(frappe.db, "get_value", side_effect=recording_get_value):
			check_on_hold_or_closed_status("Purchase Order", po.name)

		self.assertTrue(any(call.get("for_update") for call in captured_calls))

	def test_sales_order_update_status_rejects_stale_document(self):
		so = make_sales_order(qty=1)
		stale_so = frappe.get_doc("Sales Order", so.name)
		time.sleep(1)
		so.update_status("Closed")

		with self.assertRaises(frappe.ValidationError):
			stale_so.update_status("Draft")

	def test_delivery_note_submit_failure_rolls_back_sales_order_progress(self):
		frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 1)
		so = make_sales_order(qty=5)
		dn = make_delivery_note(so.name)
		dn.items[0].qty = 5
		dn.insert()

		frappe.db.savepoint("before_failed_delivery_note_submit")

		with patch.object(DeliveryNote, "update_stock_ledger", side_effect=frappe.ValidationError("forced failure")):
			with self.assertRaises(frappe.ValidationError):
				dn.submit()

		frappe.db.rollback(save_point="before_failed_delivery_note_submit")
		so.reload()
		dn.reload()

		self.assertEqual(flt(so.per_delivered, 6), 0)
		self.assertEqual(so.status, "To Deliver and Bill")
		self.assertEqual(dn.docstatus, 0)
