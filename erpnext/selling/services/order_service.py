# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import json
import time

import frappe
from frappe import _
from frappe.utils import flt, now

from erpnext.models.order import (
    OrderLifecycleState,
    get_last_result,
    record_status_change,
    snapshot_order,
)
from erpnext.selling.doctype.sales_order.sales_order_extension import (
    can_transition,
    extend_sales_order_class,
    update_sales_order_docfield,
)


OrderStatus = OrderLifecycleState


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
    def _coerce_cached_result(cls, value):
        if not value:
            return None
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, bytes):
            value = value.decode()
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except Exception:
                return None
            return parsed if isinstance(parsed, dict) else None
        return None

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
        lock_value = json.dumps(
            {
                "owner": frappe.session.user,
                "token": frappe.generate_hash(length=12),
                "issued_at": time.time(),
            },
            sort_keys=True,
            default=str,
        )
        attempts = max(1, int(timeout / cls.LOCK_RETRY_INTERVAL))
        cache = frappe.cache()

        for _ in range(attempts):
            if cache.set(lock_key, lock_value, expire=timeout, nx=True):
                cls._increment_metric("lock_acquisitions")
                return {"key": lock_key, "value": lock_value}
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
    def release_lock(cls, lock_context):
        if not lock_context:
            return

        cache = frappe.cache()
        current_value = cache.get(lock_context["key"])
        if isinstance(current_value, bytes):
            current_value = current_value.decode()
        if current_value == lock_context["value"]:
            cache.delete(lock_context["key"])
            cls._increment_metric("lock_releases")

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
            "lock_releases": int(cache.get(f"{cls.METRIC_PREFIX}lock_releases") or 0),
            "lock_timeouts": int(cache.get(f"{cls.METRIC_PREFIX}lock_timeouts") or 0),
            "idempotent_hits": int(cache.get(f"{cls.METRIC_PREFIX}idempotent_hits") or 0),
            "rollback_events": int(cache.get(f"{cls.METRIC_PREFIX}rollback_events") or 0),
            "state_transition_rejections": int(
                cache.get(f"{cls.METRIC_PREFIX}state_transition_rejections") or 0
            ),
        }

    @classmethod
    def _idempotency_cache_key(cls, order_id, signature):
        return f"{cls.IDEMPOTENCY_PREFIX}{order_id}:{signature}"

    @classmethod
    def check_idempotency(cls, order_id, signature, order=None):
        cached = cls._coerce_cached_result(frappe.cache().get(cls._idempotency_cache_key(order_id, signature)))
        if cached:
            cls._increment_metric("idempotent_hits")
            cached["is_idempotent"] = True
            return cached

        order = order or frappe.get_doc("Sales Order", order_id)
        last_signature = order.get("signature")
        last_result = cls._extract_last_result(order)
        if last_signature == signature and last_result:
            cls._increment_metric("idempotent_hits")
            result = dict(last_result)
            result["is_idempotent"] = True
            cls.store_idempotent_result(order_id, signature, result)
            return result

        return None

    @classmethod
    def store_idempotent_result(cls, order_id, signature, result):
        frappe.cache().set(
            cls._idempotency_cache_key(order_id, signature),
            json.dumps(result, sort_keys=True, default=str),
            expire=3600,
        )

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
            return [dict(row) for row in inventory_deductions]

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
                    "qty": flt(item.get("stock_qty") or item.get("qty") or 0),
                }
            )
        return deductions

    @classmethod
    def _prepare_accounting_entries(cls, order, inventory_deductions, accounting_entries):
        if accounting_entries:
            return [dict(entry) for entry in accounting_entries]

        order_items = {item.item_code: item for item in order.items}
        entries = []
        for deduction in inventory_deductions:
            row = order_items.get(deduction["item_code"])
            qty = flt(deduction.get("qty") or 0)
            rate = flt((row and row.get("rate")) or 0)
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
    def _build_accounting_summary(cls, accounting_entries):
        entries = accounting_entries or []
        return {
            "entry_count": len(entries),
            "ledger_amount_total": sum(flt(entry.get("ledger_amount")) for entry in entries),
            "currencies": sorted({entry.get("ledger_currency") for entry in entries if entry.get("ledger_currency")}),
        }

    @classmethod
    def _apply_order_result(cls, order, previous_status, new_status, signature, result, rolled_back=False):
        metadata = {
            "success": result.get("success", False),
            "result_status": result.get("status"),
            "inventory_transaction_id": (result.get("inventory") or {}).get("transaction_id")
            if isinstance(result.get("inventory"), dict)
            else None,
            "accounting_summary": result.get("accounting_summary"),
            "rollback_state": result.get("rollback_state"),
        }
        if hasattr(order, "record_status_change"):
            order.record_status_change(
                previous_status=previous_status,
                new_status=new_status,
                signature=signature,
                result=result,
                rolled_back=rolled_back,
                metadata=metadata,
            )
        else:
            record_status_change(
                order,
                previous_status=previous_status,
                new_status=new_status,
                signature=signature,
                result=result,
                rolled_back=rolled_back,
                metadata=metadata,
            )

    @classmethod
    def _persist_rollback_result(cls, order_id, previous_status, new_status, signature, result):
        try:
            order = frappe.get_doc("Sales Order", order_id)
            order.flags.skip_status_version_increment = True
            cls._apply_order_result(order, previous_status, new_status, signature, result, rolled_back=True)
            order.save(ignore_permissions=True)
            order.flags.skip_status_version_increment = False
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"Failed to persist rollback result for {order_id}")

    @classmethod
    def _build_current_state_result(cls, order, signature, new_status):
        result = dict(cls._extract_last_result(order) or {})
        result.update(
            {
                "success": True,
                "status": "success",
                "order_id": order.name,
                "previous_status": result.get("previous_status") or order.get("last_status") or order.status,
                "new_status": new_status,
                "signature": signature,
                "version": int(order.get("version") or 0),
                "rolled_back": False,
                "state": cls._snapshot_order(order),
                "timestamp": now(),
                "metrics": cls.get_metric_snapshot(),
            }
        )
        return result

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
            "version": int(order.get("version") or 0),
            "inventory": inventory_result,
            "accounting_entries": accounting_entries,
            "accounting_summary": cls._build_accounting_summary(accounting_entries),
            "rolled_back": False,
            "state": cls._snapshot_order(order),
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
            "accounting_summary": cls._build_accounting_summary(accounting_entries),
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
            extra_payload={"simulate_failure": simulate_failure, **kwargs},
        )

        cached = cls.check_idempotency(order_id, signature)
        if cached:
            return cached

        lock_context = None
        previous_status = None
        rollback_state = {}
        deductions = []
        entries = []

        try:
            lock_context = cls.acquire_lock(order_id)
            try:
                frappe.db.begin()
                order = cls._lock_order_row(order_id)
                previous_status = order.status
                rollback_state = cls._snapshot_order(order)

                cached = cls.check_idempotency(order_id, signature, order=order)
                if cached:
                    frappe.db.rollback()
                    return cached

                if order.status == new_status:
                    cls._increment_metric("idempotent_hits")
                    current_result = cls._build_current_state_result(order, signature, new_status)
                    current_result["is_idempotent"] = True
                    cls.store_idempotent_result(order_id, signature, current_result)
                    frappe.db.rollback()
                    return current_result

                if not cls.validate_status_transition(order.status, new_status):
                    cls._increment_metric("state_transition_rejections")
                    failure = cls._build_failure_result(
                        order_id=order_id,
                        signature=signature,
                        previous_status=order.status,
                        attempted_status=new_status,
                        error=_("Cannot transition from {0} to {1}").format(order.status, new_status),
                        rollback_state=rollback_state,
                        inventory_deductions=inventory_deductions or [],
                        accounting_entries=accounting_entries or [],
                    )
                    raise StatusTransitionError(failure["error"], result=failure)

                deductions = cls._prepare_inventory_deductions(order, inventory_deductions)
                entries = cls._prepare_accounting_entries(order, deductions, accounting_entries)
                inventory_result = {
                    "status": "skipped",
                    "deductions": [],
                    "accounting_entries": [],
                    "accounting_summary": cls._build_accounting_summary(entries),
                }

                if deductions or entries:
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
                elif simulate_failure:
                    raise OrderOperationError(
                        _("Simulated inventory/accounting rollback for Sales Order {0}").format(order.name)
                    )

                order.status = new_status
                order.save(ignore_permissions=True)

                success_result = cls._build_success_result(
                    order=order,
                    signature=signature,
                    previous_status=previous_status,
                    new_status=new_status,
                    inventory_result=inventory_result,
                    accounting_entries=entries,
                )
                order.flags.skip_status_version_increment = True
                cls._apply_order_result(order, previous_status, new_status, signature, success_result)
                order.save(ignore_permissions=True)
                order.flags.skip_status_version_increment = False
                frappe.db.commit()
            except StatusTransitionError as exc:
                frappe.db.rollback()
                cls.store_idempotent_result(order_id, signature, exc.result)
                cls._persist_rollback_result(order_id, previous_status, new_status, signature, exc.result)
                raise
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
            cls.release_lock(lock_context)

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
