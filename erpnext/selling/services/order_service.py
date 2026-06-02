# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""
Order Service - Concurrency-safe order processing with idempotency
This service handles concurrent modifications to orders and inventory
"""

import frappe
import hashlib
import time
from frappe import _
from frappe.utils import now, get_datetime
from frappe.model.document import Document
from enum import Enum


class OrderStatus(Enum):
    """Order status machine enum"""
    DRAFT = "Draft"
    TO_CONFIRM = "To Confirm"
    CONFIRMED = "Confirmed"
    TO_DELIVER = "To Deliver"
    DELIVERED = "Delivered"
    TO_BILL = "To Bill"
    COMPLETED = "Completed"
    CANCELLED = "Cancelled"
    CLOSED = "Closed"


class StatusTransitionError(frappe.ValidationError):
    pass


class ConcurrencyError(frappe.ValidationError):
    pass


class OrderService:
    """
    Service layer for order processing with concurrency control and idempotency
    """

    LOCK_TIMEOUT = 30  # seconds
    LOCK_RETRY_INTERVAL = 0.5
    LOCK_PREFIX = "order_lock_"

    # Status transition rules
    STATUS_TRANSITIONS = {
        OrderStatus.DRAFT: [OrderStatus.TO_CONFIRM, OrderStatus.CANCELLED],
        OrderStatus.TO_CONFIRM: [OrderStatus.CONFIRMED, OrderStatus.DRAFT, OrderStatus.CANCELLED],
        OrderStatus.CONFIRMED: [OrderStatus.TO_DELIVER, OrderStatus.TO_BILL, OrderStatus.CANCELLED],
        OrderStatus.TO_DELIVER: [OrderStatus.DELIVERED, OrderStatus.CONFIRMED, OrderStatus.CANCELLED],
        OrderStatus.DELIVERED: [OrderStatus.TO_BILL, OrderStatus.COMPLETED, OrderStatus.CANCELLED],
        OrderStatus.TO_BILL: [OrderStatus.COMPLETED, OrderStatus.CANCELLED],
        OrderStatus.COMPLETED: [OrderStatus.CLOSED],
        OrderStatus.CANCELLED: [],
        OrderStatus.CLOSED: [],
    }

    @classmethod
    def generate_idempotency_key(cls, order_id: str, operation: str) -> str:
        """
        Generate a unique idempotency key for an operation
        """
        key_data = f"{order_id}_{operation}_{int(time.time() // 60)}"
        return hashlib.sha256(key_data.encode()).hexdigest()

    @classmethod
    def acquire_lock(cls, order_id: str, timeout: int = None) -> str:
        """
        Acquire an exclusive lock on an order
        """
        timeout = timeout or cls.LOCK_TIMEOUT
        lock_key = f"{cls.LOCK_PREFIX}{order_id}"
        lock_value = f"{frappe.session.user}_{time.time()}"
        
        max_attempts = int(timeout / cls.LOCK_RETRY_INTERVAL)
        
        for _ in range(max_attempts):
            if frappe.cache().set(lock_key, lock_value, expire=timeout, nx=True):
                return lock_key
            
            time.sleep(cls.LOCK_RETRY_INTERVAL)
        
        raise ConcurrencyError(
            _("Could not acquire lock on order {0}. Another process might be holding it.")
            .format(order_id)
        )

    @classmethod
    def release_lock(cls, lock_key: str):
        """
        Release a previously acquired lock
        """
        frappe.cache().delete(lock_key)

    @classmethod
    def check_idempotency(cls, order_id: str, idempotency_key: str) -> dict | None:
        """
        Check if operation has already been processed (idempotency)
        Returns last result if found, None otherwise
        """
        key = f"order_idempotent_{order_id}_{idempotency_key}"
        return frappe.cache().get(key)

    @classmethod
    def store_idempotent_result(cls, order_id: str, idempotency_key: str, result: dict):
        """
        Store the result of an idempotent operation
        """
        key = f"order_idempotent_{order_id}_{idempotency_key}"
        frappe.cache().set(key, result, expire=3600)  # Store for 1 hour

    @classmethod
    def validate_status_transition(cls, current_status: str, new_status: str) -> bool:
        """
        Validate if status transition is allowed
        """
        try:
            current = OrderStatus(current_status)
            new = OrderStatus(new_status)
            allowed = cls.STATUS_TRANSITIONS.get(current, [])
            return new in allowed
        except ValueError:
            return False

    @classmethod
    def process_order_status_change(
        cls,
        order_id: str,
        new_status: str,
        idempotency_key: str = None,
        **kwargs
    ) -> dict:
        """
        Process order status change with concurrency control and idempotency
        
        Args:
            order_id: Sales Order ID
            new_status: Target status
            idempotency_key: Optional idempotency key for duplicate protection
            **kwargs: Additional parameters
        
        Returns:
            Dict with result info
        """
        if not idempotency_key:
            idempotency_key = cls.generate_idempotency_key(order_id, f"status_{new_status}")
        
        # Check idempotency first
        cached_result = cls.check_idempotency(order_id, idempotency_key)
        if cached_result:
            return cached_result
        
        lock_key = None
        result = {
            "success": False,
            "order_id": order_id,
            "timestamp": now(),
            "is_idempotent": False
        }
        
        try:
            # Acquire lock
            lock_key = cls.acquire_lock(order_id)
            
            # Reload order with for_update to get latest state
            order = frappe.get_doc("Sales Order", order_id, for_update=True)
            
            # Validate status transition
            if not cls.validate_status_transition(order.status, new_status):
                raise StatusTransitionError(
                    _("Cannot transition from {0} to {1}")
                    .format(order.status, new_status)
                )
            
            # Process in transaction
            try:
                frappe.db.begin()
                
                # Update order status
                order.status = new_status
                order.last_status = order.status
                order.status_changed_at = now()
                order.save(ignore_permissions=True)
                
                # Handle inventory if needed
                if new_status in [OrderStatus.CONFIRMED.value, OrderStatus.TO_DELIVER.value]:
                    cls._process_inventory_reservation(order)
                
                frappe.db.commit()
                
                result["success"] = True
                result["new_status"] = new_status
                result["last_modified"] = order.modified
                
            except Exception as e:
                frappe.db.rollback()
                raise e
            
        except Exception as e:
            result["error"] = str(e)
            raise
        finally:
            if lock_key:
                cls.release_lock(lock_key)
            
            if result.get("success"):
                cls.store_idempotent_result(order_id, idempotency_key, result)
        
        return result

    @classmethod
    def _process_inventory_reservation(cls, order):
        """
        Process inventory reservation for order items
        Uses optimistic locking on Bin records
        """
        from erpnext.stock.doctype.bin.bin import update_bin_qty
        from erpnext.stock.stock_balance import get_reserved_qty
        
        for item in order.items:
            if not item.warehouse or not frappe.db.get_value("Item", item.item_code, "is_stock_item"):
                continue
            
            # Get bin with for_update for optimistic locking
            bin_doc = frappe.db.get_value(
                "Bin",
                {"item_code": item.item_code, "warehouse": item.warehouse},
                ["name", "version"],
                as_dict=True,
                for_update=True
            )
            
            if not bin_doc:
                continue
            
            # Update reserved quantity with optimistic lock
            new_version = bin_doc.version + 1
            
            # Update only if version matches
            updated = frappe.db.sql("""
                UPDATE `tabBin`
                SET reserved_qty = reserved_qty + %s,
                    version = %s,
                    modified = NOW()
                WHERE name = %s AND version = %s
            """, (item.stock_qty, new_version, bin_doc.name, bin_doc.version))
            
            if not updated:
                raise ConcurrencyError(
                    _("Inventory for item {0} in warehouse {1} was modified by another process")
                    .format(item.item_code, item.warehouse)
                )

    @classmethod
    def confirm_order(cls, order_id: str, idempotency_key: str = None) -> dict:
        """
        Confirm an order with full processing
        """
        return cls.process_order_status_change(
            order_id,
            OrderStatus.CONFIRMED.value,
            idempotency_key
        )

    @classmethod
    def complete_order(cls, order_id: str, idempotency_key: str = None) -> dict:
        """
        Mark an order as completed
        """
        return cls.process_order_status_change(
            order_id,
            OrderStatus.COMPLETED.value,
            idempotency_key
        )

    @classmethod
    def cancel_order(cls, order_id: str, idempotency_key: str = None) -> dict:
        """
        Cancel an order and release inventory
        """
        if not idempotency_key:
            idempotency_key = cls.generate_idempotency_key(order_id, "cancel")
        
        # Check idempotency
        cached_result = cls.check_idempotency(order_id, idempotency_key)
        if cached_result:
            return cached_result
        
        lock_key = None
        result = {
            "success": False,
            "order_id": order_id,
            "timestamp": now(),
            "is_idempotent": False
        }
        
        try:
            lock_key = cls.acquire_lock(order_id)
            
            order = frappe.get_doc("Sales Order", order_id, for_update=True)
            
            frappe.db.begin()
            
            # Release inventory reservations
            cls._release_inventory_reservations(order)
            
            # Update status
            order.status = OrderStatus.CANCELLED.value
            order.last_status = order.status
            order.status_changed_at = now()
            order.save(ignore_permissions=True)
            
            frappe.db.commit()
            
            result["success"] = True
            result["new_status"] = OrderStatus.CANCELLED.value
            
        except Exception as e:
            frappe.db.rollback()
            result["error"] = str(e)
            raise
        finally:
            if lock_key:
                cls.release_lock(lock_key)
            
            if result.get("success"):
                cls.store_idempotent_result(order_id, idempotency_key, result)
        
        return result

    @classmethod
    def _release_inventory_reservations(cls, order):
        """
        Release inventory reservations on cancellation
        """
        for item in order.items:
            if not item.warehouse:
                continue
            
            bin_doc = frappe.db.get_value(
                "Bin",
                {"item_code": item.item_code, "warehouse": item.warehouse},
                ["name", "version"],
                as_dict=True,
                for_update=True
            )
            
            if bin_doc:
                new_version = bin_doc.version + 1
                updated = frappe.db.sql("""
                    UPDATE `tabBin`
                    SET reserved_qty = GREATEST(reserved_qty - %s, 0),
                        version = %s,
                        modified = NOW()
                    WHERE name = %s AND version = %s
                """, (item.stock_qty, new_version, bin_doc.name, bin_doc.version))
                
                if not updated:
                    raise ConcurrencyError(
                        _("Inventory update conflict while cancelling order")
                    )


def get_order_service() -> OrderService:
    """
    Factory function to get Order Service instance
    """
    return OrderService()
