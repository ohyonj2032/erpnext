# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import json
import time
from enum import Enum

import frappe
from frappe import _
from frappe.utils import now

from erpnext.models.order import get_last_result, record_status_change, snapshot_order
from erpnext.selling.doctype.sales_order.sales_order_extension import (
    can_transition,
    extend_sales_order_class,
    update_sales_order_docfield,
)


class OrderStatus(Enum):
    DRAFT = "Draft"
    ON_HOLD = "On Hold"
    TO_PAY = "To Pay"
    TO_DELIVER_AND_BILL = "To Deliver and Bill"
    TO_BILL = "To Bill"
    TO_DELIVER = "To Deliver"
    COMPLETED = "Completed"
    CANCELLED = "Cancelled"
    CLOSED = "Closed"


class OrderOperationError(frappe.ValidationError):
    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result or {}


class StatusTransitionError(OrderOperationError):
    pass


class ConcurrencyError(OrderOperationError):
    pass


class RollbackTrackedError(OrderOperationError):
    pass


class OrderService:
    LOCK_TIMEOUT = 15
    LOCK_RETRY_INTERVAL = 0.1
    LOCK_PREFIX = "order_lock_"
    IDEMPOTENCY_PREFIX = "order_idempotency_"
    METRIC_PREFIX = "order_metric_"
    _bootstrapped = False

    @classmethod
    def bootstrap(cls):
        if cls._bootstrapped:
            return
        try:
            update_sales_order_docfield()
        except Exception:
            frappe.clear_messages()
        extend_sales_order_class()
        cls._bootstrapped = True

    @classmethod
    def generate_idempotency_signature(
        cls,
        order_id,
        new_status,
        inventory_deductions=None,
        accounting_entries=None,
        extra_payload=None,
    ):
        payload = {
            "order_id": order_id,
            "new_status": new_status,
            "inventory_deductions": inventory_deductions or [],
            "accounting_entries": accounting_entries or [],
            "extra_payload": extra_payload or {},
        }
        serialized = json.dumps(payload, sort_keys=True, default=str)
        return frappe.generate_hash(serialized, 20)

    @classmethod
    def acquire_lock(cls, order_id, timeout=None):
        timeout = timeout or cls.LOCK_TIMEOUT
        lock_key = f"{cls.LOCK_PREFIX}{order_id}"
        lock_value = f"{frappe.session.user}:{time.time()}"
        attempts = max(1, int(timeout / cls.LOCK_RETRY_INTERVAL))

        for _ in range(attempts):
            if frappe.cache().set(lock_key, lock_value, expire=timeout, nx=True):
                cls._increment_metric("lock_acquisitions")
                return lock_key
            time.sleep(cls.LOCK_RETRY_INTERVAL)

        cls._increment_metric("lock_timeouts")
        result = {
            "success": False,
            "order_id": order_id,
            "error": _("Could not acquire lock for Sales Order {0}").format(order_id),
            "timestamp": now(),
            "metrics": cls.get_metric_snapshot(),
        }
        raise ConcurrencyError(result["error"], result=result)

    @classmethod
    def release_lock(cls, lock_key):
        if lock_key:
            frappe.cache().delete(lock_key)

    @classmethod
    def _increment_metric(cls, metric_name):
        key = f"{cls.METRIC_PREFIX}{metric_name}"
        cache = frappe.cache()
        current = cache.get(key) or 0
        cache.set(key, int(current) + 1, expire=24 * 60 * 60)

    @classmethod
    def get_metric_snapshot(cls):
        cache = frappe.cache()
        return {
            "lock_acquisitions": int(cache.get(f"{cls.METRIC_PREFIX}lock_acquisitions") or 0),
            "lock_timeouts": int(cache.get(f"{cls.METRIC_PREFIX}lock_timeouts") or 0),
            "idempotent_hits": int(cache.get(f"{cls.METRIC_PREFIX}idempotent_hits") or 0),
            "rollback_events": int(cache.get(f"{cls.METRIC_PREFIX}rollback_events") or 0),
        }

    @classmethod
    def _idempotency_cache_key(cls, order_id, signature):
        return f"{cls.IDEMPOTENCY_PREFIX}{order_id}:{signature}"

    @classmethod
    def check_idempotency(cls, order_id, signature, order=None):
        cached = frappe.cache().get(cls._idempotency_cache_key(order_id, signature))
        if cached:
            cls._increment_metric("idempotent_hits")
            result = dict(cached)
            result["is_idempotent"] = True
            return result

        order = order or frappe.get_doc("Sales Order", order_id)
        last_signature = order.get("signature")
        last_result = cls._extract_last_result(order)
        if last_signature == signature and last_result:
            cls._increment_metric("idempotent_hits")
            result = dict(last_result)
            result["is_idempotent"] = True
            frappe.cache().set(cls._idempotency_cache_key(order_id, signature), result, expire=3600)
            return result

        return None

    @classmethod
    def store_idempotent_result(cls, order_id, signature, result):
        frappe.cache().set(cls._idempotency_cache_key(order_id, signature), result, expire=3600)

    @classmethod
    def validate_status_transition(cls, current_status, new_status):
        return can_transition(current_status, new_status)

    @classmethod
    def _lock_order_row(cls, order_id):
        frappe.db.get_value("Sales Order", order_id, "name", for_update=True)
        return frappe.get_doc("Sales Order", order_id)

    @classmethod
    def _extract_last_result(cls, order):
        return get_last_result(order)

    @classmethod
    def _snapshot_order(cls, order):
        return snapshot_order(order)

    @classmethod
    def _prepare_inventory_deductions(cls, order, inventory_deductions):
        if inventory_deductions:
            return inventory_deductions

        deductions = []
        for item in order.items:
            if not item.get("warehouse"):
                continue
            if not frappe.db.get_value("Item", item.item_code, "is_stock_item"):
                continue
            deductions.append(
                {
                    "item_code": item.item_code,
                    "warehouse": item.warehouse,
                    "qty": float(item.get("stock_qty") or item.get("qty") or 0),
                }
            )
        return deductions

    @classmethod
    def _prepare_accounting_entries(cls, order, inventory_deductions, accounting_entries):
        if accounting_entries:
            return accounting_entries

        order_items = {item.item_code: item for item in order.items}
        entries = []
        for deduction in inventory_deductions:
            row = order_items.get(deduction["item_code"])
            qty = float(deduction.get("qty") or 0)
            rate = float((row and row.get("rate")) or 0)
            entries.append(
                {
                    "reference_doctype": "Sales Order",
                    "reference_name": order.name,
                    "item_code": deduction["item_code"],
                    "warehouse": deduction["warehouse"],
                    "qty": qty,
                    "ledger_amount": qty * rate,
                    "ledger_currency": order.get("currency"),
                }
            )
        return entries

    @classmethod
    def _apply_order_result(cls, order, previous_status, new_status, signature, result, rolled_back=False):
        if hasattr(order, "record_status_change"):
            order.record_status_change(
                previous_status=previous_status,
                new_status=new_status,
                signature=signature,
                result=result,
                rolled_back=rolled_back,
                metadata={"success": result.get("success", False)},
            )
        else:
            record_status_change(
                order,
                previous_status=previous_status,
                new_status=new_status,
                signature=signature,
                result=result,
                rolled_back=rolled_back,
                metadata={"success": result.get("success", False)},
            )

    @classmethod
    def _persist_rollback_result(cls, order_id, previous_status, new_status, signature, result):
        try:
            order = frappe.get_doc("Sales Order", order_id)
            cls._apply_order_result(order, previous_status, new_status, signature, result, rolled_back=True)
            order.save(ignore_permissions=True)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"Failed to persist rollback result for {order_id}")

    @classmethod
    def _build_success_result(
        cls,
        order,
        signature,
        previous_status,
        new_status,
        inventory_result,
        accounting_entries,
    ):
        return {
            "success": True,
            "status": "success",
            "order_id": order.name,
            "previous_status": previous_status,
            "new_status": new_status,
            "signature": signature,
            "version": order.get("version"),
            "inventory": inventory_result,
            "accounting_entries": accounting_entries,
            "rolled_back": False,
            "timestamp": now(),
            "metrics": cls.get_metric_snapshot(),
        }

    @classmethod
    def _build_failure_result(
        cls,
        order_id,
        signature,
        previous_status,
        attempted_status,
        error,
        rollback_state,
        inventory_deductions,
        accounting_entries,
    ):
        return {
            "success": False,
            "status": "failed",
            "order_id": order_id,
            "previous_status": previous_status,
            "attempted_status": attempted_status,
            "signature": signature,
            "error": str(error),
            "rolled_back": True,
            "rollback_state": rollback_state,
            "inventory_deductions": inventory_deductions,
            "accounting_entries": accounting_entries,
            "timestamp": now(),
            "metrics": cls.get_metric_snapshot(),
        }

    @classmethod
    def process_order_status_change(
        cls,
        order_id,
        new_status,
        idempotency_key=None,
        inventory_deductions=None,
        accounting_entries=None,
        simulate_failure=False,
        **kwargs,
    ):
        cls.bootstrap()
        signature = idempotency_key or cls.generate_idempotency_signature(
            order_id=order_id,
            new_status=new_status,
            inventory_deductions=inventory_deductions,
            accounting_entries=accounting_entries,
            extra_payload=kwargs,
        )

        cached = cls.check_idempotency(order_id, signature)
        if cached:
            return cached

        lock_key = None
        try:
            lock_key = cls.acquire_lock(order_id)
            order = cls._lock_order_row(order_id)

            cached = cls.check_idempotency(order_id, signature, order=order)
            if cached:
                return cached

            if order.status == new_status:
                current_result = cls._extract_last_result(order) or {
                    "success": True,
                    "status": "success",
                    "order_id": order_id,
                    "previous_status": order.status,
                    "new_status": new_status,
                    "signature": signature,
                    "timestamp": now(),
                }
                current_result["is_idempotent"] = True
                cls.store_idempotent_result(order_id, signature, current_result)
                return current_result

            if not cls.validate_status_transition(order.status, new_status):
                failure = cls._build_failure_result(
                    order_id=order_id,
                    signature=signature,
                    previous_status=order.status,
                    attempted_status=new_status,
                    error=_("Cannot transition from {0} to {1}").format(order.status, new_status),
                    rollback_state=cls._snapshot_order(order),
                    inventory_deductions=inventory_deductions or [],
                    accounting_entries=accounting_entries or [],
                )
                cls.store_idempotent_result(order_id, signature, failure)
                cls._persist_rollback_result(order_id, order.status, new_status, signature, failure)
                raise StatusTransitionError(failure["error"], result=failure)

            deductions = cls._prepare_inventory_deductions(order, inventory_deductions)
            entries = cls._prepare_accounting_entries(order, deductions, accounting_entries)
            previous_status = order.status
            rollback_state = cls._snapshot_order(order)
            inventory_result = {"status": "skipped", "deductions": [], "accounting_entries": []}

            try:
                frappe.db.begin()
                if deductions:
                    from erpnext.stock.services.inventory_service import InventoryService

                    inventory_result = InventoryService.apply_inventory_and_accounting(
                        reference_doctype="Sales Order",
                        reference_name=order.name,
                        deductions=deductions,
                        accounting_entries=entries,
                        transaction_id=signature,
                        manage_transaction=False,
                        simulate_failure=simulate_failure,
                    )

                order.status = new_status
                cls._apply_order_result(order, previous_status, new_status, signature, {})
                order.save(ignore_permissions=True)

                success_result = cls._build_success_result(
                    order=order,
                    signature=signature,
                    previous_status=previous_status,
                    new_status=new_status,
                    inventory_result=inventory_result,
                    accounting_entries=entries,
                )
                cls._apply_order_result(order, previous_status, new_status, signature, success_result)
                order.save(ignore_permissions=True)
                frappe.db.commit()
            except Exception as exc:
                frappe.db.rollback()
                cls._increment_metric("rollback_events")
                failure = cls._build_failure_result(
                    order_id=order_id,
                    signature=signature,
                    previous_status=previous_status,
                    attempted_status=new_status,
                    error=exc,
                    rollback_state=rollback_state,
                    inventory_deductions=deductions,
                    accounting_entries=entries,
                )
                cls.store_idempotent_result(order_id, signature, failure)
                cls._persist_rollback_result(order_id, previous_status, new_status, signature, failure)
                raise RollbackTrackedError(str(exc), result=failure)

            cls.store_idempotent_result(order_id, signature, success_result)
            return success_result
        finally:
            cls.release_lock(lock_key)

    @classmethod
    def confirm_order(cls, order_id, idempotency_key=None):
        return cls.process_order_status_change(
            order_id=order_id,
            new_status=OrderStatus.TO_DELIVER_AND_BILL.value,
            idempotency_key=idempotency_key,
        )

    @classmethod
    def complete_order(cls, order_id, idempotency_key=None):
        return cls.process_order_status_change(
            order_id=order_id,
            new_status=OrderStatus.COMPLETED.value,
            idempotency_key=idempotency_key,
        )

    @classmethod
    def cancel_order(cls, order_id, idempotency_key=None):
        return cls.process_order_status_change(
            order_id=order_id,
            new_status=OrderStatus.CANCELLED.value,
            idempotency_key=idempotency_key,
        )


def get_order_service():
    return OrderService()
