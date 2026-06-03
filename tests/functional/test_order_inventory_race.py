import json
import threading
from pathlib import Path

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


FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "initial_data.json"


class TestOrderInventoryRace(ERPNextTestSuite):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fixture = json.loads(FIXTURE_PATH.read_text())
        update_sales_order_docfield()
        extend_sales_order_class()
        migrate_bin()

    def setUp(self):
        super().setUp()
        self.snapshot = self.fixture
        self.warehouse = self.snapshot["inventory"]["warehouse"]
        self.stock_qty = self.snapshot["inventory"]["actual_qty"]
        self.order_qty = self.snapshot["sales_order"]["items"][0]["qty"]
        self.order_rate = self.snapshot["sales_order"]["items"][0]["rate"]
        self.expected_ledger_amount = self.snapshot["expected_reconciliation"]["ledger_amount"]
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
                        "company": self.snapshot["sales_order"]["company"],
                        "default_warehouse": self.warehouse,
                    }
                ],
            },
        )
        make_stock_entry(
            item_code=self.item_code,
            qty=self.stock_qty,
            to_warehouse=self.warehouse,
            rate=100,
        )
        self.order = make_sales_order(
            item_code=self.item_code,
            qty=self.order_qty,
            rate=self.order_rate,
            warehouse=self.warehouse,
            do_not_submit=True,
        )

    def test_duplicate_status_change_returns_idempotent_result(self):
        before_version = int(self.order.get("version") or 0)

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
        self.assertEqual(int(self.order.get("version") or 0), before_version + 1)
        self.assertEqual(result_1["reconciliation"]["requested_inventory_qty"], self.order_qty)
        self.assertEqual(result_1["reconciliation"]["applied_inventory_qty"], self.order_qty)
        self.assertEqual(result_1["reconciliation"]["ledger_amount"], self.expected_ledger_amount)
        self.assertTrue(result_1["reconciliation"]["is_consistent"])

    def test_inventory_deduction_increments_version(self):
        before = InventoryService.get_inventory_status(self.item_code, self.warehouse)

        result = InventoryService.atomic_deduct_inventory(
            item_code=self.item_code,
            warehouse=self.warehouse,
            qty=3,
            transaction_id=f"txn-{frappe.generate_hash(length=8)}",
            expected_version=before["version"],
        )
        after = InventoryService.get_inventory_status(self.item_code, self.warehouse)

        self.assertEqual(result["status"], "success")
        self.assertEqual(after["actual_qty"], before["actual_qty"] - 3)
        self.assertEqual(after["version"], before["version"] + 1)
        self.assertEqual(result["bin_snapshot"]["available_qty"], after["actual_qty"] - after["reserved_qty"])

    def test_inventory_version_mismatch_is_rejected(self):
        before = InventoryService.get_inventory_status(self.item_code, self.warehouse)
        InventoryService.atomic_deduct_inventory(
            item_code=self.item_code,
            warehouse=self.warehouse,
            qty=2,
            transaction_id=f"txn-{frappe.generate_hash(length=8)}",
            expected_version=before["version"],
        )

        with self.assertRaises(InventoryOperationError):
            InventoryService.atomic_deduct_inventory(
                item_code=self.item_code,
                warehouse=self.warehouse,
                qty=1,
                transaction_id=f"txn-{frappe.generate_hash(length=8)}",
                expected_version=before["version"],
            )

    def test_same_order_payload_converges_under_concurrency(self):
        before_version = int(self.order.get("version") or 0)
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
        self.assertEqual(int(self.order.get("version") or 0), before_version + 1)
        self.assertEqual(len(results), 2)
        self.assertTrue(any(result["success"] for result in results))
        self.assertTrue(any(result.get("is_idempotent") for result in results))
        self.assertTrue(all(result.get("success") or result.get("is_idempotent") for result in results))
        self.assertFalse(errors)

    def test_rollback_restores_order_and_inventory_and_is_replayable(self):
        before_order_status = self.order.status
        before_order_version = int(self.order.get("version") or 0)
        before_inventory = InventoryService.get_inventory_status(self.item_code, self.warehouse)

        with self.assertRaises(RollbackTrackedError) as exc:
            OrderService.process_order_status_change(
                order_id=self.order.name,
                new_status="To Deliver and Bill",
                simulate_failure=True,
            )

        failure_result = exc.exception.result
        self.order.reload()
        after_inventory = InventoryService.get_inventory_status(self.item_code, self.warehouse)
        replayed = OrderService.process_order_status_change(
            order_id=self.order.name,
            new_status="To Deliver and Bill",
            simulate_failure=True,
        )
        replay_log = self.order.replay_status_change_log()

        self.assertFalse(failure_result["success"])
        self.assertTrue(failure_result["rolled_back"])
        self.assertEqual(failure_result["rollback_state"]["status"], before_order_status)
        self.assertEqual(failure_result["reconciliation"]["requested_inventory_qty"], self.order_qty)
        self.assertEqual(failure_result["reconciliation"]["ledger_amount"], self.expected_ledger_amount)
        self.assertEqual(self.order.status, before_order_status)
        self.assertGreaterEqual(int(self.order.get("version") or 0), before_order_version)
        self.assertEqual(after_inventory["actual_qty"], before_inventory["actual_qty"])
        self.assertEqual(after_inventory["version"], before_inventory["version"])
        self.assertFalse(replayed["success"])
        self.assertTrue(replayed["is_idempotent"])
        self.assertTrue(replay_log[-1]["rolled_back"])
        self.assertEqual(replay_log[-1]["result"]["rollback_state"]["status"], before_order_status)
