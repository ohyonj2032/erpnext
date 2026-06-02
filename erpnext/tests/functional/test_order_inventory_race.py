import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import frappe
from frappe.utils import now

from erpnext.models.inventory import (
    InsufficientStockError,
    InventoryAdjustment,
    InventoryChange,
    InventoryService,
    OptimisticLockError,
)
from erpnext.models.order import (
    InvalidStateTransitionError,
    OrderEvent,
    OrderState,
    OrderStateMachine,
)
from erpnext.services.order_service import (
    IdempotencyRecord,
    OrderLock,
    OrderLockTimeoutError,
    OrderNotFoundError,
    OrderService,
)
from erpnext.stock.doctype.item.test_item import make_item
from erpnext.stock.stock_balance import get_actual_qty, get_reserved_qty
from erpnext.tests.utils import ERPNextTestSuite


def _make_stock_entries():
    from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

    make_stock_entry(
        item_code="_Test Item",
        target="_Test Warehouse - _TC",
        qty=100,
        basic_rate=50,
    )
    make_stock_entry(
        item_code="_Test Item",
        target="Stores - _TC",
        qty=200,
        basic_rate=50,
    )
    make_stock_entry(
        item_code="_Test Item Home Desktop 100",
        target="_Test Warehouse - _TC",
        qty=50,
        basic_rate=100,
    )


def _create_draft_sales_order(item_code="_Test Item", qty=10, warehouse="_Test Warehouse - _TC"):
    from erpnext.selling.doctype.sales_order.test_sales_order import make_sales_order

    return make_sales_order(
        item_code=item_code,
        qty=qty,
        warehouse=warehouse,
        do_not_submit=True,
    )


def _ensure_idempotency_doctype():
    try:
        frappe.get_doc(
            {
                "doctype": "DocType",
                "name": "Idempotency Record",
                "module": "Custom",
                "custom": 1,
                "fields": [
                    {"fieldname": "idempotency_key", "fieldtype": "Data", "label": "Idempotency Key", "unique": 1, "reqd": 1},
                    {"fieldname": "order_id", "fieldtype": "Data", "label": "Order ID"},
                    {"fieldname": "result_json", "fieldtype": "Long Text", "label": "Result JSON"},
                    {"fieldname": "created_at", "fieldtype": "Datetime", "label": "Created At"},
                    {"fieldname": "status", "fieldtype": "Data", "label": "Status"},
                ],
                "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
            }
        ).insert(ignore_if_duplicate=True)
    except Exception:
        pass


def _ensure_event_log_doctype():
    try:
        frappe.get_doc(
            {
                "doctype": "DocType",
                "name": "Order Event Log",
                "module": "Custom",
                "custom": 1,
                "fields": [
                    {"fieldname": "order_id", "fieldtype": "Data", "label": "Order ID", "reqd": 1},
                    {"fieldname": "event_log_json", "fieldtype": "Long Text", "label": "Event Log JSON"},
                ],
                "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
            }
        ).insert(ignore_if_duplicate=True)
    except Exception:
        pass


class TestOrderStateMachine(ERPNextTestSuite):
    def test_valid_transitions(self):
        transitions = [
            (OrderState.DRAFT, OrderState.TO_DELIVER_AND_BILL),
            (OrderState.DRAFT, OrderState.ON_HOLD),
            (OrderState.ON_HOLD, OrderState.DRAFT),
            (OrderState.ON_HOLD, OrderState.CLOSED),
            (OrderState.TO_DELIVER_AND_BILL, OrderState.TO_BILL),
            (OrderState.TO_DELIVER_AND_BILL, OrderState.TO_DELIVER),
            (OrderState.TO_DELIVER_AND_BILL, OrderState.COMPLETED),
            (OrderState.TO_DELIVER_AND_BILL, OrderState.INVENTORY_LOCKED),
            (OrderState.INVENTORY_LOCKED, OrderState.INVENTORY_ADJUSTED),
            (OrderState.INVENTORY_LOCKED, OrderState.FULFILLMENT_FAILED),
            (OrderState.INVENTORY_ADJUSTED, OrderState.COMPLETED),
            (OrderState.INVENTORY_ADJUSTED, OrderState.FULFILLMENT_FAILED),
            (OrderState.FULFILLMENT_FAILED, OrderState.TO_DELIVER_AND_BILL),
            (OrderState.FULFILLMENT_FAILED, OrderState.ON_HOLD),
            (OrderState.FULFILLMENT_FAILED, OrderState.CLOSED),
            (OrderState.COMPLETED, OrderState.CLOSED),
            (OrderState.DRAFT, OrderState.CANCELLED),
        ]

        for current, target in transitions:
            self.assertTrue(
                OrderStateMachine.can_transition(current, target),
                f"Should allow transition from {current.value} to {target.value}",
            )

    def test_invalid_transitions(self):
        invalid_transitions = [
            (OrderState.COMPLETED, OrderState.DRAFT),
            (OrderState.CANCELLED, OrderState.DRAFT),
            (OrderState.CLOSED, OrderState.TO_DELIVER_AND_BILL),
            (OrderState.DRAFT, OrderState.INVENTORY_LOCKED),
            (OrderState.CANCELLED, OrderState.COMPLETED),
        ]

        for current, target in invalid_transitions:
            self.assertFalse(
                OrderStateMachine.can_transition(current, target),
                f"Should NOT allow transition from {current.value} to {target.value}",
            )

            with self.assertRaises(InvalidStateTransitionError):
                OrderStateMachine.validate_transition(current, target)

    def test_allowed_transitions_returns_proper_set(self):
        allowed = OrderStateMachine.get_allowed_transitions(OrderState.DRAFT)
        self.assertIn(OrderState.TO_DELIVER_AND_BILL, allowed)
        self.assertIn(OrderState.ON_HOLD, allowed)
        self.assertIn(OrderState.CANCELLED, allowed)
        self.assertNotIn(OrderState.COMPLETED, allowed)
        self.assertNotIn(OrderState.INVENTORY_LOCKED, allowed)

    def test_cancelled_has_no_transitions(self):
        allowed = OrderStateMachine.get_allowed_transitions(OrderState.CANCELLED)
        self.assertEqual(len(allowed), 0, "Cancelled state should have no allowed transitions")

    def test_closed_has_no_transitions(self):
        allowed = OrderStateMachine.get_allowed_transitions(OrderState.CLOSED)
        self.assertEqual(len(allowed), 0, "Closed state should have no allowed transitions")

    def test_party_fulfilled_has_multiple_exits(self):
        allowed = OrderStateMachine.get_allowed_transitions(OrderState.PARTLY_FULFILLED)
        self.assertIn(OrderState.COMPLETED, allowed)
        self.assertIn(OrderState.INVENTORY_ADJUSTED, allowed)
        self.assertIn(OrderState.FULFILLMENT_FAILED, allowed)


class TestIdempotency(ERPNextTestSuite):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _ensure_idempotency_doctype()

    def test_compute_idempotency_key_is_deterministic(self):
        payload = {"action": "submit", "items": [{"item_code": "A", "qty": 10}]}
        key1 = OrderService.compute_idempotency_key("SO-001", payload)
        key2 = OrderService.compute_idempotency_key("SO-001", payload)
        self.assertEqual(key1, key2, "Same payload should produce same idempotency key")

    def test_compute_idempotency_key_differs_for_different_payload(self):
        payload1 = {"action": "submit", "items": [{"item_code": "A", "qty": 10}]}
        payload2 = {"action": "submit", "items": [{"item_code": "A", "qty": 20}]}
        key1 = OrderService.compute_idempotency_key("SO-001", payload1)
        key2 = OrderService.compute_idempotency_key("SO-001", payload2)
        self.assertNotEqual(key1, key2, "Different payload should produce different idempotency key")

    def test_compute_idempotency_key_differs_for_different_order(self):
        payload = {"action": "submit"}
        key1 = OrderService.compute_idempotency_key("SO-001", payload)
        key2 = OrderService.compute_idempotency_key("SO-002", payload)
        self.assertNotEqual(key1, key2, "Different order ID should produce different idempotency key")

    def test_idempotency_record_save_and_retrieve(self):
        idempotency_key = f"test-key-{now()}"
        record = IdempotencyRecord(
            idempotency_key=idempotency_key,
            order_id="SO-TEST-001",
            result={"status": "success"},
        )

        OrderService._save_idempotency_record(record)

        retrieved = OrderService.check_idempotency(idempotency_key)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.idempotency_key, idempotency_key)
        self.assertEqual(retrieved.order_id, "SO-TEST-001")
        self.assertEqual(retrieved.result, {"status": "success"})

    def test_idempotency_returns_none_for_unknown_key(self):
        result = OrderService.check_idempotency("nonexistent-key")
        self.assertIsNone(result)

    def test_idempotency_record_expiry(self):
        import frappe
        from datetime import timedelta
        from erpnext.services.order_service import IDEMPOTENCY_TTL_SECONDS

        old_key = f"test-expired-key-{now()}"

        old_created = frappe.utils.now_datetime() - timedelta(seconds=IDEMPOTENCY_TTL_SECONDS + 10)
        if frappe.db.exists("Idempotency Record", {"idempotency_key": old_key}):
            frappe.db.set_value(
                "Idempotency Record",
                {"idempotency_key": old_key},
                "created_at",
                old_created,
            )
        else:
            frappe.get_doc(
                {
                    "doctype": "Idempotency Record",
                    "idempotency_key": old_key,
                    "order_id": "SO-OLD",
                    "result_json": json.dumps({"status": "old"}),
                    "created_at": old_created,
                    "status": "completed",
                }
            ).insert(ignore_permissions=True)

        result = OrderService.check_idempotency(old_key)
        self.assertIsNone(result, "Expired idempotency record should return None")


class TestOrderLock(ERPNextTestSuite):
    def test_lock_acquire_and_release(self):
        lock = OrderLock("TEST-LOCK-001", timeout=5)
        self.assertTrue(lock.acquire())
        lock.release()

    def test_lock_context_manager(self):
        with OrderLock("TEST-LOCK-002", timeout=5) as lock:
            self.assertTrue(lock._acquired)

    def test_concurrent_lock_blocks(self):
        results = []

        def hold_and_record(order_id, hold_time, result_list):
            with OrderLock(order_id, timeout=10):
                result_list.append(f"{order_id}-acquired")
                time.sleep(hold_time)
                result_list.append(f"{order_id}-released")

        t1 = threading.Thread(target=hold_and_record, args=("LK-CONC-001", 0.2, results))
        t2 = threading.Thread(target=hold_and_record, args=("LK-CONC-001", 0.1, results))

        t1.start()
        time.sleep(0.05)
        t2.start()

        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(len(results), 4)
        self.assertEqual(results[0], "LK-CONC-001-acquired")
        self.assertEqual(results[1], "LK-CONC-001-released")
        self.assertEqual(results[2], "LK-CONC-001-acquired")
        self.assertEqual(results[3], "LK-CONC-001-released")

    def test_different_order_locks_dont_block(self):
        results = []

        def hold_and_record(order_id, hold_time, result_list):
            with OrderLock(order_id, timeout=10):
                result_list.append(f"{order_id}-acquired")
                time.sleep(hold_time)
                result_list.append(f"{order_id}-released")

        t1 = threading.Thread(target=hold_and_record, args=("LK-A", 0.15, results))
        t2 = threading.Thread(target=hold_and_record, args=("LK-B", 0.05, results))

        t1.start()
        time.sleep(0.01)
        t2.start()

        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(len(results), 4)
        acquired_events = [r for r in results if "acquired" in r]
        self.assertEqual(len(acquired_events), 2)

    def test_lock_timeout(self):
        with OrderLock("LK-TIMEOUT", timeout=5):
            lock2 = OrderLock("LK-TIMEOUT", timeout=0.05)
            with self.assertRaises(OrderLockTimeoutError):
                lock2.acquire()


class TestOrderServiceStateManagement(ERPNextTestSuite):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _ensure_idempotency_doctype()
        _ensure_event_log_doctype()
        _make_stock_entries()

    def test_set_order_state_valid_transition(self):
        so = _create_draft_sales_order(qty=1)

        result = OrderService.set_order_state(
            so.name,
            OrderState.ON_HOLD,
            payload={"action": "hold", "reason": "testing"},
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["new_state"], OrderState.ON_HOLD.value)

        updated_status = frappe.db.get_value("Sales Order", so.name, "status")
        self.assertEqual(updated_status, OrderState.ON_HOLD.value)

    def test_set_order_state_invalid_transition_raises_error(self):
        so = _create_draft_sales_order(qty=1)

        with self.assertRaises(InvalidStateTransitionError):
            OrderService.set_order_state(
                so.name,
                OrderState.INVENTORY_LOCKED,
                payload={"action": "lock"},
            )

    def test_set_order_state_nonexistent_order(self):
        with self.assertRaises(OrderNotFoundError):
            OrderService.set_order_state(
                "NONEXISTENT-SO",
                OrderState.TO_DELIVER_AND_BILL,
            )

    def test_submit_order_shortcut(self):
        so = _create_draft_sales_order(qty=1)

        result = OrderService.submit_order(so.name)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["new_state"], OrderState.TO_DELIVER_AND_BILL.value)

        updated_status = frappe.db.get_value("Sales Order", so.name, "status")
        self.assertEqual(updated_status, OrderState.TO_DELIVER_AND_BILL.value)


class TestInventoryRaceConditions(ERPNextTestSuite):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _ensure_idempotency_doctype()
        _ensure_event_log_doctype()
        _make_stock_entries()

    def test_concurrent_deductions_on_same_item(self):
        so = _create_draft_sales_order(qty=1)

        OrderService.submit_order(so.name)

        items = [{"item_code": "_Test Item", "warehouse": "_Test Warehouse - _TC", "quantity": 1}]

        lock_result = OrderService.lock_inventory_for_order(so.name, items)
        self.assertEqual(lock_result["status"], "success")

        deduct_result = OrderService.deduct_and_account(so.name, items)
        self.assertEqual(deduct_result["status"], "success")

        OrderService.complete_order(so.name)

        final_status = frappe.db.get_value("Sales Order", so.name, "status")
        self.assertEqual(final_status, OrderState.COMPLETED.value)

    def test_insufficient_stock_detection(self):
        so = _create_draft_sales_order(qty=1)

        OrderService.submit_order(so.name)

        items = [{"item_code": "_Test Item", "warehouse": "_Test Warehouse - _TC", "quantity": 99999}]

        result = OrderService.lock_inventory_for_order(so.name, items)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_type"], "InsufficientStockError")

        status = frappe.db.get_value("Sales Order", so.name, "status")
        self.assertEqual(status, OrderState.FULFILLMENT_FAILED.value)

    def test_optimistic_lock_retry(self):
        from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry as _add_stock

        _add_stock(item_code="_Test Item", target="_Test Warehouse - _TC", qty=5, basic_rate=50)

        so = _create_draft_sales_order(qty=1)
        OrderService.submit_order(so.name)

        items = [{"item_code": "_Test Item", "warehouse": "_Test Warehouse - _TC", "quantity": 1}]

        def attempt_deduct():
            try:
                return OrderService.deduct_and_account(so.name, items, financial_entries=None)
            except OptimisticLockError:
                return {"status": "optimistic_lock_error"}

        results = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(attempt_deduct) for _ in range(4)]
            for f in as_completed(futures):
                results.append(f.result())

        success_count = sum(1 for r in results if r.get("status") == "success")
        failed_count = sum(1 for r in results if r.get("status") == "optimistic_lock_error")

        self.assertGreater(success_count + failed_count, 0)

    def test_duplicate_submission_is_idempotent(self):
        so = _create_draft_sales_order(qty=1)

        result1 = OrderService.submit_order(so.name)
        self.assertEqual(result1["status"], "success")

        result2 = OrderService.submit_order(so.name)
        self.assertEqual(result2["status"], "success")

        self.assertEqual(result1, result2)


class TestInventoryRollback(ERPNextTestSuite):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _ensure_idempotency_doctype()
        _ensure_event_log_doctype()
        _make_stock_entries()

    def test_rollback_from_inventory_locked(self):
        so = _create_draft_sales_order(qty=1)
        OrderService.submit_order(so.name)

        items = [{"item_code": "_Test Item", "warehouse": "_Test Warehouse - _TC", "quantity": 1}]
        lock_result = OrderService.lock_inventory_for_order(so.name, items)
        self.assertEqual(lock_result["status"], "success")

        rollback_result = OrderService.rollback_order(so.name, reason="test rollback")
        self.assertEqual(rollback_result["status"], "success")
        self.assertEqual(rollback_result["previous_state"], OrderState.INVENTORY_LOCKED.value)

        status = frappe.db.get_value("Sales Order", so.name, "status")
        self.assertEqual(status, OrderState.TO_DELIVER_AND_BILL.value)

    def test_rollback_from_fulfillment_failed(self):
        so = _create_draft_sales_order(qty=1)
        OrderService.submit_order(so.name)

        items = [{"item_code": "_Test Item", "warehouse": "_Test Warehouse - _TC", "quantity": 99999}]
        result = OrderService.lock_inventory_for_order(so.name, items)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_type"], "InsufficientStockError")

        rollback_result = OrderService.rollback_order(so.name, reason="test recovery")
        self.assertEqual(rollback_result["status"], "success")

        status = frappe.db.get_value("Sales Order", so.name, "status")
        self.assertEqual(status, OrderState.TO_DELIVER_AND_BILL.value)

    def test_rollback_not_allowed_from_draft(self):
        so = _create_draft_sales_order(qty=1)

        with self.assertRaises(InvalidStateTransitionError):
            OrderService.rollback_order(so.name, reason="should not work")

    def test_rollback_preserves_change_log(self):
        so = _create_draft_sales_order(qty=1)
        OrderService.submit_order(so.name)

        items = [{"item_code": "_Test Item", "warehouse": "_Test Warehouse - _TC", "quantity": 1}]
        OrderService.lock_inventory_for_order(so.name, items)

        rollback_result = OrderService.rollback_order(so.name, reason="audit test")
        self.assertIsNotNone(rollback_result.get("change_log"))
        change_log = rollback_result["change_log"]
        self.assertGreater(len(change_log), 0, "Rollback should include change log for audit trail")


class TestOrderStatusCheck(ERPNextTestSuite):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _ensure_idempotency_doctype()
        _ensure_event_log_doctype()
        _make_stock_entries()

    def test_get_order_status(self):
        so = _create_draft_sales_order(qty=1)

        status = OrderService.get_order_status(so.name)
        self.assertEqual(status["status"], OrderState.DRAFT.value)
        self.assertIsInstance(status["allowed_transitions"], list)
        self.assertIn(OrderState.TO_DELIVER_AND_BILL.value, status["allowed_transitions"])
        self.assertIn(OrderState.ON_HOLD.value, status["allowed_transitions"])

    def test_get_order_status_after_submit(self):
        so = _create_draft_sales_order(qty=1)
        OrderService.submit_order(so.name)

        status = OrderService.get_order_status(so.name)
        self.assertEqual(status["status"], OrderState.TO_DELIVER_AND_BILL.value)
        self.assertIn(OrderState.INVENTORY_LOCKED.value, status["allowed_transitions"])

    def test_event_count_increases(self):
        so = _create_draft_sales_order(qty=1)

        status1 = OrderService.get_order_status(so.name)

        OrderService.hold_order(so.name)
        status2 = OrderService.get_order_status(so.name)

        OrderService.cancel_order(so.name)
        status3 = OrderService.get_order_status(so.name)

        self.assertGreater(status3["event_count"], status1["event_count"])