import threading
import time
from unittest.mock import patch

import frappe
from frappe.utils import flt, getdate, nowdate, add_days

from erpnext.selling.doctype.sales_order.sales_order import update_status
from erpnext.selling.doctype.sales_order.test_sales_order import (
    create_dn_against_so,
    get_reserved_qty,
    make_sales_order,
)
from erpnext.stock.doctype.item.test_item import make_item
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.tests.utils import ERPNextTestSuite


class TestSalesOrderConcurrency(ERPNextTestSuite):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        make_item("_Test Concurrency Item", {"is_stock_item": 1})
        make_stock_entry(
            item="_Test Concurrency Item",
            target="_Test Warehouse - _TC",
            qty=100,
            rate=50,
        )

    def setUp(self):
        self.so = make_sales_order(
            item_code="_Test Concurrency Item",
            qty=10,
            rate=100,
            do_not_submit=False,
        )
        self.so.reload()
        self.initial_modified = str(self.so.modified)

    # ============================================================
    # Scenario 1: Concurrency on update_status (TOCTOU)
    # Two threads try to close the same SO simultaneously.
    # Without a fix, both can succeed and update_reserved_qty runs
    # with stale data — leading to a double write risk.
    # ============================================================
    def test_concurrent_update_status_should_not_double_write(self):
        so_name = self.so.name
        errors_in_threads = []
        reserved_after_first = None
        reserved_after_second = None

        def thread_a():
            nonlocal reserved_after_first
            try:
                frappe.db.connect()
                update_status("Closed", so_name)
                frappe.db.commit()
                reserved_after_first = get_reserved_qty("_Test Concurrency Item")
            except Exception as e:
                errors_in_threads.append(("A", str(e)))

        def thread_b():
            nonlocal reserved_after_second
            try:
                frappe.db.connect()
                update_status("Closed", so_name)
                frappe.db.commit()
                reserved_after_second = get_reserved_qty("_Test Concurrency Item")
            except Exception as e:
                errors_in_threads.append(("B", str(e)))

        t1 = threading.Thread(target=thread_a)
        t2 = threading.Thread(target=thread_b)

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        so = frappe.get_doc("Sales Order", so_name)
        so.reload()

        final_reserved = get_reserved_qty("_Test Concurrency Item")

        # Status must be "Closed" and reserved_qty must be 0
        # (Closed SO releases all reserved qty)
        self.assertEqual(so.status, "Closed")

        if not errors_in_threads:
            # Both threads reported success but the SO status
            # should be set only once — the reserved_qty should
            # be zeroed exactly once.  No double decrement.
            self.assertEqual(
                final_reserved, 0,
                f"Reserved qty should be 0 for closed SO, got {final_reserved}"
            )
        else:
            # If one thread got a conflict error, that is acceptable too.
            self.assertIn("modified", str(errors_in_threads[0][1]).lower()
                          if errors_in_threads else "")

    # ============================================================
    # Scenario 2: Delivery Note and Status Change race
    # A Delivery Note submit writes delivered_qty via raw SQL.
    # Simultaneously, someone calls update_status("Closed") on SO.
    # The close can succeed based on old delivered_qty,
    # and DN then succeeds writing to a closed SO.
    # ============================================================
    def test_dn_submit_and_so_close_should_not_conflict(self):
        so_name = self.so.name
        so = frappe.get_doc("Sales Order", so_name)

        make_stock_entry(
            item="_Test Concurrency Item",
            target="_Test Warehouse - _TC",
            qty=10,
            rate=50,
        )

        dn = None
        errors_in_threads = []

        def deliver():
            nonlocal dn
            try:
                frappe.db.connect()
                dn = create_dn_against_so(so_name, delivered_qty=5, do_not_submit=False)
                frappe.db.commit()
            except Exception as e:
                errors_in_threads.append(("DN", str(e)))

        def close_so():
            try:
                frappe.db.connect()
                update_status("Closed", so_name)
                frappe.db.commit()
            except Exception as e:
                errors_in_threads.append(("Close", str(e)))

        t1 = threading.Thread(target=deliver)
        t2 = threading.Thread(target=close_so)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        so.reload()
        final_reserved = get_reserved_qty("_Test Concurrency Item")

        if so.status == "Closed":
            # If SO is closed, reserved_qty must be 0
            self.assertEqual(final_reserved, 0,
                             f"Closed SO must have 0 reserved_qty but got {final_reserved}")
        elif so.status == "To Deliver and Bill":
            # If SO is not closed, delivered_qty from DN should be 5
            self.assertEqual(
                flt(so.items[0].delivered_qty), 5.0,
                f"DN delivered_qty should be 5.0, got {so.items[0].delivered_qty}"
            )

    # ============================================================
    # Scenario 3: Duplicate Submit (idempotency)
    # Same update_status("Draft") called twice concurrently
    # on an "On Hold" SO.  Should recover Draft without
    # double-adding to reserved_qty.
    # ============================================================
    def test_duplicate_draft_restore_should_be_idempotent(self):
        so_name = self.so.name

        self.so.db_set("status", "On Hold")
        self.so.reload()

        reserved_before = get_reserved_qty("_Test Concurrency Item")

        errors = []

        def restore_to_draft():
            try:
                frappe.db.connect()
                update_status("Draft", so_name)
                frappe.db.commit()
            except Exception as e:
                errors.append(str(e))

        t1 = threading.Thread(target=restore_to_draft)
        t2 = threading.Thread(target=restore_to_draft)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        so = frappe.get_doc("Sales Order", so_name)
        so.reload()
        reserved_after = get_reserved_qty("_Test Concurrency Item")

        self.assertEqual(so.status, "Draft")
        self.assertEqual(
            reserved_after, reserved_before,
            f"Restoring Draft should not change reserved_qty: "
            f"before={reserved_before} after={reserved_after}"
        )

    # ============================================================
    # Scenario 4: SO Cancel vs Delivery Note Cancel
    # Cancelling SO and DN concurrently can lead to status
    # inconsistency — DN cancels and writes back delivered_qty=0,
    # but SO cancel also tries to update status.
    # ============================================================
    def test_concurrent_cancel_should_leave_consistent_state(self):
        make_stock_entry(
            item="_Test Concurrency Item",
            target="_Test Warehouse - _TC",
            qty=10,
            rate=50,
        )

        so_name = self.so.name
        dn = create_dn_against_so(so_name, delivered_qty=5, do_not_submit=False)
        self.so.reload()

        errors = []

        def cancel_so():
            try:
                frappe.db.connect()
                so = frappe.get_doc("Sales Order", so_name)
                so.cancel()
                frappe.db.commit()
            except Exception as e:
                errors.append(("SO", str(e)))

        def cancel_dn():
            try:
                frappe.db.connect()
                d = frappe.get_doc("Delivery Note", dn.name)
                d.cancel()
                frappe.db.commit()
            except Exception as e:
                errors.append(("DN", str(e)))

        t1 = threading.Thread(target=cancel_so)
        t2 = threading.Thread(target=cancel_dn)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        so = frappe.get_doc("Sales Order", so_name)
        so.reload()

        reserved_final = get_reserved_qty("_Test Concurrency Item")

        if so.docstatus == 2:
            # Cancelled SO should have status="Cancelled", reserved_qty=0
            self.assertEqual(so.status, "Cancelled")
            self.assertEqual(reserved_final, 0,
                             f"Cancelled SO reserved_qty must be 0, got {reserved_final}")
        else:
            # If SO survived, DN cancel should have restored
            # delivered_qty to 0 on the SO line.
            self.assertEqual(
                flt(so.items[0].delivered_qty), 0.0,
                f"Cancelled DN should reset delivered_qty to 0, got {so.items[0].delivered_qty}"
            )

    # ============================================================
    # Scenario 5: Rapid status cycle (Draft→Closed→Draft→Closed)
    # Validates that reserved_qty, billing_status, and delivery_status
    # remain consistent across multiple rapid status changes.
    # ============================================================
    def test_rapid_status_cycle_maintains_consistency(self):
        so_name = self.so.name
        reserved_initial = get_reserved_qty("_Test Concurrency Item")

        # Cycle: Draft → Closed → Draft → Closed → Draft
        update_status("Closed", so_name)
        so = frappe.get_doc("Sales Order", so_name)
        self.assertEqual(so.status, "Closed")
        self.assertEqual(get_reserved_qty("_Test Concurrency Item"), 0)

        update_status("Draft", so_name)
        so = frappe.get_doc("Sales Order", so_name)
        self.assertEqual(so.status, "Draft")
        self.assertEqual(get_reserved_qty("_Test Concurrency Item"), reserved_initial)

        update_status("Closed", so_name)
        so = frappe.get_doc("Sales Order", so_name)
        self.assertEqual(so.status, "Closed")
        self.assertEqual(get_reserved_qty("_Test Concurrency Item"), 0)

        update_status("Draft", so_name)
        so = frappe.get_doc("Sales Order", so_name)
        self.assertEqual(so.status, "Draft")
        self.assertEqual(get_reserved_qty("_Test Concurrency Item"), reserved_initial)

        # billing_status and delivery_status must also reset correctly
        self.assertIn(so.billing_status, ("Not Billed", "Fully Billed"))
        self.assertIn(so.delivery_status, ("Not Delivered", "Fully Delivered"))

    # ============================================================
    # Scenario 6: Billing status conflict — two Sales Invoices
    # hitting the same SO simultaneously via make_sales_invoice.
    # ============================================================
    def test_dual_invoice_against_same_so_should_not_overbill(self):
        from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

        so_name = self.so.name
        errors = []

        si1_name = [None]
        si2_name = [None]

        def create_si(store):
            try:
                frappe.db.connect()
                si = make_sales_invoice(so_name)
                si.insert()
                si.submit()
                frappe.db.commit()
                store[0] = si.name
            except Exception as e:
                errors.append(str(e))
                frappe.db.rollback()

        t1 = threading.Thread(target=create_si, args=(si1_name,))
        t2 = threading.Thread(target=create_si, args=(si2_name,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        so = frappe.get_doc("Sales Order", so_name)
        so.reload()

        total_billed = sum(
            flt(item.billed_amt) for item in so.items if hasattr(item, "billed_amt")
        )
        total_qty = sum(flt(item.qty) for item in so.items)

        self.assertLessEqual(
            total_billed, flt(so.base_grand_total),
            f"Billed amount {total_billed} exceeds SO total {so.base_grand_total}"
        )

    # ============================================================
    # Scenario 7: State machine invariant tests
    # Validates that illegal state transitions are rejected
    # regardless of concurrency.
    # ============================================================
    def test_invalid_state_transitions_are_rejected(self):
        so_name = self.so.name

        # Cancelled SO cannot be closed
        self.so.cancel()
        with self.assertRaises(frappe.ValidationError):
            update_status("Closed", so_name)

        # A closed Draft SO should not accept invalid target statuses
        so2 = make_sales_order(
            item_code="_Test Concurrency Item",
            qty=5,
            rate=50,
            do_not_submit=False,
        )
        update_status("Closed", so2.name)

        # Now try concurrent Draft→On Hold and Draft→Closed from same SO
        errors = []

        def set_on_hold():
            try:
                frappe.db.connect()
                so = frappe.get_doc("Sales Order", so2.name)
                if so.status == "Draft" and so.docstatus == 1:
                    so.update_status("On Hold")
                elif so.status == "Closed":
                    pass
                frappe.db.commit()
            except Exception as e:
                errors.append(str(e))

        def set_closed():
            try:
                frappe.db.connect()
                so = frappe.get_doc("Sales Order", so2.name)
                if so.status == "Draft" and so.docstatus == 1:
                    so.update_status("Closed")
                elif so.status == "On Hold":
                    pass
                frappe.db.commit()
            except Exception as e:
                errors.append(str(e))

        t1 = threading.Thread(target=set_on_hold)
        t2 = threading.Thread(target=set_closed)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        final = frappe.get_doc("Sales Order", so2.name)
        final.reload()
        self.assertIn(final.status, ("On Hold", "Closed"),
                      f"Final status {final.status} is not valid")


def get_reserved_qty(item_code="_Test Concurrency Item",
                     warehouse="_Test Warehouse - _TC"):
    return flt(frappe.db.get_value(
        "Bin",
        {"item_code": item_code, "warehouse": warehouse},
        "reserved_qty",
    ))