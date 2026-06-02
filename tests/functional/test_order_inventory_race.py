import json
import operator
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, flt, nowdate, today

from erpnext.models.order import (
    InvalidStateTransition,
    OrderStatus,
    VALID_TRANSITIONS,
    check_idempotency,
    compute_signature,
    get_state_history,
    record_state_change,
    set_last_result,
    validate_transition,
)
from erpnext.models.inventory import (
    ConcurrentModificationError,
    InsufficientStockError,
    atomic_deduct_stock,
    atomic_reserve_stock,
    check_optimistic_lock,
)
from erpnext.services.order_service import (
    IdempotentResult,
    LockAcquisitionError,
    acquire_lock,
    cancel_order_with_lock,
    clear_idempotency_cache,
    deduct_stock_for_delivery,
    get_order_lock_status,
    order_lock,
    release_lock,
    submit_order_with_lock,
    submit_order_with_reservation,
    update_order_status_with_lock,
)


TEST_ITEM = "_Test Race Item"
TEST_WAREHOUSE = "_Test Race Warehouse - _TC"
TEST_COMPANY = "_Test Company"


class TestOrderInventoryRace(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fixtures = cls._load_fixtures()

    @classmethod
    def _load_fixtures(cls):
        fixture_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "fixtures",
            "initial_data.json",
        )
        if os.path.exists(fixture_path):
            with open(fixture_path) as f:
                return json.load(f)
        return {}

    def setUp(self):
        frappe.set_user("Administrator")
        self._ensure_test_item()
        self._ensure_test_warehouse()
        self._ensure_stock(100)

    def _ensure_test_item(self):
        if not frappe.db.exists("Item", TEST_ITEM):
            doc = frappe.new_doc("Item")
            doc.item_code = TEST_ITEM
            doc.item_name = TEST_ITEM
            doc.item_group = "_Test Item Group"
            doc.stock_uom = "_Test UOM"
            doc.is_stock_item = 1
            doc.valuation_rate = 100.0
            doc.standard_rate = 100.0
            doc.insert(ignore_permissions=True)

    def _ensure_test_warehouse(self):
        if not frappe.db.exists("Warehouse", TEST_WAREHOUSE):
            parent = frappe.db.get_value(
                "Warehouse", {"warehouse_name": "_Test Warehouse", "company": TEST_COMPANY}, "name"
            )
            if not parent:
                parent = "_Test Warehouse - _TC"
            doc = frappe.new_doc("Warehouse")
            doc.warehouse_name = "_Test Race Warehouse"
            doc.company = TEST_COMPANY
            doc.is_group = 0
            if frappe.db.exists("Warehouse", parent):
                doc.parent_warehouse = parent
            doc.insert(ignore_permissions=True)

    def _ensure_stock(self, qty):
        current = frappe.db.get_value(
            "Bin",
            {"item_code": TEST_ITEM, "warehouse": TEST_WAREHOUSE},
            "actual_qty",
        ) or 0

        if operator.lt(flt(current), flt(qty)):
            from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
            make_stock_entry(
                item_code=TEST_ITEM,
                target=TEST_WAREHOUSE,
                qty=flt(qty) - flt(current),
                basic_rate=100.0,
                do_not_submit=False,
            )

    def _make_draft_so(self, qty=10, rate=100.0):
        so = frappe.new_doc("Sales Order")
        so.company = TEST_COMPANY
        so.customer = "_Test Customer"
        so.currency = "INR"
        so.delivery_date = add_days(nowdate(), 10)
        so.append(
            "items",
            {
                "item_code": TEST_ITEM,
                "warehouse": TEST_WAREHOUSE,
                "qty": qty,
                "rate": rate,
            },
        )
        so.insert(ignore_permissions=True)
        return so.name

    def _get_bin_qty(self):
        result = frappe.db.get_value(
            "Bin",
            {"item_code": TEST_ITEM, "warehouse": TEST_WAREHOUSE},
            ["actual_qty", "reserved_qty", "projected_qty"],
            as_dict=True,
        )
        if not result:
            return {"actual_qty": 0, "reserved_qty": 0, "projected_qty": 0}
        return result

    def test_state_machine_valid_transitions(self):
        for source, targets in VALID_TRANSITIONS.items():
            for target in targets:
                self.assertTrue(
                    validate_transition(source, target),
                    f"Transition from {source.value} to {target.value} should be valid",
                )

    def test_state_machine_invalid_transition(self):
        self.assertRaises(
            InvalidStateTransition,
            validate_transition,
            OrderStatus.CANCELLED,
            OrderStatus.COMPLETED,
        )

    def test_state_machine_completed_to_draft_invalid(self):
        self.assertRaises(
            InvalidStateTransition,
            validate_transition,
            OrderStatus.COMPLETED,
            OrderStatus.DRAFT,
        )

    def test_lock_acquire_and_release(self):
        lock_key, lock_value = acquire_lock("TEST-SO-001", timeout=10)
        self.assertIsNotNone(lock_key)
        self.assertIsNotNone(lock_value)

        status = get_order_lock_status("TEST-SO-001")
        self.assertTrue(status["is_locked"])

        released = release_lock(lock_key, lock_value)
        self.assertTrue(released)

        status = get_order_lock_status("TEST-SO-001")
        self.assertFalse(status["is_locked"])

    def test_lock_prevents_double_acquire(self):
        lock_key1, lock_value1 = acquire_lock("TEST-SO-002", timeout=10)
        self.assertIsNotNone(lock_key1)

        lock_key2, lock_value2 = acquire_lock("TEST-SO-002", timeout=10)
        self.assertIsNone(lock_key2)

        release_lock(lock_key1, lock_value1)

    def test_lock_context_manager(self):
        with order_lock("TEST-SO-003", timeout=10) as (lk, lv):
            self.assertIsNotNone(lk)
            status = get_order_lock_status("TEST-SO-003")
            self.assertTrue(status["is_locked"])

        status = get_order_lock_status("TEST-SO-003")
        self.assertFalse(status["is_locked"])

    def test_idempotency_first_call(self):
        is_dup, result = check_idempotency("TEST-SO-004", "submit")
        self.assertFalse(is_dup)
        self.assertIsNone(result)

    def test_idempotency_duplicate_call(self):
        test_result = {"order_name": "TEST-SO-005", "action": "submit", "status": "success"}
        set_last_result("TEST-SO-005", "submit", test_result)

        is_dup, cached = check_idempotency("TEST-SO-005", "submit")
        self.assertTrue(is_dup)
        self.assertEqual(cached, test_result)

    def test_idempotency_different_actions(self):
        set_last_result("TEST-SO-006", "submit", {"action": "submit"})
        set_last_result("TEST-SO-006", "cancel", {"action": "cancel"})

        is_dup_submit, _ = check_idempotency("TEST-SO-006", "submit")
        is_dup_cancel, _ = check_idempotency("TEST-SO-006", "cancel")
        is_dup_update, _ = check_idempotency("TEST-SO-006", "update_status")

        self.assertTrue(is_dup_submit)
        self.assertTrue(is_dup_cancel)
        self.assertFalse(is_dup_update)

    def test_compute_signature_deterministic(self):
        sig1 = compute_signature("SO-001", "submit", qty=10)
        sig2 = compute_signature("SO-001", "submit", qty=10)
        self.assertEqual(sig1, sig2)

    def test_compute_signature_different_params(self):
        sig1 = compute_signature("SO-001", "submit", qty=10)
        sig2 = compute_signature("SO-001", "submit", qty=20)
        self.assertNotEqual(sig1, sig2)

    def test_state_history_recording(self):
        entry = record_state_change("TEST-SO-007", "Draft", "To Deliver and Bill", "submit")
        self.assertEqual(entry["from_status"], "Draft")
        self.assertEqual(entry["to_status"], "To Deliver and Bill")
        self.assertEqual(entry["action"], "submit")

        history = get_state_history("TEST-SO-007")
        self.assertTrue(len(history) >= 1)
        self.assertEqual(history[-1]["action"], "submit")

    def test_submit_order_with_lock(self):
        so_name = self._make_draft_so()
        result = submit_order_with_lock(so_name)

        self.assertFalse(result.is_duplicate)
        self.assertEqual(result.result["order_name"], so_name)
        self.assertEqual(result.result["action"], "submit")

        so_status = frappe.db.get_value("Sales Order", so_name, "status")
        self.assertNotEqual(so_status, "Draft")

    def test_duplicate_submit_returns_idempotent(self):
        so_name = self._make_draft_so()
        result1 = submit_order_with_lock(so_name)
        self.assertFalse(result1.is_duplicate)

        clear_idempotency_cache(so_name, "submit")

        so_status = frappe.db.get_value("Sales Order", so_name, "status")
        if so_status != "Draft":
            result2 = submit_order_with_lock(so_name)
            self.assertTrue(result2.is_duplicate)

    def test_cancel_order_with_lock(self):
        so_name = self._make_draft_so()
        submit_order_with_lock(so_name)

        result = cancel_order_with_lock(so_name)
        self.assertFalse(result.is_duplicate)
        self.assertEqual(result.result["action"], "cancel")

        so_status = frappe.db.get_value("Sales Order", so_name, "status")
        self.assertEqual(so_status, "Cancelled")

    def test_cancel_draft_order_invalid(self):
        so_name = self._make_draft_so()
        self.assertRaises(
            InvalidStateTransition,
            cancel_order_with_lock,
            so_name,
        )

    def test_concurrent_submit_same_order(self):
        so_name = self._make_draft_so()

        results = []
        errors = []

        def try_submit():
            try:
                result = submit_order_with_lock(so_name)
                results.append(result)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=try_submit) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        successful = [r for r in results if not r.is_duplicate and r.result and "error" not in r.result]
        idempotent = [r for r in results if r.is_duplicate]

        self.assertTrue(
            len(successful) >= 1,
            "At least one submit should succeed",
        )

        so_status = frappe.db.get_value("Sales Order", so_name, "status")
        self.assertNotEqual(so_status, "Draft")

    def test_concurrent_reserve_same_bin(self):
        self._ensure_stock(100)

        results = []
        errors = []

        def try_reserve(qty):
            try:
                result = atomic_reserve_stock(TEST_ITEM, TEST_WAREHOUSE, qty)
                results.append(result)
            except (InsufficientStockError, ConcurrentModificationError) as e:
                errors.append(str(e))

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(try_reserve, 50) for _ in range(3)]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception:
                    pass

        bin_data = self._get_bin_qty()
        total_reserved = flt(bin_data.get("reserved_qty", 0))
        self.assertTrue(
            operator.le(total_reserved, 100),
            f"Total reserved qty {total_reserved} should not exceed actual qty 100",
        )

    def test_concurrent_deduct_same_bin(self):
        self._ensure_stock(100)

        results = []
        errors = []

        def try_deduct(qty):
            try:
                result = atomic_deduct_stock(TEST_ITEM, TEST_WAREHOUSE, qty)
                results.append(result)
            except (InsufficientStockError, ConcurrentModificationError) as e:
                errors.append(str(e))

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(try_deduct, 50) for _ in range(3)]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception:
                    pass

        bin_data = self._get_bin_qty()
        actual = flt(bin_data.get("actual_qty", 0))
        self.assertTrue(
            operator.ge(actual, 0),
            f"Actual qty {actual} should not go negative",
        )

    def test_insufficient_stock_error(self):
        self._ensure_stock(5)
        self.assertRaises(
            InsufficientStockError,
            atomic_deduct_stock,
            TEST_ITEM,
            TEST_WAREHOUSE,
            999,
        )

    def test_rollback_on_reservation_failure(self):
        self._ensure_stock(5)

        so_name = self._make_draft_so(qty=999)

        with self.assertRaises(Exception):
            submit_order_with_reservation(so_name)

        so_status = frappe.db.get_value("Sales Order", so_name, "status")
        self.assertEqual(so_status, "Draft")

    def test_submit_with_reservation_success(self):
        self._ensure_stock(100)

        so_name = self._make_draft_so(qty=10)
        result = submit_order_with_reservation(so_name)

        self.assertFalse(result.is_duplicate)
        self.assertIn("reservations", result.result)

        so_status = frappe.db.get_value("Sales Order", so_name, "status")
        self.assertNotEqual(so_status, "Draft")

    def test_update_status_with_lock(self):
        so_name = self._make_draft_so()
        submit_order_with_lock(so_name)

        so_status = frappe.db.get_value("Sales Order", so_name, "status")

        if so_status in ("To Deliver and Bill", "To Deliver"):
            result = update_order_status_with_lock(so_name, "Closed")
            self.assertFalse(result.is_duplicate)

            new_status = frappe.db.get_value("Sales Order", so_name, "status")
            self.assertEqual(new_status, "Closed")

    def test_deduct_stock_for_delivery(self):
        self._ensure_stock(100)

        result = deduct_stock_for_delivery(
            order_name="TEST-DEDUCT-001",
            item_code=TEST_ITEM,
            warehouse=TEST_WAREHOUSE,
            qty=10,
            company=TEST_COMPANY,
        )

        self.assertFalse(result.is_duplicate)
        self.assertIn("deduction", result.result)
        self.assertEqual(result.result["deduction"]["deducted_qty"], 10)

        bin_data = self._get_bin_qty()
        self.assertEqual(flt(bin_data.get("actual_qty", 0)), 90)

    def test_duplicate_deduct_returns_idempotent(self):
        self._ensure_stock(100)

        deduct_stock_for_delivery(
            order_name="TEST-DEDUCT-DUP-001",
            item_code=TEST_ITEM,
            warehouse=TEST_WAREHOUSE,
            qty=10,
            company=TEST_COMPANY,
        )

        result = deduct_stock_for_delivery(
            order_name="TEST-DEDUCT-DUP-001",
            item_code=TEST_ITEM,
            warehouse=TEST_WAREHOUSE,
            qty=10,
            company=TEST_COMPANY,
        )

        self.assertTrue(result.is_duplicate)

        bin_data = self._get_bin_qty()
        self.assertEqual(flt(bin_data.get("actual_qty", 0)), 90)

    def test_optimistic_lock_detection(self):
        self._ensure_stock(100)

        bin_name = frappe.db.get_value(
            "Bin", {"item_code": TEST_ITEM, "warehouse": TEST_WAREHOUSE}, "name"
        )
        original_modified = frappe.db.get_value("Bin", bin_name, "modified")

        frappe.db.set_value("Bin", bin_name, "actual_qty", 50, update_modified=True)

        self.assertRaises(
            ConcurrentModificationError,
            check_optimistic_lock,
            bin_name,
            original_modified,
        )

    def test_financial_consistency_after_deduct(self):
        self._ensure_stock(100)

        from erpnext.models.inventory import atomic_deduct_with_gl

        result = atomic_deduct_with_gl(
            item_code=TEST_ITEM,
            warehouse=TEST_WAREHOUSE,
            qty=10,
            company=TEST_COMPANY,
            voucher_type="Sales Order",
            voucher_no="TEST-FIN-001",
        )

        self.assertIn("gl_entries", result)
        gl_entries = result["gl_entries"]

        total_debit = sum(flt(e.get("debit", 0)) for e in gl_entries)
        total_credit = sum(flt(e.get("credit", 0)) for e in gl_entries)

        self.assertAlmostEqual(
            total_debit,
            total_credit,
            places=2,
            msg="GL entries must balance: debit must equal credit",
        )

    def test_clear_idempotency_cache(self):
        set_last_result("TEST-CLEAR-001", "submit", {"test": True})

        is_dup, _ = check_idempotency("TEST-CLEAR-001", "submit")
        self.assertTrue(is_dup)

        clear_idempotency_cache("TEST-CLEAR-001", "submit")

        is_dup, _ = check_idempotency("TEST-CLEAR-001", "submit")
        self.assertFalse(is_dup)

    def test_lock_auto_expires(self):
        lock_key, lock_value = acquire_lock("TEST-EXPIRE-001", timeout=2)
        self.assertIsNotNone(lock_key)

        time.sleep(3)

        lock_key2, lock_value2 = acquire_lock("TEST-EXPIRE-001", timeout=10)
        self.assertIsNotNone(lock_key2)

        if lock_key2:
            release_lock(lock_key2, lock_value2)

    def test_concurrent_order_and_inventory_modification(self):
        self._ensure_stock(100)

        so_name = self._make_draft_so(qty=30)

        order_results = []
        inventory_results = []
        inventory_errors = []

        def submit_order():
            try:
                result = submit_order_with_lock(so_name)
                order_results.append(result)
            except Exception as e:
                order_results.append(str(e))

        def deduct_inventory():
            try:
                result = atomic_deduct_stock(TEST_ITEM, TEST_WAREHOUSE, 30)
                inventory_results.append(result)
            except (InsufficientStockError, ConcurrentModificationError) as e:
                inventory_errors.append(str(e))

        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(submit_order)
            f2 = executor.submit(deduct_inventory)

            for f in as_completed([f1, f2]):
                try:
                    f.result()
                except Exception:
                    pass

        bin_data = self._get_bin_qty()
        actual = flt(bin_data.get("actual_qty", 0))
        self.assertTrue(
            operator.ge(actual, 0),
            f"Actual qty {actual} should not go negative after concurrent operations",
        )

    def test_order_status_enum_coverage(self):
        all_statuses = set(e.value for e in OrderStatus)
        so_status_field = frappe.get_meta("Sales Order").get_field("status")
        if so_status_field and so_status_field.options:
            allowed = set(so_status_field.options.split("\n"))
            for status in all_statuses:
                self.assertIn(
                    status,
                    allowed,
                    f"OrderStatus.{status} not in Sales Order status options",
                )


if __name__ == "__main__":
    unittest.main()
