# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import hashlib
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, Optional

import frappe
from frappe import _
from frappe.model.document import Document


class OrderLockError(Exception):
    pass


class OrderStateError(Exception):
    pass


class IdempotentResult:
    def __init__(self, is_retry: bool, result: Dict, timestamp: datetime):
        self.is_retry = is_retry
        self.result = result
        self.timestamp = timestamp


@contextmanager
def acquire_lock(order_name: str, timeout: int = 30):
    """
    Acquire an exclusive lock for an order to prevent concurrent modifications.
    Uses database locking mechanism.
    """
    lock_key = f"order_lock:{order_name}"
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        # Try to acquire lock using database
        try:
            frappe.db.sql(
                """INSERT INTO `tabOrder Lock` (name, lock_time, locked_by) 
                   VALUES (%s, NOW(), %s)
                   ON DUPLICATE KEY UPDATE name=name""",
                (lock_key, frappe.session.user)
            )
            frappe.db.commit()
            
            yield lock_key
            
            # Release lock
            frappe.db.sql("DELETE FROM `tabOrder Lock` WHERE name = %s", (lock_key,))
            frappe.db.commit()
            return
            
        except Exception:
            # Lock is held by someone else
            time.sleep(0.1)
    
    raise OrderLockError(f"Could not acquire lock for order {order_name} within {timeout} seconds")


def generate_idempotency_key(order_name: str, operation: str, timestamp: float = None) -> str:
    """
    Generate an idempotency key based on order name, operation, and timestamp.
    """
    if timestamp is None:
        timestamp = time.time()
    
    key_data = f"{order_name}:{operation}:{timestamp}"
    return hashlib.sha256(key_data.encode()).hexdigest()


def check_idempotency(order_name: str, idempotency_key: str) -> Optional[IdempotentResult]:
    """
    Check if this operation has already been executed successfully with the same key.
    Returns the previous result if found, otherwise None.
    """
    record = frappe.db.get_value(
        "Order Idempotency Record",
        {
            "order": order_name,
            "idempotency_key": idempotency_key
        },
        ["result", "timestamp"]
    )
    
    if record:
        result, timestamp = record
        return IdempotencyResult(
            is_retry=True,
            result=frappe.parse_json(result) if result else {},
            timestamp=timestamp
        )
    
    return None


def save_idempotency_result(
    order_name: str, 
    idempotency_key: str, 
    result: Dict
) -> None:
    """
    Save the result of an idempotent operation.
    """
    existing = frappe.db.exists("Order Idempotency Record", {
        "order": order_name,
        "idempotency_key": idempotency_key
    })
    
    if existing:
        record = frappe.get_doc("Order Idempotency Record", existing)
    else:
        record = frappe.new_doc("Order Idempotency Record")
        record.order = order_name
        record.idempotency_key = idempotency_key
    
    record.result = frappe.as_json(result)
    record.timestamp = frappe.utils.now_datetime()
    record.save(ignore_permissions=True)


# State machine for order status
ORDER_STATUS_TRANSITIONS = {
    "Draft": ["On Hold", "To Deliver and Bill", "To Bill", "To Deliver"],
    "On Hold": ["Draft", "To Deliver and Bill", "To Bill", "To Deliver"],
    "To Deliver and Bill": ["To Deliver", "To Bill", "Completed", "Closed"],
    "To Bill": ["Completed", "Closed"],
    "To Deliver": ["Completed", "Closed"],
    "Completed": ["Closed"],
    "Closed": ["Draft"],
    "Cancelled": []
}


def validate_status_transition(current_status: str, new_status: str) -> bool:
    """
    Validate if a status transition is allowed by the state machine.
    """
    if new_status not in ORDER_STATUS_TRANSITIONS.get(current_status, []):
        raise OrderStateError(
            _("Invalid status transition from {0} to {1}").format(
                current_status, new_status
            )
        )
    return True


class OrderService:
    """
    Service layer for order operations with concurrency control, idempotency, and state machine validation.
    """
    
    @staticmethod
    def update_order_status(
        order_name: str, 
        new_status: str, 
        idempotency_key: str = None,
        **kwargs
    ) -> Dict:
        """
        Update order status with concurrency control and state machine validation.
        """
        if idempotency_key is None:
            idempotency_key = generate_idempotency_key(order_name, "update_status")
        
        # Check idempotency
        idempotent_result = check_idempotency(order_name, idempotency_key)
        if idempotent_result:
            return idempotent_result.result
        
        result = {}
        
        try:
            with acquire_lock(order_name):
                order = frappe.get_doc("Sales Order", order_name)
                
                # Validate status transition
                validate_status_transition(order.status, new_status)
                
                # Perform the update
                order.status = new_status
                
                # Update other fields if provided
                for key, value in kwargs.items():
                    if hasattr(order, key):
                        setattr(order, key, value)
                
                order.save(ignore_permissions=True)
                order.reload()
                
                result = {
                    "success": True,
                    "order_name": order.name,
                    "new_status": order.status,
                    "modified": order.modified
                }
                
                # Save idempotent result
                save_idempotency_result(order_name, idempotency_key, result)
                
                return result
                
        except OrderLockError as e:
            frappe.throw(_("Order is locked: {0}").format(str(e)))
        except OrderStateError as e:
            frappe.throw(str(e))
    
    @staticmethod
    def process_order_with_inventory(
        order_name: str,
        idempotency_key: str = None,
    ) -> Dict:
        """
        Process an order with inventory operations in a single transaction.
        Ensures atomicity of order status update and inventory deduction.
        """
        if idempotency_key is None:
            idempotency_key = generate_idempotency_key(order_name, "process_with_inventory")
        
        # Check idempotency
        idempotent_result = check_idempotency(order_name, idempotency_key)
        if idempotent_result:
            return idempotent_result.result
        
        result = {}
        
        try:
            with acquire_lock(order_name):
                # Use database transaction for atomicity
                frappe.db.begin()
                
                try:
                    order = frappe.get_doc("Sales Order", order_name)
                    
                    # Validate state transition
                    if order.status not in ["To Deliver and Bill", "To Deliver"]:
                        raise OrderStateError(
                            _("Order must be in a deliverable status to process inventory")
                        )
                    
                    # Process inventory deduction
                    inventory_results = OrderService._deduct_inventory(order)
                    
                    # Update order status
                    order.status = "Completed"
                    order.save(ignore_permissions=True)
                    
                    # Record state change
                    OrderService._record_state_change(order, "To Deliver and Bill", "Completed")
                    
                    frappe.db.commit()
                    
                    result = {
                        "success": True,
                        "order_name": order.name,
                        "status": order.status,
                        "inventory_results": inventory_results,
                        "modified": order.modified
                    }
                    
                    # Save idempotent result
                    save_idempotency_result(order_name, idempotency_key, result)
                    
                    return result
                    
                except Exception as e:
                    frappe.db.rollback()
                    raise e
                    
        except Exception as e:
            frappe.throw(_("Error processing order: {0}").format(str(e)))
    
    @staticmethod
    def _deduct_inventory(order: Document) -> Dict:
        """
        Deduct inventory for order items with optimistic locking.
        """
        results = {}
        
        for item in order.items:
            if frappe.get_cached_value("Item", item.item_code, "is_stock_item") == 1:
                # Get bin with optimistic locking
                bin_doc = frappe.get_doc(
                    "Bin",
                    {"item_code": item.item_code, "warehouse": item.warehouse}
                )
                
                # Check current version
                current_version = bin_doc.get("version", 0)
                
                # Validate sufficient stock
                if bin_doc.actual_qty < item.qty:
                    raise ValueError(
                        _("Insufficient stock for item {0} in warehouse {1}").format(
                            item.item_code, item.warehouse
                        )
                    )
                
                # Calculate new quantity
                new_actual_qty = bin_doc.actual_qty - item.qty
                new_projected_qty = bin_doc.projected_qty - item.qty
                
                # Update bin with version check (optimistic locking)
                update_count = frappe.db.sql("""
                    UPDATE `tabBin`
                    SET actual_qty = %s,
                        projected_qty = %s,
                        version = version + 1
                    WHERE name = %s AND version = %s
                """, (new_actual_qty, new_projected_qty, bin_doc.name, current_version))
                
                if update_count == 0:
                    # Optimistic lock failed
                    raise ValueError(
                        _("Inventory for item {0} was modified by another transaction").format(
                            item.item_code
                        )
                    )
                
                results[item.item_code] = {
                    "warehouse": item.warehouse,
                    "qty_deducted": item.qty,
                    "old_actual_qty": bin_doc.actual_qty,
                    "new_actual_qty": new_actual_qty
                }
        
        return results
    
    @staticmethod
    def _record_state_change(
        order: Document, 
        old_status: str, 
        new_status: str
    ) -> None:
        """
        Record a state change for the order with timestamp and user information.
        """
        state_change = frappe.new_doc("Order State Change")
        state_change.order = order.name
        state_change.old_status = old_status
        state_change.new_status = new_status
        state_change.changed_by = frappe.session.user
        state_change.timestamp = frappe.utils.now_datetime()
        state_change.save(ignore_permissions=True)
    
    @staticmethod
    def get_order_state_history(order_name: str) -> list:
        """
        Get the complete state change history of an order.
        """
        changes = frappe.get_all(
            "Order State Change",
            filters={"order": order_name},
            fields=["old_status", "new_status", "changed_by", "timestamp"],
            order_by="timestamp desc"
        )
        return changes


def setup_order_tables():
    """
    Create necessary database tables if they don't exist.
    Note: In a real ERPNext setup, these would be defined as DocTypes
    """
    # Create Order Lock table if not exists
    if not frappe.db.exists("DocType", "Order Lock"):
        frappe.db.sql("""
            CREATE TABLE IF NOT EXISTS `tabOrder Lock` (
                `name` VARCHAR(140) NOT NULL,
                `lock_time` DATETIME,
                `locked_by` VARCHAR(140),
                `modified` DATETIME,
                PRIMARY KEY (`name`)
            ) ENGINE=InnoDB
        """)
    
    # Create Order Idempotency Record table if not exists
    if not frappe.db.exists("DocType", "Order Idempotency Record"):
        frappe.db.sql("""
            CREATE TABLE IF NOT EXISTS `tabOrder Idempotency Record` (
                `name` VARCHAR(140) NOT NULL,
                `order` VARCHAR(140),
                `idempotency_key` VARCHAR(255),
                `result` TEXT,
                `timestamp` DATETIME,
                `modified` DATETIME,
                PRIMARY KEY (`name`),
                UNIQUE KEY `idx_order_key` (`order`, `idempotency_key`)
            ) ENGINE=InnoDB
        """)
    
    # Create Order State Change table if not exists
    if not frappe.db.exists("DocType", "Order State Change"):
        frappe.db.sql("""
            CREATE TABLE IF NOT EXISTS `tabOrder State Change` (
                `name` VARCHAR(140) NOT NULL,
                `order` VARCHAR(140),
                `old_status` VARCHAR(140),
                `new_status` VARCHAR(140),
                `changed_by` VARCHAR(140),
                `timestamp` DATETIME,
                `modified` DATETIME,
                PRIMARY KEY (`name`),
                KEY `idx_order` (`order`)
            ) ENGINE=InnoDB
        """)
    
    # Add version column to Bin if not exists
    if not frappe.db.has_column("Bin", "version"):
        frappe.db.sql("ALTER TABLE `tabBin` ADD COLUMN `version` INT DEFAULT 0")
