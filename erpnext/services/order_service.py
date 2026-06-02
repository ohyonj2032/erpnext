from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from typing import Any

import frappe
from frappe import _

from erpnext.models.inventory import (
    InsufficientStockError,
    InventoryAdjustment,
    InventoryChange,
    InventoryService,
    OptimisticLockError,
)
from erpnext.models.order import (
    InvalidStateTransitionError,
    OrderEvent,
    OrderState,
    OrderStateMachine,
)

LOCK_TIMEOUT_SECONDS = 30
IDEMPOTENCY_TTL_SECONDS = 3600


class IdempotencyRecord:
    def __init__(
        self,
        idempotency_key: str,
        order_id: str = "",
        result: dict[str, Any] | None = None,
        created_at: datetime | None = None,
        status: str = "completed",
    ):
        self.idempotency_key = idempotency_key
        self.order_id = order_id
        self.result = result or {}
        self.created_at = created_at or datetime.utcnow()
        self.status = status

    def is_expired(self) -> bool:
        return (datetime.utcnow() - self.created_at).total_seconds() > IDEMPOTENCY_TTL_SECONDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.idempotency_key,
            "order_id": self.order_id,
            "result": self.result,
            "created_at": self.created_at.isoformat(),
            "status": self.status,
        }


class OrderLock:
    _locks: dict[str, threading.Lock] = {}
    _lock_registry_lock = threading.Lock()

    def __init__(self, order_id: str, timeout: int = LOCK_TIMEOUT_SECONDS):
        self.order_id = order_id
        self.timeout = timeout
        self._acquired = False

    def __enter__(self) -> OrderLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False

    def acquire(self) -> bool:
        with self._lock_registry_lock:
            if self.order_id not in self._locks:
                self._locks[self.order_id] = threading.Lock()
            lock = self._locks[self.order_id]

        self._acquired = lock.acquire(timeout=self.timeout)
        if not self._acquired:
            raise OrderLockTimeoutError(
                _("Failed to acquire lock for order {0} within {1} seconds").format(
                    self.order_id, self.timeout
                )
            )
        return True

    def release(self) -> None:
        if not self._acquired:
            return
        with self._lock_registry_lock:
            lock = self._locks.get(self.order_id)
        if lock:
            try:
                lock.release()
            except RuntimeError:
                pass
        self._acquired = False

    @classmethod
    def cleanup_locks(cls) -> None:
        with cls._lock_registry_lock:
            cls._locks.clear()


class OrderLockTimeoutError(Exception):
    pass


class OrderNotFoundError(Exception):
    pass


class OrderService:
    @classmethod
    def compute_idempotency_key(cls, order_id: str, payload: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"order_id": order_id, "payload": payload},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def check_idempotency(cls, idempotency_key: str) -> IdempotencyRecord | None:
        record_data = frappe.db.get_value(
            "Idempotency Record",
            {"idempotency_key": idempotency_key},
            ["idempotency_key", "order_id", "result_json", "created_at", "status"],
            as_dict=True,
        )
        if not record_data:
            return None

        record = IdempotencyRecord(
            idempotency_key=record_data.idempotency_key,
            order_id=record_data.order_id,
            result=json.loads(record_data.result_json) if record_data.result_json else {},
            created_at=record_data.created_at,
            status=record_data.status,
        )

        if record.is_expired():
            cls._delete_idempotency_record(idempotency_key)
            return None

        return record

    @classmethod
    def _save_idempotency_record(cls, record: IdempotencyRecord) -> None:
        if frappe.db.exists("Idempotency Record", {"idempotency_key": record.idempotency_key}):
            frappe.db.set_value(
                "Idempotency Record",
                {"idempotency_key": record.idempotency_key},
                {
                    "result_json": json.dumps(record.result),
                    "status": record.status,
                },
            )
        else:
            frappe.get_doc(
                {
                    "doctype": "Idempotency Record",
                    "idempotency_key": record.idempotency_key,
                    "order_id": record.order_id,
                    "result_json": json.dumps(record.result),
                    "status": record.status,
                }
            ).insert(ignore_permissions=True)

    @classmethod
    def _delete_idempotency_record(cls, idempotency_key: str) -> None:
        frappe.db.delete("Idempotency Record", {"idempotency_key": idempotency_key})

    @classmethod
    def validate_state(cls, order_id: str) -> OrderState:
        status = frappe.db.get_value("Sales Order", order_id, "status")
        if not status:
            raise OrderNotFoundError(_("Sales Order {0} not found").format(order_id))
        return OrderState.from_frappe_status(status)

    @classmethod
    def acquire_lock(cls, order_id: str) -> OrderLock:
        return OrderLock(order_id)

    @classmethod
    def _log_event(cls, event: OrderEvent) -> None:
        log_json = json.dumps(
            [e.to_dict() for e in [event]],
            indent=2,
        )

        existing = frappe.db.get_value(
            "Order Event Log",
            {"order_id": event.order_id},
            "event_log_json",
        )

        if existing:
            try:
                existing_events = json.loads(existing)
            except (json.JSONDecodeError, TypeError):
                existing_events = []
            existing_events.append(event.to_dict())
            frappe.db.set_value(
                "Order Event Log",
                {"order_id": event.order_id},
                "event_log_json",
                json.dumps(existing_events),
            )
        else:
            frappe.get_doc(
                {
                    "doctype": "Order Event Log",
                    "order_id": event.order_id,
                    "event_log_json": log_json,
                }
            ).insert(ignore_permissions=True)

    @classmethod
    def get_change_log(cls, order_id: str) -> list[dict[str, Any]]:
        result = frappe.db.get_value(
            "Order Event Log",
            {"order_id": order_id},
            "event_log_json",
        )
        if not result:
            return []
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return []

    @classmethod
    def set_order_state(
        cls,
        order_id: str,
        target_state: OrderState,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        idempotency_key = cls.compute_idempotency_key(order_id, payload or {})

        existing_record = cls.check_idempotency(idempotency_key)
        if existing_record:
            frappe.log(
                "Idempotent replay detected for order {0}, key {1}".format(
                    order_id, idempotency_key[:16]
                )
            )
            return existing_record.result

        with cls.acquire_lock(order_id) as lock:
            current_state = cls.validate_state(order_id)

            OrderStateMachine.validate_transition(current_state, target_state)

            event = OrderEvent(
                order_id=order_id,
                event_type="state_change",
                previous_state=current_state,
                new_state=target_state,
                payload=payload or {},
                signature=idempotency_key,
            )

            try:
                frappe.db.set_value("Sales Order", order_id, "status", target_state.to_frappe_status())
                frappe.db.set_value("Sales Order", order_id, "last_result", json.dumps({"status": "success", "new_state": target_state.value}))
                frappe.db.set_value("Sales Order", order_id, "signature", idempotency_key)
            except Exception as e:
                event.result = {"status": "failed", "error": str(e)}
                frappe.log_error(
                    title="OrderService.set_order_state failed",
                    message=f"Order {order_id}: {e}",
                )
                raise

            event.result = {"status": "success", "new_state": target_state.value}
            cls._log_event(event)

            idempotency_record = IdempotencyRecord(
                idempotency_key=idempotency_key,
                order_id=order_id,
                result=event.result,
            )
            cls._save_idempotency_record(idempotency_record)

            return event.result

    @classmethod
    def submit_order(cls, order_id: str) -> dict[str, Any]:
        result = cls.set_order_state(order_id, OrderState.TO_DELIVER_AND_BILL)
        return result

    @classmethod
    def complete_order(cls, order_id: str) -> dict[str, Any]:
        result = cls.set_order_state(order_id, OrderState.COMPLETED)
        return result

    @classmethod
    def cancel_order(cls, order_id: str) -> dict[str, Any]:
        result = cls.set_order_state(order_id, OrderState.CANCELLED)
        return result

    @classmethod
    def close_order(cls, order_id: str) -> dict[str, Any]:
        result = cls.set_order_state(order_id, OrderState.CLOSED)
        return result

    @classmethod
    def hold_order(cls, order_id: str) -> dict[str, Any]:
        result = cls.set_order_state(order_id, OrderState.ON_HOLD)
        return result

    @classmethod
    def lock_inventory_for_order(
        cls,
        order_id: str,
        item_warehouse_pairs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = {
            "action": "lock_inventory",
            "items": item_warehouse_pairs,
        }
        idempotency_key = cls.compute_idempotency_key(order_id, payload)

        existing_record = cls.check_idempotency(idempotency_key)
        if existing_record:
            return existing_record.result

        with cls.acquire_lock(order_id) as lock:
            current_state = cls.validate_state(order_id)

            OrderStateMachine.validate_transition(current_state, OrderState.INVENTORY_LOCKED)

            adjustment = InventoryAdjustment(
                transaction_id=idempotency_key,
            )

            for pair in item_warehouse_pairs:
                change = InventoryChange(
                    item_code=pair["item_code"],
                    warehouse=pair["warehouse"],
                    quantity=-pair["quantity"],
                    change_type="reserve",
                    reference_doctype="Sales Order",
                    reference_name=order_id,
                    transaction_id=idempotency_key,
                )
                adjustment.deductions.append(change)

            savepoint = f"lock_inv_{order_id}"
            try:
                frappe.db.savepoint(savepoint)

                executed = InventoryService.execute_adjustment(adjustment)

                cls.set_order_state(order_id, OrderState.INVENTORY_LOCKED, payload)

                frappe.db.release_savepoint(savepoint)
            except (OptimisticLockError, InsufficientStockError) as e:
                frappe.db.rollback(save_point=savepoint)

                event = OrderEvent(
                    order_id=order_id,
                    event_type="inventory_lock_failed",
                    previous_state=current_state,
                    new_state=OrderState.FULFILLMENT_FAILED,
                    payload=payload,
                    result={"status": "failed", "error": str(e)},
                )
                cls._log_event(event)

                return {
                    "status": "failed",
                    "error": str(e),
                    "error_type": type(e).__name__,
                }
            except InvalidStateTransitionError:
                frappe.db.rollback(save_point=savepoint)
                raise
            except Exception:
                frappe.db.rollback(save_point=savepoint)
                raise

            event = OrderEvent(
                order_id=order_id,
                event_type="inventory_locked",
                previous_state=current_state,
                new_state=OrderState.INVENTORY_LOCKED,
                payload=payload,
                result={"status": "success", "adjustment": executed.to_dict()},
            )
            cls._log_event(event)

            return {
                "status": "success",
                "adjustment": executed.to_dict(),
            }

    @classmethod
    def deduct_and_account(
        cls,
        order_id: str,
        item_warehouse_pairs: list[dict[str, Any]],
        financial_entries: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "action": "deduct_and_account",
            "items": item_warehouse_pairs,
            "financial_entries": financial_entries,
        }
        idempotency_key = cls.compute_idempotency_key(order_id, payload)

        existing_record = cls.check_idempotency(idempotency_key)
        if existing_record:
            return existing_record.result

        with cls.acquire_lock(order_id) as lock:
            current_state = cls.validate_state(order_id)

            OrderStateMachine.validate_transition(current_state, OrderState.INVENTORY_ADJUSTED)

            adjustment = InventoryAdjustment(
                transaction_id=idempotency_key,
            )

            for pair in item_warehouse_pairs:
                change = InventoryChange(
                    item_code=pair["item_code"],
                    warehouse=pair["warehouse"],
                    quantity=-pair["quantity"],
                    change_type="deduct",
                    reference_doctype="Sales Order",
                    reference_name=order_id,
                    transaction_id=idempotency_key,
                )
                adjustment.deductions.append(change)

            savepoint = f"deduct_acct_{order_id}"
            financial_result = None

            try:
                frappe.db.savepoint(savepoint)

                executed = InventoryService.execute_adjustment(adjustment)

                if financial_entries:
                    financial_result = cls._create_financial_entries(
                        order_id, financial_entries, idempotency_key
                    )

                cls.set_order_state(order_id, OrderState.INVENTORY_ADJUSTED, payload)

                frappe.db.release_savepoint(savepoint)
            except (OptimisticLockError, InsufficientStockError) as e:
                frappe.db.rollback(save_point=savepoint)

                event = OrderEvent(
                    order_id=order_id,
                    event_type="deduct_account_failed",
                    previous_state=current_state,
                    new_state=OrderState.FULFILLMENT_FAILED,
                    payload=payload,
                    result={"status": "failed", "error": str(e)},
                )
                cls._log_event(event)

                return {
                    "status": "failed",
                    "error": str(e),
                    "error_type": type(e).__name__,
                }
            except Exception:
                frappe.db.rollback(save_point=savepoint)
                raise

            event = OrderEvent(
                order_id=order_id,
                event_type="inventory_adjusted",
                previous_state=current_state,
                new_state=OrderState.INVENTORY_ADJUSTED,
                payload=payload,
                result={
                    "status": "success",
                    "adjustment": executed.to_dict(),
                    "financial_result": financial_result,
                },
            )
            cls._log_event(event)

            return {
                "status": "success",
                "adjustment": executed.to_dict(),
                "financial_result": financial_result,
            }

    @classmethod
    def _create_financial_entries(
        cls,
        order_id: str,
        financial_entries: list[dict[str, Any]],
        transaction_id: str,
    ) -> list[dict[str, Any]]:
        results = []
        for entry in financial_entries:
            results.append(
                {
                    "entry_type": entry.get("type", "journal_entry"),
                    "amount": entry.get("amount", 0),
                    "account": entry.get("account", ""),
                    "transaction_id": transaction_id,
                    "status": "created",
                }
            )
        return results

    @classmethod
    def rollback_order(
        cls,
        order_id: str,
        reason: str = "",
    ) -> dict[str, Any]:
        payload = {"action": "rollback", "reason": reason}
        idempotency_key = cls.compute_idempotency_key(order_id, payload)

        existing_record = cls.check_idempotency(idempotency_key)
        if existing_record:
            return existing_record.result

        with cls.acquire_lock(order_id) as lock:
            current_state = cls.validate_state(order_id)

            allowed_states = {
                OrderState.INVENTORY_LOCKED,
                OrderState.INVENTORY_ADJUSTED,
                OrderState.PARTLY_FULFILLED,
                OrderState.FULFILLMENT_FAILED,
            }

            if current_state not in allowed_states:
                raise InvalidStateTransitionError(
                    _("Rollback not allowed from state {0}").format(current_state.value)
                )

            change_log = cls.get_change_log(order_id)

            savepoint = f"rollback_{order_id}"
            try:
                frappe.db.savepoint(savepoint)

                cls.set_order_state(order_id, OrderState.TO_DELIVER_AND_BILL, payload)

                frappe.db.release_savepoint(savepoint)
            except Exception:
                frappe.db.rollback(save_point=savepoint)
                raise

            event = OrderEvent(
                order_id=order_id,
                event_type="rollback",
                previous_state=current_state,
                new_state=OrderState.TO_DELIVER_AND_BILL,
                payload=payload,
                result={"status": "success", "reason": reason},
            )
            cls._log_event(event)

            return {
                "status": "success",
                "reason": reason,
                "previous_state": current_state.value,
                "change_log": change_log,
            }

    @classmethod
    def get_order_status(cls, order_id: str) -> dict[str, Any]:
        status = cls.validate_state(order_id)
        change_log = cls.get_change_log(order_id)
        return {
            "order_id": order_id,
            "status": status.value,
            "allowed_transitions": [
                s.value for s in OrderStateMachine.get_allowed_transitions(status)
            ],
            "event_count": len(change_log),
            "last_event": change_log[-1] if change_log else None,
        }

    @classmethod
    def process_order_fulfillment(
        cls,
        order_id: str,
        items: list[dict[str, Any]],
        financial_entries: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        lock_result = cls.lock_inventory_for_order(order_id, items)
        if lock_result["status"] != "success":
            return lock_result

        deduct_result = cls.deduct_and_account(order_id, items, financial_entries)
        if deduct_result["status"] != "success":
            return deduct_result

        complete_result = cls.complete_order(order_id)

        return {
            "status": "success",
            "lock_result": lock_result,
            "deduct_result": deduct_result,
            "complete_result": complete_result,
        }