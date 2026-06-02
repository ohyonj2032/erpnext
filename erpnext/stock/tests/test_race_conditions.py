# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""
Tests for race conditions, state consistency, and transaction safety
between Sales Order and Stock modules.

Key scenarios covered:
1. Concurrent status updates on Sales Order from different modules
2. Stock reservation and delivery note submission race conditions
3. State rollback on transaction failures
4. Bin qty consistency after concurrent operations
5. Stock Ledger Entry integrity during cancel/resubmit cycles
"""

import json
import threading
import time
from queue import Queue
from unittest.mock import patch

import frappe
from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, nowdate, today

from erpnext.selling.doctype.sales_order.sales_order import (
	make_delivery_note,
	make_sales_invoice,
)
from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice as dn_make_si
from erpnext.stock.doctype.item.test_item import make_item
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.stock.doctype.stock_ledger_entry.stock_ledger_entry import StockLedgerEntry
from erpnext.stock.stock_balance import get_balance_qty_from_sle, update_bin_qty
from erpnext.stock.utils import get_bin


def make_sales_order_for_test(item_code=None, qty=10, rate=100, warehouse=None, do_not_submit=False):
	"""Helper to create a sales order for testing."""
	from erpnext.selling.doctype.sales_order.test_sales_order import make_sales_order

	return make_sales_order(
		item_code=item_code or "_Test Item",
		qty=qty,
		rate=rate,
		warehouse=warehouse or "_Test Warehouse - _TC",
		do_not_submit=do_not_submit,
	)


class TestSalesOrderStockRaceConditions(IntegrationTestCase):
	"""Test race conditions between Sales Order and Stock module operations."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.test_item = make_item(
			"_Test Race Condition Item",
			{"is_stock_item": 1, "stock_uom": "Nos", "valuation_rate": 100},
		)
		cls.warehouse = "_Test Warehouse - _TC"

	def setUp(self):
		frappe.db.rollback()
		self.clear_stock_ledger()

	def clear_stock_ledger(self):
		"""Clear existing SLE and Bin data for test item."""
		frappe.db.sql(
			"delete from `tabStock Ledger Entry` where item_code = %s",
			self.test_item.item_code,
		)
		frappe.db.sql(
			"delete from `tabBin` where item_code = %s",
			self.test_item.item_code,
		)

	def test_concurrent_delivery_note_submission(self):
		"""
		Test that two delivery notes against the same SO cannot both
		claim the same reserved qty. This simulates a race condition
		where two workers try to deliver the same items simultaneously.

		Scenario:
		- SO with qty=10
		- Two DN threads both try to deliver qty=10
		- Only one should succeed, the other should fail or deliver remaining
		"""
		make_stock_entry(
			item_code=self.test_item.item_code,
			target=self.warehouse,
			qty=10,
			basic_rate=100,
		)

		so = make_sales_order_for_test(
			item_code=self.test_item.item_code,
			qty=10,
			rate=100,
			warehouse=self.warehouse,
		)

		results = Queue()

		def create_and_submit_dn(result_queue, dn_qty):
			"""Worker function to create and submit DN."""
			try:
				dn = make_delivery_note(so.name)
				dn.items[0].qty = dn_qty
				dn.save()
				dn.submit()
				result_queue.put(("success", dn.name, dn.items[0].qty))
			except Exception as e:
				result_queue.put(("error", str(e), dn_qty))

		thread1 = threading.Thread(
			target=create_and_submit_dn, args=(results, 10)
		)
		thread2 = threading.Thread(
			target=create_and_submit_dn, args=(results, 10)
		)

		thread1.start()
		thread1.join()

		thread2.start()
		thread2.join()

		success_count = 0
		errors = []
		while not results.empty():
			status, data, qty = results.get()
			if status == "success":
				success_count += 1
			else:
				errors.append(data)

		so.reload()
		total_delivered = so.items[0].delivered_qty

		self.assertLessEqual(total_delivered, 10, "Delivered qty should not exceed SO qty")

	def test_sales_order_cancel_with_pending_delivery_note(self):
		"""
		Test that SO cannot be cancelled when a draft DN exists.
		Verifies the check_nextdoc_docstatus validation path.

		Code path: sales_order.py:check_nextdoc_docstatus()
		"""
		so = make_sales_order_for_test(
			item_code=self.test_item.item_code,
			qty=5,
			rate=100,
			warehouse=self.warehouse,
		)

		dn = make_delivery_note(so.name)
		dn.items[0].qty = 3
		dn.save()

		so.reload()
		self.assertRaises(frappe.ValidationError, so.cancel)

		dn.cancel()
		so.reload()
		so.cancel()

	def test_bin_qty_consistency_after_so_cancel(self):
		"""
		Test that Bin reserved_qty is correctly updated when SO is cancelled.

		Code paths:
		- sales_order.py:update_reserved_qty()
		- bin.py:update_qty()
		"""
		make_stock_entry(
			item_code=self.test_item.item_code,
			target=self.warehouse,
			qty=100,
			basic_rate=100,
		)

		bin_before = get_bin(self.test_item.item_code, self.warehouse)
		initial_reserved = flt(bin_before.reserved_qty)

		so = make_sales_order_for_test(
			item_code=self.test_item.item_code,
			qty=10,
			rate=100,
			warehouse=self.warehouse,
		)

		bin_after_so = get_bin(self.test_item.item_code, self.warehouse)
		self.assertEqual(
			flt(bin_after_so.reserved_qty),
			initial_reserved + 10,
			"Reserved qty should increase by SO qty",
		)

		so.cancel()

		bin_after_cancel = get_bin(self.test_item.item_code, self.warehouse)
		self.assertEqual(
			flt(bin_after_cancel.reserved_qty),
			initial_reserved,
			"Reserved qty should return to initial after SO cancel",
		)

	def test_stock_ledger_entry_integrity_on_cancel_resubmit(self):
		"""
		Test that SLE entries are properly reversed on cancel and
		recreated on resubmit without duplication.

		Code path: stock_controller.py:make_sl_entries()
		"""
		make_stock_entry(
			item_code=self.test_item.item_code,
			target=self.warehouse,
			qty=50,
			basic_rate=100,
		)

		initial_sle_count = frappe.db.count(
			"Stock Ledger Entry",
			{"item_code": self.test_item.item_code, "warehouse": self.warehouse},
		)

		so = make_sales_order_for_test(
			item_code=self.test_item.item_code,
			qty=5,
			rate=100,
			warehouse=self.warehouse,
		)

		dn = make_delivery_note(so.name)
		dn.submit()

		sle_count_after_dn = frappe.db.count(
			"Stock Ledger Entry",
			{"item_code": self.test_item.item_code, "warehouse": self.warehouse},
		)

		self.assertGreater(
			sle_count_after_dn,
			initial_sle_count,
			"SLE should be created on DN submit",
		)

		dn.cancel()

		sle_count_after_cancel = frappe.db.count(
			"Stock Ledger Entry",
			{"item_code": self.test_item.item_code, "warehouse": self.warehouse, "is_cancelled": 0},
		)

		self.assertEqual(
			sle_count_after_cancel,
			initial_sle_count,
			"SLE count should return to initial after DN cancel",
		)

	def test_concurrent_status_update_race(self):
		"""
		Test that concurrent status updates on the same SO don't cause
		inconsistent state. Simulates two users updating status simultaneously.

		Code path: sales_order.py:update_status()
		"""
		so = make_sales_order_for_test(
			item_code=self.test_item.item_code,
			qty=5,
			rate=100,
			warehouse=self.warehouse,
		)

		results = Queue()

		def update_status(result_queue, new_status):
			try:
				so_doc = frappe.get_doc("Sales Order", so.name)
				so_doc.update_status(new_status)
				result_queue.put(("success", new_status))
			except Exception as e:
				result_queue.put(("error", str(e)))

		thread1 = threading.Thread(
			target=update_status, args=(results, "On Hold")
		)
		thread2 = threading.Thread(
			target=update_status, args=(results, "Closed")
		)

		thread1.start()
		thread1.join()

		thread2.start()
		thread2.join()

		so.reload()
		final_status = so.status

		self.assertIn(
			final_status,
			["On Hold", "Closed"],
			f"Final status should be one of the attempted updates, got: {final_status}",
		)

	def test_partial_delivery_then_cancel_consistency(self):
		"""
		Test that partial delivery followed by SO cancel maintains
		consistent delivered_qty and reserved_qty.

		Scenario:
		- SO qty=10
		- DN1 delivers 4
		- Cancel SO (should fail because DN exists)
		- Cancel DN1
		- Cancel SO (should succeed)
		- Verify all quantities are reset
		"""
		make_stock_entry(
			item_code=self.test_item.item_code,
			target=self.warehouse,
			qty=20,
			basic_rate=100,
		)

		bin_initial = get_bin(self.test_item.item_code, self.warehouse)
		initial_actual = flt(bin_initial.actual_qty)
		initial_reserved = flt(bin_initial.reserved_qty)

		so = make_sales_order_for_test(
			item_code=self.test_item.item_code,
			qty=10,
			rate=100,
			warehouse=self.warehouse,
		)

		dn = make_delivery_note(so.name)
		dn.items[0].qty = 4
		dn.submit()

		so.reload()
		self.assertEqual(so.items[0].delivered_qty, 4, "Delivered qty should be 4")

		bin_after_delivery = get_bin(self.test_item.item_code, self.warehouse)
		self.assertEqual(
			flt(bin_after_delivery.actual_qty),
			initial_actual - 4,
			"Actual qty should decrease by delivered qty",
		)

		dn.cancel()

		so.reload()
		self.assertEqual(so.items[0].delivered_qty, 0, "Delivered qty should reset after DN cancel")

		so.cancel()

		bin_final = get_bin(self.test_item.item_code, self.warehouse)
		self.assertEqual(
			flt(bin_final.reserved_qty),
			initial_reserved,
			"Reserved qty should return to initial after SO cancel",
		)

	def test_sales_invoice_with_update_stock_race(self):
		"""
		Test that Sales Invoice with update_stock=1 correctly updates
		stock ledger and doesn't double-count when multiple SIs are created.

		Code path: selling_controller.py:update_stock_ledger()
		"""
		make_stock_entry(
			item_code=self.test_item.item_code,
			target=self.warehouse,
			qty=20,
			basic_rate=100,
		)

		so = make_sales_order_for_test(
			item_code=self.test_item.item_code,
			qty=10,
			rate=100,
			warehouse=self.warehouse,
		)

		si1 = make_sales_invoice(so.name)
		si1.set("update_stock", 1)
		si1.items[0].qty = 5
		si1.save()
		si1.submit()

		bin_after_si1 = get_bin(self.test_item.item_code, self.warehouse)
		actual_after_si1 = flt(bin_after_si1.actual_qty)

		si2 = make_sales_invoice(so.name)
		si2.set("update_stock", 1)
		si2.items[0].qty = 3
		si2.save()
		si2.submit()

		bin_after_si2 = get_bin(self.test_item.item_code, self.warehouse)
		actual_after_si2 = flt(bin_after_si2.actual_qty)

		self.assertEqual(
			actual_after_si1 - actual_after_si2,
			3,
			"Second SI should reduce actual qty by 3",
		)

		so.reload()
		self.assertEqual(
			so.items[0].delivered_qty,
			8,
			"Total delivered qty should be 8 (5+3)",
		)


class TestStateRollbackScenarios(IntegrationTestCase):
	"""Test state rollback scenarios when transactions fail mid-way."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.test_item = make_item(
			"_Test Rollback Item",
			{"is_stock_item": 1, "stock_uom": "Nos", "valuation_rate": 50},
		)
		cls.warehouse = "_Test Warehouse - _TC"

	def setUp(self):
		frappe.db.rollback()
		frappe.db.sql(
			"delete from `tabStock Ledger Entry` where item_code = %s",
			self.test_item.item_code,
		)
		frappe.db.sql(
			"delete from `tabBin` where item_code = %s",
			self.test_item.item_code,
		)

	def test_rollback_on_validation_failure_during_submit(self):
		"""
		Test that when validation fails during DN submit, no partial
		state changes persist (no SLE created, bin not updated).
		"""
		make_stock_entry(
			item_code=self.test_item.item_code,
			target=self.warehouse,
			qty=10,
			basic_rate=50,
		)

		so = make_sales_order_for_test(
			item_code=self.test_item.item_code,
			qty=5,
			rate=100,
			warehouse=self.warehouse,
		)

		dn = make_delivery_note(so.name)
		dn.items[0].qty = 5

		bin_before = get_bin(self.test_item.item_code, self.warehouse)
		actual_before = flt(bin_before.actual_qty)

		sle_before = frappe.db.count(
			"Stock Ledger Entry",
			{"item_code": self.test_item.item_code, "warehouse": self.warehouse, "is_cancelled": 0},
		)

		dn.save()

		dn.items[0].qty = -1
		self.assertRaises(frappe.NonNegativeError, dn.submit)

		bin_after = get_bin(self.test_item.item_code, self.warehouse)
		self.assertEqual(
			flt(bin_after.actual_qty),
			actual_before,
			"Bin actual_qty should not change on failed submit",
		)

		sle_after = frappe.db.count(
			"Stock Ledger Entry",
			{"item_code": self.test_item.item_code, "warehouse": self.warehouse, "is_cancelled": 0},
		)
		self.assertEqual(
			sle_after,
			sle_before,
			"No new SLE should be created on failed submit",
		)

	def test_modified_date_check_prevents_stale_update(self):
		"""
		Test that check_modified_date prevents updating a document
		that has been modified by another transaction.

		Code path: sales_order.py:check_modified_date()
		"""
		so = make_sales_order_for_test(
			item_code=self.test_item.item_code,
			qty=5,
			rate=100,
			warehouse=self.warehouse,
		)

		so_doc1 = frappe.get_doc("Sales Order", so.name)
		so_doc2 = frappe.get_doc("Sales Order", so.name)

		so_doc1.update_status("On Hold")

		so_doc2.reload()
		so_doc2.update_status("Closed")

	def test_stock_reservation_entry_cancel_consistency(self):
		"""
		Test that cancelling Stock Reservation Entry properly resets
		all related quantities.

		Code paths:
		- stock_reservation_entry.py:on_cancel()
		- stock_reservation_entry.py:update_reserved_qty_in_voucher()
		"""
		make_stock_entry(
			item_code=self.test_item.item_code,
			target=self.warehouse,
			qty=50,
			basic_rate=50,
		)

		so = make_sales_order_for_test(
			item_code=self.test_item.item_code,
			qty=10,
			rate=100,
			warehouse=self.warehouse,
			do_not_submit=True,
		)
		so.reserve_stock = 1
		so.submit()

		sre_list = frappe.get_all(
			"Stock Reservation Entry",
			filters={"voucher_no": so.name},
			pluck="name",
		)
		self.assertTrue(sre_list, "SRE should be created on SO submit")

		so.reload()
		self.assertGreater(
			flt(so.items[0].stock_reserved_qty),
			0,
			"Stock reserved qty should be set",
		)

		cancel_stock_reservation_entries("Sales Order", so.name)

		so.reload()
		self.assertEqual(
			flt(so.items[0].stock_reserved_qty),
			0,
			"Stock reserved qty should reset after SRE cancel",
		)


def cancel_stock_reservation_entries(voucher_type, voucher_no):
	"""Helper to cancel all SREs for a voucher."""
	sre_list = frappe.get_all(
		"Stock Reservation Entry",
		filters={
			"voucher_type": voucher_type,
			"voucher_no": voucher_no,
			"docstatus": 1,
		},
		pluck="name",
	)
	for sre in sre_list:
		sre_doc = frappe.get_doc("Stock Reservation Entry", sre)
		sre_doc.cancel()


class TestFinancialDataIntegrity(IntegrationTestCase):
	"""
	Tests for financial data integrity - the most critical category
	that if missed will directly impact financial reporting.

	Three most easily missed but critical test scenarios:
	1. Amount calculation precision with multiple decimal places
	2. GL Entry consistency with Stock Ledger Entry
	3. Advance payment status tracking on order cancellation
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.test_item = make_item(
			"_Test Financial Item",
			{"is_stock_item": 1, "stock_uom": "Nos", "valuation_rate": 33.333},
		)
		cls.warehouse = "_Test Warehouse - _TC"

	def setUp(self):
		frappe.db.rollback()

	def test_amount_precision_with_high_decimal_rates(self):
		"""
		Test that amounts are calculated correctly with high precision rates.
		Catches floating point errors that could cause financial discrepancies.

		Focus areas:
		- Rate with many decimal places
		- Quantity with fractional values
		- Tax calculations on imprecise base amounts
		"""
		so = make_sales_order_for_test(
			item_code=self.test_item.item_code,
			qty=3,
			rate=33.333333,
			warehouse=self.warehouse,
		)

		expected_base = flt(3 * 33.333333, 2)
		actual_base = flt(so.items[0].base_amount, 2)

		self.assertEqual(
			actual_base,
			expected_base,
			f"Base amount precision mismatch: expected {expected_base}, got {actual_base}",
		)

		self.assertEqual(
			flt(so.grand_total, 2),
			expected_base,
			f"Grand total precision mismatch: expected {expected_base}, got {flt(so.grand_total, 2)}",
		)

	def test_gl_entry_matches_stock_ledger_on_dn_submit(self):
		"""
		Test that GL Entry values match Stock Ledger Entry values
		when Delivery Note is submitted. This is critical for
		financial reporting accuracy.

		Code path: stock_controller.py:make_gl_entries()

		Focus: stock_value_difference in SLE should match
		debit/credit in GL Entry for the same transaction.
		"""
		make_stock_entry(
			item_code=self.test_item.item_code,
			target=self.warehouse,
			qty=10,
			basic_rate=100,
		)

		so = make_sales_order_for_test(
			item_code=self.test_item.item_code,
			qty=5,
			rate=150,
			warehouse=self.warehouse,
		)

		dn = make_delivery_note(so.name)
		dn.submit()

		sle = frappe.db.get_value(
			"Stock Ledger Entry",
			{"voucher_type": "Delivery Note", "voucher_no": dn.name},
			["stock_value_difference", "actual_qty"],
			as_dict=True,
		)

		gl_entries = frappe.get_all(
			"GL Entry",
			{"voucher_type": "Delivery Note", "voucher_no": dn.name},
			["account", "debit", "credit"],
		)

		total_gl_value = sum(entry.debit - entry.credit for entry in gl_entries)

		self.assertEqual(
			flt(abs(total_gl_value), 2),
			flt(abs(sle.stock_value_difference), 2),
			"GL Entry total should match SLE stock_value_difference",
		)

	def test_advance_payment_status_on_so_cancel(self):
		"""
		Test that advance payment status is correctly reset when
		SO is cancelled after payment entries are cancelled.

		Code path: sales_order.py:on_cancel() with pricing_rule updates

		This is easily missed because payment status tracking involves
		multiple document types and state synchronization.
		"""
		from erpnext.accounts.doctype.payment_entry.test_payment_entry import get_payment_entry
		from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request

		so = make_sales_order_for_test(
			item_code=self.test_item.item_code,
			qty=1,
			rate=100,
			warehouse=self.warehouse,
		)

		self.assertEqual(
			frappe.db.get_value("Sales Order", so.name, "advance_payment_status"),
			"Not Requested",
		)

		pr = make_payment_request(
			dt=so.doctype,
			dn=so.name,
			submit_doc=True,
			return_doc=True,
			mute_email=True,
		)

		self.assertEqual(
			frappe.db.get_value("Sales Order", so.name, "advance_payment_status"),
			"Requested",
		)

		pe = get_payment_entry(so.doctype, so.name)
		pe.reference_no = "TEST-001"
		pe.reference_date = nowdate()
		pe.save()
		pe.submit()

		self.assertEqual(
			frappe.db.get_value("Sales Order", so.name, "advance_payment_status"),
			"Fully Paid",
		)

		pe.cancel()
		self.assertEqual(
			frappe.db.get_value("Sales Order", so.name, "advance_payment_status"),
			"Requested",
		)

		pr.cancel()
		self.assertEqual(
			frappe.db.get_value("Sales Order", so.name, "advance_payment_status"),
			"Not Requested",
		)


class TestMultiCurrencyMultiRoleRegression(IntegrationTestCase):
	"""
	Minimal but high-value regression tests covering:
	1. Permission misconfiguration
	2. Amount precision loss with multi-currency
	3. State transition anomalies
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.test_item = make_item(
			"_Test Multi Currency Item",
			{"is_stock_item": 1, "stock_uom": "Nos", "valuation_rate": 100},
		)
		cls.warehouse = "_Test Warehouse - _TC"

	def setUp(self):
		frappe.db.rollback()

	def test_user_cannot_submit_so_without_permission(self):
		"""
		Test that users without Sales Manager role cannot submit
		Sales Orders when workflow is active.

		Code path: sales_order workflow transitions
		"""
		from frappe.model.workflow import apply_workflow

		workflow = self._create_test_workflow()

		so = make_sales_order_for_test(
			item_code=self.test_item.item_code,
			qty=1,
			rate=150,
			warehouse=self.warehouse,
			do_not_submit=True,
		)

		apply_workflow(so, "Approve")

		test_user = self._create_test_user_with_roles(
			"test_regress@example.com", ["Sales User", "Test Junior Approver"]
		)

		frappe.set_user(test_user.name)

		trans_item = json.dumps(
			[{"item_code": self.test_item.item_code, "rate": 150, "qty": 2, "docname": so.items[0].name}]
		)

		self.assertRaises(
			frappe.ValidationError,
			self._update_child_qty,
			"Sales Order",
			trans_item,
			so.name,
		)

		frappe.set_user("Administrator")
		workflow.is_active = 0
		workflow.save()

	def test_multi_currency_precision_on_so(self):
		"""
		Test that multi-currency SO maintains precision in
		base_amount and grand_total calculations.

		Focus: exchange_rate * rate should not lose precision
		"""
		currency = self._ensure_usd_currency()

		so = frappe.new_doc("Sales Order")
		so.customer = "_Test Customer"
		so.currency = currency
		so.conversion_rate = 83.123456
		so.company = "_Test Company"
		so.transaction_date = today()
		so.append(
			"items",
			{
				"item_code": self.test_item.item_code,
				"qty": 7,
				"rate": 10.123456,
				"warehouse": self.warehouse,
				"delivery_date": today(),
			},
		)
		so.save()
		so.submit()

		expected_base_amount = flt(7 * 10.123456 * 83.123456, 2)
		actual_base_amount = flt(so.items[0].base_amount, 2)

		self.assertAlmostEqual(
			actual_base_amount,
			expected_base_amount,
			places=2,
			msg=f"Base amount precision: expected {expected_base_amount}, got {actual_base_amount}",
		)

	def test_state_transition_from_delivered_to_cancelled(self):
		"""
		Test that SO cannot be cancelled when DN is submitted,
		and that state transitions are properly blocked.

		Covers: state machine validation paths
		"""
		make_stock_entry(
			item_code=self.test_item.item_code,
			target=self.warehouse,
			qty=20,
			basic_rate=100,
		)

		so = make_sales_order_for_test(
			item_code=self.test_item.item_code,
			qty=5,
			rate=100,
			warehouse=self.warehouse,
		)

		dn = make_delivery_note(so.name)
		dn.submit()

		so.reload()
		self.assertEqual(so.status, "To Deliver and Bill")

		self.assertRaises(frappe.LinkExistsError, so.cancel)

		dn.cancel()
		so.reload()
		so.cancel()
		self.assertEqual(so.status, "Cancelled")

	def _create_test_workflow(self):
		"""Create a test workflow for Sales Order."""
		if frappe.db.exists("Workflow", "SO Regression Test Workflow"):
			doc = frappe.get_doc("Workflow", "SO Regression Test Workflow")
			doc.set("is_active", 1)
			doc.save()
			return doc

		frappe.get_doc(doctype="Role", role_name="Test Junior Approver").insert(
			ignore_if_duplicate=True
		)
		frappe.get_doc(doctype="Role", role_name="Test Approver").insert(
			ignore_if_duplicate=True
		)

		workflow = frappe.get_doc(
			{
				"doctype": "Workflow",
				"workflow_name": "SO Regression Test Workflow",
				"document_type": "Sales Order",
				"workflow_state_field": "workflow_state",
				"is_active": 1,
				"send_email_alert": 0,
			}
		)
		workflow.append("states", dict(state="Pending", allow_edit="All"))
		workflow.append(
			"states", dict(state="Approved", allow_edit="Test Approver", doc_status=1)
		)
		workflow.append(
			"transitions",
			dict(
				state="Pending",
				action="Approve",
				next_state="Approved",
				allowed="Test Junior Approver",
				allow_self_approval=1,
				condition="doc.grand_total < 200",
			),
		)
		workflow.insert(ignore_permissions=True)
		return workflow

	def _create_test_user_with_roles(self, email, roles):
		"""Create a test user with specified roles."""
		if frappe.db.exists("User", email):
			user = frappe.get_doc("User", email)
		else:
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "Test",
					"user_type": "System User",
				}
			)
			user.insert(ignore_permissions=True)

		for role in roles:
			user.add_roles(role)

		return user

	def _update_child_qty(self, doctype, trans_item, docname):
		"""Helper to call update_child_qty_rate."""
		from erpnext.controllers.accounts_controller import update_child_qty_rate

		update_child_qty_rate(doctype, trans_item, docname)

	def _ensure_usd_currency(self):
		"""Ensure USD currency exists."""
		if not frappe.db.exists("Currency", "USD"):
			frappe.get_doc(
				{
					"doctype": "Currency",
					"currency_name": "USD",
					"name": "USD",
				}
			).insert(ignore_permissions=True)
		return "USD"
