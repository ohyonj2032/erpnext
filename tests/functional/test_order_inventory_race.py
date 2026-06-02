import threading

import frappe

from erpnext.selling.doctype.sales_order.sales_order_extension import (
    extend_sales_order_class,
    update_sales_order_docfield,
)
from erpnext.selling.doctype.sales_order.test_sales_order import make_sales_order
from erpnext.selling.services.order_service import OrderService, RollbackTrackedError
from erpnext.stock.doctype.item.test_item import make_item
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.stock.services.inventory_service import InventoryOperationError, InventoryService, migrate_bin
from erpnext.tests.utils import ERPNextTestSuite


class TestOrderInventoryRace(ERPNextTestSuite):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        update_sales_order_docfield()
        extend_sales_order_class()
        migrate_bin()

    def setUp(self):
        super().setUp()
        self.item_code = f"RACE-ITEM-{frappe.generate_hash(length=8)}"
        make_item(
            self.item_code,
            {
                "is_stock_item": 1,
                "valuation_rate": 100,
                "standard_rate": 100,
                "stock_uom": "Nos",
                "item_defaults": [
                    {
                        "company": "_Test Company",
                        "default_warehouse": "_Test Warehouse - _TC",
                    }
                ],
            },
        )
        make_stock_entry(
            item_code=self.item_code,
            qty=40,
            to_warehouse="_Test Warehouse - _TC",
            rate=100,
        )
        self.order = make_sales_order(
            item_code=self.item_code,
            qty=5,
            rate=150,
            warehouse="_Test Warehouse - _TC",
            do_not_submit=True,
        )

    def test_duplicate_status_change_returns_idempotent_result(self):
        result_1 = OrderService.process_order_status_change(
            order_id=self.order.name,
            new_status="To Deliver and Bill",
        )
        result_2 = OrderService.process_order_status_change(
            order_id=self.order.name,
            new_status="To Deliver and Bill",
        )

        self.order.reload()

        self.assertTrue(result_1["success"])
        self.assertTrue(result_2["is_idempotent"])
        self.assertEqual(result_1["signature"], result_2["signature"])
        self.assertEqual(self.order.status, "To Deliver and Bill")
        self.assertEqual(self.order.signature, result_1["signature"])

    def test_inventory_deduction_increments_version(self):
        before = InventoryService.get_inventory_status(self.item_code, "_Test Warehouse - _TC")

        result = InventoryService.atomic_deduct_inventory(
            item_code=self.item_code,
            warehouse="_Test Warehouse - _TC",
            qty=3,
            transaction_id=f"txn-{frappe.generate_hash(length=8)}",
            expected_version=before["version"],
        )
        after = InventoryService.get_inventory_status(self.item_code, "_Test Warehouse - _TC")

        self.assertEqual(result["status"], "success")
        self.assertEqual(after["actual_qty"], before["actual_qty"] - 3)
        self.assertEqual(after["version"], before["version"] + 1)

    def test_inventory_version_mismatch_is_rejected(self):
        before = InventoryService.get_inventory_status(self.item_code, "_Test Warehouse - _TC")
        InventoryService.atomic_deduct_inventory(
            item_code=self.item_code,
            warehouse="_Test Warehouse - _TC",
            qty=2,
            transaction_id=f"txn-{frappe.generate_hash(length=8)}",
            expected_version=before["version"],
        )

        with self.assertRaises(InventoryOperationError):
            InventoryService.atomic_deduct_inventory(
                item_code=self.item_code,
                warehouse="_Test Warehouse - _TC",
                qty=1,
                transaction_id=f"txn-{frappe.generate_hash(length=8)}",
                expected_version=before["version"],
            )

    def test_same_order_payload_converges_under_concurrency(self):
        results = []
        errors = []
        start_gate = threading.Barrier(3)

        def worker():
            try:
                start_gate.wait()
                results.append(
                    OrderService.process_order_status_change(
                        order_id=self.order.name,
                        new_status="To Deliver and Bill",
                    )
                )
            except Exception as exc:
                if isinstance(exc, RollbackTrackedError):
                    errors.append(exc.result)
                else:
                    errors.append({"error": str(exc)})

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        start_gate.wait()
        for thread in threads:
            thread.join()

        self.order.reload()

        self.assertEqual(self.order.status, "To Deliver and Bill")
        self.assertTrue(any(result["success"] for result in results))
        self.assertTrue(all(result.get("success") or result.get("is_idempotent") for result in results))
        self.assertFalse(errors)

    def test_rollback_restores_order_and_inventory_and_is_replayable(self):
        before_order_status = self.order.status
        before_inventory = InventoryService.get_inventory_status(self.item_code, "_Test Warehouse - _TC")

        with self.assertRaises(RollbackTrackedError) as exc:
            OrderService.process_order_status_change(
                order_id=self.order.name,
                new_status="To Deliver and Bill",
                simulate_failure=True,
            )

        failure_result = exc.exception.result
        self.order.reload()
        after_inventory = InventoryService.get_inventory_status(self.item_code, "_Test Warehouse - _TC")
        replayed = OrderService.process_order_status_change(
            order_id=self.order.name,
            new_status="To Deliver and Bill",
            simulate_failure=True,
        )

        self.assertFalse(failure_result["success"])
        self.assertTrue(failure_result["rolled_back"])
        self.assertEqual(self.order.status, before_order_status)
        self.assertEqual(after_inventory["actual_qty"], before_inventory["actual_qty"])
        self.assertEqual(after_inventory["version"], before_inventory["version"])
        self.assertFalse(replayed["success"])
        self.assertTrue(replayed["is_idempotent"])
        self.assertTrue(self.order.replay_status_change_log()[-1]["rolled_back"])
