import json
import threading
from pathlib import Path

import frappe

from erpnext.selling.doctype.sales_order.sales_order_extension import (
    extend_sales_order_class,
    update_sales_order_docfield,
)
from erpnext.selling.services.order_service import OrderService, RollbackTrackedError
from erpnext.selling.doctype.sales_order.test_sales_order import make_sales_order
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
        fixture_path = Path(__file__).resolve().parent / "fixtures" / "initial_data.json"
        cls.initial_data = json.loads(fixture_path.read_text())

    def setUp(self):
        super().setUp()
        sales_order_fixture = self.initial_data["sales_order"]
        inventory_fixture = self.initial_data["inventory"]
        self.item_code = f"RACE-ITEM-{frappe.generate_hash(length=8)}"
        make_item(
            self.item_code,
            {
                "is_stock_item": 1,
                "valuation_rate": inventory_fixture["valuation_rate"],
                "standard_rate": inventory_fixture["valuation_rate"],
                "stock_uom": sales_order_fixture["stock_uom"],
                "item_defaults": [
                    {
                        "company": sales_order_fixture["company"],
                        "default_warehouse": inventory_fixture["warehouse"],
                    }
                ],
            },
        )
        make_stock_entry(
            item_code=self.item_code,
            qty=inventory_fixture["actual_qty"],
            to_warehouse=inventory_fixture["warehouse"],
            rate=inventory_fixture["valuation_rate"],
        )
        self.order = make_sales_order(
            item_code=self.item_code,
            qty=sales_order_fixture["qty"],
            rate=sales_order_fixture["rate"],
            warehouse=inventory_fixture["warehouse"],
            do_not_submit=True,
        )
        self.expected_status = sales_order_fixture["target_status"]
        self.warehouse = inventory_fixture["warehouse"]
        self.expected_ledger_amount = sales_order_fixture["qty"] * sales_order_fixture["rate"]

    def test_duplicate_status_change_returns_idempotent_result(self):
        before_metrics = OrderService.get_metric_snapshot()

        result_1 = OrderService.process_order_status_change(
            order_id=self.order.name,
            new_status=self.expected_status,
        )
        result_2 = OrderService.process_order_status_change(
            order_id=self.order.name,
            new_status=self.expected_status,
        )

        self.order.reload()
        after_metrics = OrderService.get_metric_snapshot()

        self.assertTrue(result_1["success"])
        self.assertTrue(result_2["is_idempotent"])
        self.assertEqual(result_1["signature"], result_2["signature"])
        self.assertEqual(result_1["accounting_summary"]["ledger_amount_total"], self.expected_ledger_amount)
        self.assertEqual(self.order.status, self.expected_status)
        self.assertEqual(self.order.signature, result_1["signature"])
        self.assertGreaterEqual(after_metrics["idempotent_hits"], before_metrics["idempotent_hits"] + 1)

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
        self.assertEqual(result["expected_version"], before["version"])
        self.assertEqual(after["actual_qty"], before["actual_qty"] - 3)
        self.assertEqual(after["version"], before["version"] + 1)

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
        before_metrics = OrderService.get_metric_snapshot()
        results = []
        errors = []
        start_gate = threading.Barrier(3)

        def worker():
            try:
                start_gate.wait()
                results.append(
                    OrderService.process_order_status_change(
                        order_id=self.order.name,
                        new_status=self.expected_status,
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
        after_metrics = OrderService.get_metric_snapshot()

        self.assertEqual(self.order.status, self.expected_status)
        self.assertTrue(any(result["success"] for result in results))
        self.assertTrue(all(result.get("success") or result.get("is_idempotent") for result in results))
        self.assertFalse(errors)
        self.assertGreaterEqual(after_metrics["lock_acquisitions"], before_metrics["lock_acquisitions"] + 2)
        self.assertGreaterEqual(after_metrics["lock_releases"], before_metrics["lock_releases"] + 2)
        self.assertGreaterEqual(after_metrics["idempotent_hits"], before_metrics["idempotent_hits"] + 1)

    def test_rollback_restores_order_and_inventory_and_is_replayable(self):
        before_order_status = self.order.status
        before_inventory = InventoryService.get_inventory_status(self.item_code, self.warehouse)

        with self.assertRaises(RollbackTrackedError) as exc:
            OrderService.process_order_status_change(
                order_id=self.order.name,
                new_status=self.expected_status,
                simulate_failure=True,
            )

        failure_result = exc.exception.result
        self.order.reload()
        after_inventory = InventoryService.get_inventory_status(self.item_code, self.warehouse)
        replayed = OrderService.process_order_status_change(
            order_id=self.order.name,
            new_status=self.expected_status,
            simulate_failure=True,
        )
        replay_log = self.order.replay_status_change_log()

        self.assertFalse(failure_result["success"])
        self.assertTrue(failure_result["rolled_back"])
        self.assertEqual(failure_result["rollback_state"]["status"], before_order_status)
        self.assertEqual(failure_result["accounting_summary"]["ledger_amount_total"], self.expected_ledger_amount)
        self.assertEqual(self.order.status, before_order_status)
        self.assertEqual(after_inventory["actual_qty"], before_inventory["actual_qty"])
        self.assertEqual(after_inventory["version"], before_inventory["version"])
        self.assertFalse(replayed["success"])
        self.assertTrue(replayed["is_idempotent"])
        self.assertTrue(replay_log[-1]["rolled_back"])
        self.assertEqual(replay_log[-1]["result"]["rollback_state"]["status"], before_order_status)
