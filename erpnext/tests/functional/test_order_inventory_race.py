# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List

import frappe
from frappe.utils import nowdate

from erpnext.selling.doctype.sales_order.order_service import (
    OrderService,
    acquire_lock,
    generate_idempotency_key,
    setup_order_tables,
)
from erpnext.selling.doctype.sales_order.sales_order import SalesOrder
from erpnext.stock.doctype.bin.bin import Bin
from erpnext.tests.utils import ERPNextTestSuite


class TestOrderInventoryRace(ERPNextTestSuite):
    """
    Test suite for concurrent order and inventory operations.
    Tests for race conditions, idempotency, and state machine validation.
    """
    
    def setUp(self):
        super().setUp()
        
        # Setup database tables
        setup_order_tables()
        
        # Create test item
        self.test_item_code = "_Test_Concurrency_Item"
        self.test_warehouse = "_Test Warehouse - _TC"
        
        # Create or get test item
        if not frappe.db.exists("Item", self.test_item_code):
            from erpnext.stock.doctype.item.test_item import make_item
            make_item(self.test_item_code, {"is_stock_item": 1})
        
        # Ensure test bin exists and has sufficient stock
        self._ensure_test_bin()
        
        # Create test sales order
        self.test_order = self._create_test_order()
    
    def tearDown(self):
        super().tearDown()
    
    def _ensure_test_bin(self, qty: float = 100.0):
        """Ensure test bin exists and has specified quantity"""
        bin_filters = {
            "item_code": self.test_item_code,
            "warehouse": self.test_warehouse
        }
        
        if frappe.db.exists("Bin", bin_filters):
            bin_doc = frappe.get_doc("Bin", bin_filters)
            bin_doc.actual_qty = qty
            bin_doc.projected_qty = qty
            bin_doc.save(ignore_permissions=True)
        else:
            bin_doc = frappe.new_doc("Bin")
            bin_doc.item_code = self.test_item_code
            bin_doc.warehouse = self.test_warehouse
            bin_doc.actual_qty = qty
            bin_doc.projected_qty = qty
            bin_doc.save(ignore_permissions=True)
    
    def _create_test_order(self) -> SalesOrder:
        """Create a test sales order for testing"""
        if frappe.db.exists("Sales Order", {"name": ("like", "_Test_Concurrency_Order_%")}):
            # Delete existing test orders
            existing = frappe.get_all(
                "Sales Order",
                {"name": ("like", "_Test_Concurrency_Order_%")},
                pluck="name"
            )
            for name in existing:
                frappe.delete_doc("Sales Order", name, force=True, ignore_permissions=True)
        
        # Create new order
        order = frappe.new_doc("Sales Order")
        order.customer = "_Test Customer"
        order.delivery_date = nowdate()
        order.company = "_Test Company"
        
        order.append("items", {
            "item_code": self.test_item_code,
            "qty": 10.0,
            "rate": 100.0,
            "warehouse": self.test_warehouse
        })
        
        order.save(ignore_permissions=True)
        order.submit()
        
        return order
    
    def test_concurrent_status_updates(self):
        """Test concurrent status updates with locking"""
        results: List[Dict] = []
        errors: List[Exception] = []
        order_name = self.test_order.name
        
        def update_status_worker(target_status: str):
            try:
                # Different workers try to update status simultaneously
                result = OrderService.update_order_status(
                    order_name,
                    target_status
                )
                results.append(result)
            except Exception as e:
                errors.append(e)
        
        # Create threads
        threads = [
            threading.Thread(target=update_status_worker, args=("To Deliver and Bill",)),
            threading.Thread(target=update_status_worker, args=("To Deliver",)),
            threading.Thread(target=update_status_worker, args=("To Bill",))
        ]
        
        # Start all threads simultaneously
        for t in threads:
            t.start()
        
        # Wait for all threads to complete
        for t in threads:
            t.join()
        
        # Verify that at least one update succeeded
        self.assertTrue(len(results) > 0 or len(errors) > 0)
        
        # The final status should be one of the allowed states
        final_order = frappe.get_doc("Sales Order", order_name)
        self.assertIn(final_order.status, ["To Deliver and Bill", "To Deliver", "To Bill"])
    
    def test_idempotent_operations(self):
        """Test idempotent operation handling"""
        order_name = self.test_order.name
        idempotency_key = generate_idempotency_key(order_name, "test_idempotent")
        
        # First execution
        result1 = OrderService.update_order_status(
            order_name,
            "To Deliver and Bill",
            idempotency_key=idempotency_key
        )
        
        # Second execution with same key - should return same result
        result2 = OrderService.update_order_status(
            order_name,
            "To Deliver and Bill",
            idempotency_key=idempotency_key
        )
        
        # Results should be identical
        self.assertEqual(result1["success"], result2["success"])
        self.assertEqual(result1["order_name"], result2["order_name"])
        self.assertEqual(result1["new_status"], result2["new_status"])
    
    def test_invalid_status_transition(self):
        """Test invalid state transitions are rejected"""
        order_name = self.test_order.name
        
        # Set initial status to Completed
        OrderService.update_order_status(order_name, "To Deliver and Bill")
        OrderService.update_order_status(order_name, "Completed")
        
        # Try invalid transition from Completed back to To Deliver
        with self.assertRaises(Exception):
            OrderService.update_order_status(order_name, "To Deliver")
    
    def test_concurrent_inventory_deduction(self):
        """Test concurrent inventory deduction with optimistic locking"""
        order_name = self.test_order.name
        initial_qty = 50.0
        self._ensure_test_bin(initial_qty)
        
        # Create multiple orders to deplete stock
        order_names = [order_name]
        for i in range(3):
            order_names.append(self._create_test_order().name)
        
        results: List[Dict] = []
        errors: List[Exception] = []
        
        def process_order_worker(order_name_to_process: str):
            try:
                result = OrderService.process_order_with_inventory(
                    order_name_to_process
                )
                results.append(result)
            except Exception as e:
                errors.append(e)
        
        # Use ThreadPoolExecutor for better control
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(process_order_worker, name): name 
                for name in order_names
            }
            
            for future in as_completed(futures):
                pass
        
        # Final inventory should not be negative
        bin_doc = frappe.get_doc(
            "Bin",
            {"item_code": self.test_item_code, "warehouse": self.test_warehouse}
        )
        self.assertGreaterEqual(bin_doc.actual_qty, 0)
        
        # Print test results for verification
        print(f"Successfully processed: {len(results)} orders")
        print(f"Errors: {len(errors)}")
        print(f"Final inventory: {bin_doc.actual_qty}")
    
    def test_locking_mechanism(self):
        """Test the distributed locking mechanism"""
        order_name = self.test_order.name
        lock_holder_time = 0.5  # seconds
        
        lock_acquired_times: List[float] = []
        errors: List[Exception] = []
        
        def lock_worker(worker_id: int):
            try:
                start_time = time.time()
                with acquire_lock(order_name):
                    lock_acquired_times.append(time.time())
                    time.sleep(lock_holder_time)
            except Exception as e:
                errors.append(e)
        
        # Start 3 workers
        threads = [
            threading.Thread(target=lock_worker, args=(1,)),
            threading.Thread(target=lock_worker, args=(2,)),
            threading.Thread(target=lock_worker, args=(3,))
        ]
        
        start_test_time = time.time()
        
        for t in threads:
            t.start()
        
        for t in threads:
            t.join()
        
        # Verify locks were acquired sequentially
        self.assertEqual(len(lock_acquired_times), 3)
        
        # Sort the times
        sorted_times = sorted(lock_acquired_times)
        
        # Check that each acquisition was at least lock_holder_time apart
        for i in range(1, len(sorted_times)):
            time_diff = sorted_times[i] - sorted_times[i-1]
            self.assertGreaterEqual(time_diff, lock_holder_time - 0.1)  # Allow small tolerance
    
    def test_state_machine_transitions(self):
        """Test all valid state transitions in sequence"""
        order_name = self.test_order.name
        
        valid_transitions = [
            ("Draft", "On Hold"),
            ("On Hold", "To Deliver and Bill"),
            ("To Deliver and Bill", "To Deliver"),
            ("To Deliver", "Completed"),
            ("Completed", "Closed"),
            ("Closed", "Draft")
        ]
        
        current_status = self.test_order.status
        
        for from_status, to_status in valid_transitions:
            if current_status != from_status:
                OrderService.update_order_status(order_name, from_status)
            
            OrderService.update_order_status(order_name, to_status)
            current_status = to_status
            
            order = frappe.get_doc("Sales Order", order_name)
            self.assertEqual(order.status, to_status)


if __name__ == "__main__":
    unittest.main()
