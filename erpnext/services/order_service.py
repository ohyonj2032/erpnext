import hashlib
import json
import time
import uuid
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import now_datetime

from erpnext.models.order import (
    InvalidStateTransition,
    OrderStatus,
    check_idempotency,
    compute_signature,
    record_state_change,
    set_last_result,
    validate_transition,
)
from erpnext.models.inventory import (
    ConcurrentModificationError,
    InsufficientStockError,
    atomic_deduct_with_gl,
    atomic_reserve_stock,
)


class LockAcquisitionError(frappe.ValidationError):
    pass


class IdempotentResult:
    def __init__(self, is_duplicate, result=None):
        self.is_duplicate = is_duplicate
        self.result = result

    def to_dict(self):
        return {"is_duplicate": self.is_duplicate, "result": self.result}


LOCK_PREFIX = "order_lock:"
LOCK_DEFAULT_TIMEOUT = 30
LOCK_RETRY_COUNT = 3
LOCK_RETRY_DELAY = 0.5


def acquire_lock(order_name, timeout=LOCK_DEFAULT_TIMEOUT):
    lock_key = f"{LOCK_PREFIX}{order_name}"
    lock_value = str(uuid.uuid4())
    acquired = frappe.cache().set_value(lock_key, lock_value, expires_in_sec=timeout)
    if not acquired:
        existing = frappe.cache().get_value(lock_key)
        if existing is not None:
            return None, None
        acquired = frappe.cache().set_value(lock_key, value=lock_value, expires_in_sec=timeout)
        if not acquired:
            return None, None
    return lock_key, lock_value


def release_lock(lock_key, lock_value):
    existing = frappe.cache().get_value(lock_key)
    if existing and str(existing) == str(lock_value):
        frappe.cache().delete_key(lock_key)
        return True
    return False


@contextmanager
def order_lock(order_name, timeout=LOCK_DEFAULT_TIMEOUT):
    lock_key = None
    lock_value = None
    acquired = False

    for attempt in range(LOCK_RETRY_COUNT):
        lock_key, lock_value = acquire_lock(order_name, timeout)
        if lock_key and lock_value:
            acquired = True
            break
        time.sleep(LOCK_RETRY_DELAY)

    if not acquired:
        frappe.throw(
            _("Could not acquire lock for Sales Order {0} after {1} attempts").format(
                order_name, LOCK_RETRY_COUNT
            ),
            LockAcquisitionError,
        )

    try:
        yield lock_key, lock_value
    finally:
        if acquired and lock_key and lock_value:
            release_lock(lock_key, lock_value)


def submit_order_with_lock(order_name, signature=None):
    is_dup, cached_result = check_idempotency(order_name, "submit")
    if is_dup:
        return IdempotentResult(is_duplicate=True, result=cached_result)

    with order_lock(order_name):
        is_dup, cached_result = check_idempotency(order_name, "submit")
        if is_dup:
            return IdempotentResult(is_duplicate=True, result=cached_result)

        doc = frappe.get_doc("Sales Order", order_name)
        current_status = doc.status

        validate_transition(current_status, OrderStatus.DRAFT)

        try:
            doc.submit()
            new_status = frappe.db.get_value("Sales Order", order_name, "status")

            result = {
                "order_name": order_name,
                "action": "submit",
                "from_status": current_status,
                "to_status": new_status,
                "timestamp": str(now_datetime()),
            }

            set_last_result(order_name, "submit", result)
            record_state_change(order_name, current_status, new_status, "submit", result)

            return IdempotentResult(is_duplicate=False, result=result)

        except Exception as e:
            frappe.db.rollback()
            result = {
                "order_name": order_name,
                "action": "submit",
                "from_status": current_status,
                "to_status": current_status,
                "error": str(e),
                "timestamp": str(now_datetime()),
            }
            set_last_result(order_name, "submit", result)
            record_state_change(order_name, current_status, current_status, "submit_failed", result)
            raise


def cancel_order_with_lock(order_name, signature=None):
    is_dup, cached_result = check_idempotency(order_name, "cancel")
    if is_dup:
        return IdempotentResult(is_duplicate=True, result=cached_result)

    with order_lock(order_name):
        is_dup, cached_result = check_idempotency(order_name, "cancel")
        if is_dup:
            return IdempotentResult(is_duplicate=True, result=cached_result)

        doc = frappe.get_doc("Sales Order", order_name)
        current_status = doc.status

        validate_transition(current_status, OrderStatus.CANCELLED)

        try:
            doc.cancel()
            new_status = frappe.db.get_value("Sales Order", order_name, "status")

            result = {
                "order_name": order_name,
                "action": "cancel",
                "from_status": current_status,
                "to_status": new_status,
                "timestamp": str(now_datetime()),
            }

            set_last_result(order_name, "cancel", result)
            record_state_change(order_name, current_status, new_status, "cancel", result)

            return IdempotentResult(is_duplicate=False, result=result)

        except Exception as e:
            frappe.db.rollback()
            result = {
                "order_name": order_name,
                "action": "cancel",
                "from_status": current_status,
                "to_status": current_status,
                "error": str(e),
                "timestamp": str(now_datetime()),
            }
            set_last_result(order_name, "cancel", result)
            record_state_change(order_name, current_status, current_status, "cancel_failed", result)
            raise


def update_order_status_with_lock(order_name, target_status, signature=None):
    action = f"update_status_{target_status}"
    is_dup, cached_result = check_idempotency(order_name, action)
    if is_dup:
        return IdempotentResult(is_duplicate=True, result=cached_result)

    with order_lock(order_name):
        is_dup, cached_result = check_idempotency(order_name, action)
        if is_dup:
            return IdempotentResult(is_duplicate=True, result=cached_result)

        doc = frappe.get_doc("Sales Order", order_name)
        current_status = doc.status

        if current_status == target_status:
            result = {
                "order_name": order_name,
                "action": action,
                "from_status": current_status,
                "to_status": target_status,
                "skipped": True,
                "timestamp": str(now_datetime()),
            }
            set_last_result(order_name, action, result)
            return IdempotentResult(is_duplicate=False, result=result)

        validate_transition(current_status, target_status)

        try:
            doc.update_status(target_status)
            new_status = frappe.db.get_value("Sales Order", order_name, "status")

            result = {
                "order_name": order_name,
                "action": action,
                "from_status": current_status,
                "to_status": new_status,
                "timestamp": str(now_datetime()),
            }

            set_last_result(order_name, action, result)
            record_state_change(order_name, current_status, new_status, action, result)

            return IdempotentResult(is_duplicate=False, result=result)

        except Exception as e:
            frappe.db.rollback()
            result = {
                "order_name": order_name,
                "action": action,
                "from_status": current_status,
                "to_status": current_status,
                "error": str(e),
                "timestamp": str(now_datetime()),
            }
            set_last_result(order_name, action, result)
            record_state_change(order_name, current_status, current_status, f"{action}_failed", result)
            raise


def submit_order_with_reservation(order_name):
    action = "submit_with_reservation"
    is_dup, cached_result = check_idempotency(order_name, action)
    if is_dup:
        return IdempotentResult(is_duplicate=True, result=cached_result)

    with order_lock(order_name):
        is_dup, cached_result = check_idempotency(order_name, action)
        if is_dup:
            return IdempotentResult(is_duplicate=True, result=cached_result)

        doc = frappe.get_doc("Sales Order", order_name)
        current_status = doc.status

        validate_transition(current_status, OrderStatus.DRAFT)

        try:
            with frappe.db.transaction():
                doc.submit()

                reservation_results = []
                for item in doc.items:
                    if item.warehouse and item.qty:
                        try:
                            res = atomic_reserve_stock(
                                item.item_code, item.warehouse, item.qty
                            )
                            reservation_results.append(res)
                        except InsufficientStockError:
                            frappe.db.rollback()
                            raise

                new_status = frappe.db.get_value("Sales Order", order_name, "status")

                result = {
                    "order_name": order_name,
                    "action": action,
                    "from_status": current_status,
                    "to_status": new_status,
                    "reservations": reservation_results,
                    "timestamp": str(now_datetime()),
                }

                set_last_result(order_name, action, result)
                record_state_change(order_name, current_status, new_status, action, result)

                return IdempotentResult(is_duplicate=False, result=result)

        except Exception:
            frappe.db.rollback()
            result = {
                "order_name": order_name,
                "action": action,
                "from_status": current_status,
                "to_status": current_status,
                "error": "Reservation or submit failed, rolled back",
                "timestamp": str(now_datetime()),
            }
            set_last_result(order_name, action, result)
            record_state_change(
                order_name, current_status, current_status, f"{action}_failed", result
            )
            raise


def deduct_stock_for_delivery(order_name, item_code, warehouse, qty, company, cost_center=None):
    action = f"deduct_{item_code}_{warehouse}_{qty}"
    is_dup, cached_result = check_idempotency(order_name, action)
    if is_dup:
        return IdempotentResult(is_duplicate=True, result=cached_result)

    with order_lock(order_name):
        is_dup, cached_result = check_idempotency(order_name, action)
        if is_dup:
            return IdempotentResult(is_duplicate=True, result=cached_result)

        try:
            with frappe.db.transaction():
                result_data = atomic_deduct_with_gl(
                    item_code=item_code,
                    warehouse=warehouse,
                    qty=qty,
                    company=company,
                    voucher_type="Sales Order",
                    voucher_no=order_name,
                    cost_center=cost_center,
                )

                result = {
                    "order_name": order_name,
                    "action": action,
                    "deduction": result_data,
                    "timestamp": str(now_datetime()),
                }

                set_last_result(order_name, action, result)

                return IdempotentResult(is_duplicate=False, result=result)

        except (InsufficientStockError, ConcurrentModificationError):
            frappe.db.rollback()
            raise
        except Exception:
            frappe.db.rollback()
            raise


def get_order_lock_status(order_name):
    lock_key = f"{LOCK_PREFIX}{order_name}"
    existing = frappe.cache().get_value(lock_key)
    return {"order_name": order_name, "is_locked": existing is not None, "lock_value": existing}


def clear_idempotency_cache(order_name, action=None):
    if action:
        key = f"order_idempotent:{order_name}:{action}"
        frappe.cache().delete_key(key)
    else:
        pattern = f"order_idempotent:{order_name}:*"
        keys = frappe.cache().get_keys(pattern)
        for key in keys:
            frappe.cache().delete_key(key)
