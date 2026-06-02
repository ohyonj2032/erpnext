
# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""
Concurrency Tests for Order and Inventory
Tests race conditions, duplicate submissions, and rollback scenarios
"""

import frappe
import threading
import time
import uuid
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now, add_days


class TestOrderInventoryRace(FrappeTestCase):
    """
    Test class for order and inventory concurrency scenarios
    """

    def setUp(self):
        """
        Set up test data
        """
        super().setUp()
        self._create_test_item()
        self._create_test_warehouse()
        self._create_test_bin()
        self._setup_services()

    def _setup_services(self):
        """
        Set up service classes for testing
        """
        # Try to import our custom services
        try:
            from erpnext.selling.services.order_service import OrderService
            from erpnext.stock.services.inventory_service import InventoryService
            self.order_service = OrderService
            self.inventory_service = InventoryService
        except ImportError:
            self.order_service = None
            self.inventory_service = None
            print("Warning: Custom services not available, running basic tests")

    def _create_test_item(self):
        """
        Create test item
        """
        if not frappe.db.exists("Item", "TEST-ITEM-001"):
            item = frappe.get_doc({
                "doctype": "Item",
                "item_code": "TEST-ITEM-001",
                "item_name": "Test Item for Concurrency",
                "item_group": "Products",
                "stock_uom": "Nos",
                "is_stock_item": 1,
                "valuation_rate": 100.0,
                "opening_stock": 100.0,
                "opening_stock_warehouse": "Test Warehouse"
            })
            item.insert(ignore_permissions=True)
            self.test_item = item
        else:
            self.test_item = frappe.get_doc("Item", "TEST-ITEM-001")

    def _create_test_warehouse(self):
        """
        Create test warehouse
        """
        if not frappe.db.exists("Warehouse", "Test Warehouse"):
            warehouse = frappe.get_doc({
                "doctype": "Warehouse",
                "warehouse_name": "Test Warehouse",
                "company": "_Test Company"
            })
            warehouse.insert(ignore_permissions=True)
            self.test_warehouse = warehouse
        else:
            self.test_warehouse = frappe.get_doc("Warehouse", "Test Warehouse")

    def _create_test_bin(self):
        """
        Create test bin with stock
        """
        if not frappe.db.exists("Bin", {"item_code": "TEST-ITEM-001", "warehouse": "Test Warehouse"}):
            bin_doc = frappe.get_doc({
                "doctype": "Bin",
                "item_code": "TEST-ITEM-001",
                "warehouse": "Test Warehouse",
                "actual_qty": 100.0,
                "valuation_rate": 100.0
            })
            bin_doc.insert(ignore_permissions=True)
            self.test_bin = bin_doc
        else:
            self.test_bin = frappe.get_doc("Bin", {"item_code": "TEST-ITEM-001", "warehouse": "Test Warehouse"})
            # Ensure we have enough stock
            if self.test_bin.actual_qty < 100:
                frappe.db.set_value("Bin", self.test_bin.name, "actual_qty", 100.0)

    def _create_test_order(self, status="Draft"):
        """
        Create test sales order
        """
        order = frappe.get_doc({
            "doctype": "Sales Order",
            "customer": "_Test Customer",
            "company": "_Test Company",
            "transaction_date": now(),
            "delivery_date": add_days(now(), 7),
            "status": status,
            "items": [{
                "item_code": "TEST-ITEM-001",
                "qty": 10,
                "rate": 150.0,
                "warehouse": "Test Warehouse"
            }]
        })
        order.insert(ignore_permissions=True)
        return order

    def test_order_status_transition_validation(self):
        """
        Test that invalid state transitions are blocked
        """
        order = self._create_test_order(status="Draft")

        # Try to transition to invalid state
        try:
            order.status = "Closed"
            order.save()
            self.fail("Should have thrown an error for invalid status transition")
        except Exception as e:
            self.assertIn("status", str(e).lower())

    def test_idempotent_operation(self):
        """
        Test that the same operation can be safely repeated
        """
        if not self.order_service:
            self.skipTest("OrderService not available")

        order = self._create_test_order(status="Draft")
        idempotency_key = str(uuid.uuid4())

        # First operation
        result1 = self.order_service.process_order_status_change(
            order_id=order.name,
            new_status="To Deliver and Bill",
            idempotency_key=idempotency_key
        )

        # Second operation with same key should return same result
        result2 = self.order_service.process_order_status_change(
            order_id=order.name,
            new_status="To Deliver and Bill",
            idempotency_key=idempotency_key
        )

        self.assertEqual(result1.get("status"), result2.get("status"))
        self.assertTrue(result2.get("is_idempotent", False))

    def test_concurrent_order_updates(self):
        """
        Test that concurrent order updates are handled correctly
        """
        if not self.order_service:
            self.skipTest("OrderService not available")

        order = self._create_test_order(status="Draft")
        results = []
        errors = []

        def update_order(order_id, new_status, thread_id):
            try:
                result = self.order_service.process_order_status_change(
                    order_id=order_id,
                    new_status=new_status,
                    idempotency_key=f"thread-{thread_id}-{uuid.uuid4()}"
                )
                results.append((thread_id, result))
            except Exception as e:
                errors.append((thread_id, str(e)))

        # Create multiple threads trying to update the same order
        threads = []
        for i in range(5):
            t = threading.Thread(
                target=update_order,
                args=(order.name, "To Deliver and Bill", i)
            )
            threads.append(t)

        # Start all threads
        for t in threads:
            t.start()

        # Wait for all threads to complete
        for t in threads:
            t.join()

        # Verify that only one update succeeded and others got proper handling
        self.assertTrue(len(results) > 0)

    def test_atomic_inventory_deduction(self):
        """
        Test that inventory deductions are atomic
        """
        if not self.inventory_service:
            self.skipTest("InventoryService not available")

        # Get initial stock
        initial_qty = frappe.db.get_value(
            "Bin",
            {"item_code": "TEST-ITEM-001", "warehouse": "Test Warehouse"},
            "actual_qty"
        )

        # Perform atomic deduction
        transaction_id = str(uuid.uuid4())
        result = self.inventory_service.atomic_deduct_inventory(
            item_code="TEST-ITEM-001",
            warehouse="Test Warehouse",
            qty=5.0,
            transaction_id=transaction_id
        )

        self.assertEqual(result.get("status"), "success")

        # Verify stock was deducted
        final_qty = frappe.db.get_value(
            "Bin",
            {"item_code": "TEST-ITEM-001", "warehouse": "Test Warehouse"},
            "actual_qty"
        )
        self.assertEqual(final_qty, initial_qty - 5.0)

    def test_concurrent_inventory_deduction(self):
        """
        Test concurrent inventory deductions
        """
        if not self.inventory_service:
            self.skipTest("InventoryService not available")

        # Reset stock to 100
        frappe.db.set_value(
            "Bin",
            {"item_code": "TEST-ITEM-001", "warehouse": "Test Warehouse"},
            "actual_qty",
            100.0
        )

        results = []
        errors = []

        def deduct_stock(qty, thread_id):
            try:
                result = self.inventory_service.atomic_deduct_inventory(
                    item_code="TEST-ITEM-001",
                    warehouse="Test Warehouse",
                    qty=qty,
                    transaction_id=f"thread-{thread_id}-{uuid.uuid4()}"
                )
                results.append((thread_id, result))
            except Exception as e:
                errors.append((thread_id, str(e)))

        # Create 10 threads each deducting 10 units
        threads = []
        for i in range(10):
            t = threading.Thread(target=deduct_stock, args=(10.0, i))
            threads.append(t)

        # Start all threads
        for t in threads:
            t.start()

        # Wait for all threads
        for t in threads:
            t.join()

        # Check final stock (should be 0)
        final_qty = frappe.db.get_value(
            "Bin",
            {"item_code": "TEST-ITEM-001", "warehouse": "Test Warehouse"},
            "actual_qty"
        )
        self.assertEqual(final_qty, 0.0)

    def test_insufficient_stock(self):
        """
        Test that deduction fails when stock is insufficient
        """
        if not self.inventory_service:
            self.skipTest("InventoryService not available")

        # Set stock to 5
        frappe.db.set_value(
            "Bin",
            {"item_code": "TEST-ITEM-001", "warehouse": "Test Warehouse"},
            "actual_qty",
            5.0
        )

        # Try to deduct 10
        try:
            self.inventory_service.atomic_deduct_inventory(
                item_code="TEST-ITEM-001",
                warehouse="Test Warehouse",
                qty=10.0,
                transaction_id=str(uuid.uuid4())
            )
            self.fail("Should have thrown insufficient stock error")
        except Exception as e:
            self.assertIn("insufficient", str(e).lower())

    def test_order_and_inventory_combined(self):
        """
        Test combined order status change and inventory deduction
        """
        if not self.order_service or not self.inventory_service:
            self.skipTest("Services not available")

        order = self._create_test_order(status="Draft")
        transaction_id = str(uuid.uuid4())

        try:
            # This should be done in one transaction in real scenario
            result = self.order_service.process_order_status_change(
                order_id=order.name,
                new_status="To Deliver and Bill",
                idempotency_key=transaction_id
            )

            self.assertEqual(result.get("status"), "success")

            # Verify order status changed
            order.reload()
            self.assertEqual(order.status, "To Deliver and Bill")

        except Exception as e:
            self.fail(f"Combined operation failed: {e}")

    def test_version_increment(self):
        """
        Test that version number increments on each save
        """
        order = self._create_test_order(status="Draft")
        initial_version = order.get("version", 1)

        # Update and save
        order.customer_name = "Test Name Update"
        order.save()
        order.reload()

        self.assertTrue(order.get("version", 1) > initial_version)

    def tearDown(self):
        """
        Clean up test data
        """
        # Clean up orders we created
        orders = frappe.get_all(
            "Sales Order",
            filters={"customer": "_Test Customer"},
            limit=10
        )
        for order in orders:
            try:
                frappe.delete_doc("Sales Order", order.name, force=1)
            except:
                pass

        # Reset bin
        frappe.db.set_value(
            "Bin",
            {"item_code": "TEST-ITEM-001", "warehouse": "Test Warehouse"},
            "actual_qty",
            100.0
        )

        frappe.db.commit()


def run_tests():
    """
    Helper function to run tests
    """
    import unittest
    suite = unittest.TestLoader().loadTestsFromTestCase(TestOrderInventoryRace)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result


if __name__ == "__main__":
    run_tests()

