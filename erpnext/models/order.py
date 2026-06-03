import json
from enum import Enum

import frappe
from frappe.utils import now


class OrderState(Enum):
    DRAFT = "Draft"
    ON_HOLD = "On Hold"
    TO_PAY = "To Pay"
    TO_DELIVER_AND_BILL = "To Deliver and Bill"
    TO_BILL = "To Bill"
    TO_DELIVER = "To Deliver"
    COMPLETED = "Completed"
    CANCELLED = "Cancelled"
    CLOSED = "Closed"
    ROLLBACK_PENDING = "Rollback Pending"


ORDER_STATUS_TRANSITIONS = {
    OrderState.DRAFT.value: {
        OrderState.ON_HOLD.value,
        OrderState.TO_DELIVER_AND_BILL.value,
        OrderState.TO_BILL.value,
        OrderState.TO_DELIVER.value,
        OrderState.COMPLETED.value,
        OrderState.CANCELLED.value,
    },
    OrderState.ON_HOLD.value: {
        OrderState.DRAFT.value,
        OrderState.TO_DELIVER_AND_BILL.value,
        OrderState.TO_BILL.value,
        OrderState.TO_DELIVER.value,
        OrderState.COMPLETED.value,
        OrderState.CANCELLED.value,
    },
    OrderState.TO_PAY.value: {
        OrderState.TO_DELIVER_AND_BILL.value,
        OrderState.TO_BILL.value,
        OrderState.TO_DELIVER.value,
        OrderState.COMPLETED.value,
        OrderState.CANCELLED.value,
    },
    OrderState.TO_DELIVER_AND_BILL.value: {
        OrderState.TO_BILL.value,
        OrderState.TO_DELIVER.value,
        OrderState.COMPLETED.value,
        OrderState.CANCELLED.value,
        OrderState.CLOSED.value,
    },
    OrderState.TO_BILL.value: {
        OrderState.COMPLETED.value,
        OrderState.CANCELLED.value,
        OrderState.CLOSED.value,
    },
    OrderState.TO_DELIVER.value: {
        OrderState.COMPLETED.value,
        OrderState.CANCELLED.value,
        OrderState.CLOSED.value,
    },
    OrderState.COMPLETED.value: {
        OrderState.CLOSED.value,
        OrderState.CANCELLED.value,
    },
    OrderState.CLOSED.value: {OrderState.CANCELLED.value},
    OrderState.CANCELLED.value: set(),
    OrderState.ROLLBACK_PENDING.value: {
        OrderState.DRAFT.value,
        OrderState.ON_HOLD.value,
        OrderState.TO_PAY.value,
        OrderState.TO_DELIVER_AND_BILL.value,
        OrderState.TO_BILL.value,
        OrderState.TO_DELIVER.value,
        OrderState.COMPLETED.value,
        OrderState.CANCELLED.value,
        OrderState.CLOSED.value,
    },
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


def normalize_state(status):
    if isinstance(status, OrderState):
        return status.value
    return status


def get_allowed_transitions(from_status):
    return sorted(ORDER_STATUS_TRANSITIONS.get(normalize_state(from_status), set()))


def can_transition(from_status, to_status):
    from_status = normalize_state(from_status)
    to_status = normalize_state(to_status)
    if not from_status or not to_status or from_status == to_status:
        return True
    return to_status in ORDER_STATUS_TRANSITIONS.get(from_status, set())


def snapshot_order(order):
    return {
        "status": order.status,
        "version": order.get("version"),
        "modified": order.modified,
        "signature": order.get("signature"),
        "last_result": get_last_result(order),
        "allowed_transitions": get_allowed_transitions(order.status),
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
    history = get_status_change_log(order)
    metadata = metadata or {}
    event_index = len(history) + 1
    return {
        "sequence": event_index,
        "timestamp": now(),
        "from_status": normalize_state(previous_status),
        "to_status": normalize_state(new_status),
        "signature": signature,
        "rolled_back": rolled_back,
        "metadata": {
            **metadata,
            "order_version": int(order.get("version") or 0),
            "allowed_transitions": get_allowed_transitions(previous_status),
        },
        "result": result or {},
    }


def record_status_change(order, previous_status, new_status, signature=None, result=None, rolled_back=False, metadata=None):
    history = get_status_change_log(order)
    history.append(
        build_status_change_record(
            order,
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
