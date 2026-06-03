import json
from enum import Enum

import frappe
from frappe.utils import now


class OrderLifecycleState(Enum):
    DRAFT = "Draft"
    ON_HOLD = "On Hold"
    TO_PAY = "To Pay"
    TO_DELIVER_AND_BILL = "To Deliver and Bill"
    TO_BILL = "To Bill"
    TO_DELIVER = "To Deliver"
    COMPLETED = "Completed"
    CANCELLED = "Cancelled"
    CLOSED = "Closed"


ORDER_STATUS_TRANSITIONS = {
    OrderLifecycleState.DRAFT.value: {
        OrderLifecycleState.ON_HOLD.value,
        OrderLifecycleState.TO_DELIVER_AND_BILL.value,
        OrderLifecycleState.TO_BILL.value,
        OrderLifecycleState.TO_DELIVER.value,
        OrderLifecycleState.COMPLETED.value,
        OrderLifecycleState.CANCELLED.value,
    },
    OrderLifecycleState.ON_HOLD.value: {
        OrderLifecycleState.DRAFT.value,
        OrderLifecycleState.TO_DELIVER_AND_BILL.value,
        OrderLifecycleState.TO_BILL.value,
        OrderLifecycleState.TO_DELIVER.value,
        OrderLifecycleState.COMPLETED.value,
        OrderLifecycleState.CANCELLED.value,
    },
    OrderLifecycleState.TO_PAY.value: {
        OrderLifecycleState.TO_DELIVER_AND_BILL.value,
        OrderLifecycleState.TO_BILL.value,
        OrderLifecycleState.TO_DELIVER.value,
        OrderLifecycleState.COMPLETED.value,
        OrderLifecycleState.CANCELLED.value,
    },
    OrderLifecycleState.TO_DELIVER_AND_BILL.value: {
        OrderLifecycleState.TO_BILL.value,
        OrderLifecycleState.TO_DELIVER.value,
        OrderLifecycleState.COMPLETED.value,
        OrderLifecycleState.CANCELLED.value,
        OrderLifecycleState.CLOSED.value,
    },
    OrderLifecycleState.TO_BILL.value: {
        OrderLifecycleState.COMPLETED.value,
        OrderLifecycleState.CANCELLED.value,
        OrderLifecycleState.CLOSED.value,
    },
    OrderLifecycleState.TO_DELIVER.value: {
        OrderLifecycleState.COMPLETED.value,
        OrderLifecycleState.CANCELLED.value,
        OrderLifecycleState.CLOSED.value,
    },
    OrderLifecycleState.COMPLETED.value: {
        OrderLifecycleState.CLOSED.value,
        OrderLifecycleState.CANCELLED.value,
    },
    OrderLifecycleState.CLOSED.value: {OrderLifecycleState.CANCELLED.value},
    OrderLifecycleState.CANCELLED.value: set(),
}


def dump_json(value):
    return frappe.as_json(value) if value is not None else None


def load_json(value, fallback=None):
    if not value:
        return [] if fallback is None else fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return [] if fallback is None else fallback


def normalize_state(state):
    if isinstance(state, Enum):
        return state.value
    return state


def get_order_statuses():
    return [state.value for state in OrderLifecycleState]


def can_transition(from_status, to_status):
    from_status = normalize_state(from_status)
    to_status = normalize_state(to_status)
    if not from_status or not to_status or from_status == to_status:
        return True
    return to_status in ORDER_STATUS_TRANSITIONS.get(from_status, set())


def snapshot_order(order):
    return {
        "status": order.status,
        "last_status": order.get("last_status"),
        "version": int(order.get("version") or 0),
        "modified": order.modified,
        "signature": order.get("signature"),
        "status_changed_at": order.get("status_changed_at"),
        "last_result": get_last_result(order),
    }


def get_last_result(order):
    return load_json(order.get("last_result"), fallback={})


def get_status_change_log(order):
    return load_json(order.get("status_change_log"), fallback=[])


def build_status_change_record(
    order,
    previous_status,
    new_status,
    signature=None,
    result=None,
    rolled_back=False,
    metadata=None,
):
    result = result or {}
    return {
        "timestamp": now(),
        "from_status": previous_status,
        "to_status": new_status,
        "signature": signature,
        "result": result,
        "rolled_back": rolled_back,
        "metadata": metadata or {},
        "snapshot": snapshot_order(order),
        "rollback_state": result.get("rollback_state"),
    }


def record_status_change(order, previous_status, new_status, signature=None, result=None, rolled_back=False, metadata=None):
    history = get_status_change_log(order)
    history.append(
        build_status_change_record(
            order=order,
            previous_status=previous_status,
            new_status=new_status,
            signature=signature,
            result=result,
            rolled_back=rolled_back,
            metadata=metadata,
        )
    )
    history = history[-20:]
    if "last_status" in order.meta.get_valid_columns():
        order.last_status = previous_status
    if "status_changed_at" in order.meta.get_valid_columns():
        order.status_changed_at = now()
    if "signature" in order.meta.get_valid_columns():
        order.signature = signature
    if "last_result" in order.meta.get_valid_columns():
        order.last_result = dump_json(result or {})
    if "status_change_log" in order.meta.get_valid_columns():
        order.status_change_log = dump_json(history)
    return history
