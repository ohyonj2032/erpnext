import json

import frappe
from frappe.utils import now


ORDER_STATUS_TRANSITIONS = {
    "Draft": {"On Hold", "To Deliver and Bill", "To Bill", "To Deliver", "Completed", "Cancelled"},
    "On Hold": {"Draft", "To Deliver and Bill", "To Bill", "To Deliver", "Completed", "Cancelled"},
    "To Pay": {"To Deliver and Bill", "To Bill", "To Deliver", "Completed", "Cancelled"},
    "To Deliver and Bill": {"To Bill", "To Deliver", "Completed", "Cancelled", "Closed"},
    "To Bill": {"Completed", "Cancelled", "Closed"},
    "To Deliver": {"Completed", "Cancelled", "Closed"},
    "Completed": {"Closed", "Cancelled"},
    "Closed": {"Cancelled"},
    "Cancelled": set(),
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


def can_transition(from_status, to_status):
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
    }


def get_last_result(order):
    return load_json(order.get("last_result"), fallback={})


def get_status_change_log(order):
    return load_json(order.get("status_change_log"), fallback=[])


def record_status_change(order, previous_status, new_status, signature=None, result=None, rolled_back=False, metadata=None):
    history = get_status_change_log(order)
    history.append(
        {
            "timestamp": now(),
            "from_status": previous_status,
            "to_status": new_status,
            "signature": signature,
            "result": result,
            "rolled_back": rolled_back,
            "metadata": metadata or {},
        }
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
