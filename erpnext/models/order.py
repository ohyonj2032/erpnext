import hashlib
import json
from enum import Enum

import frappe
from frappe import _
from frappe.utils import now_datetime


class OrderStatus(str, Enum):
    DRAFT = "Draft"
    ON_HOLD = "On Hold"
    TO_PAY = "To Pay"
    TO_DELIVER_AND_BILL = "To Deliver and Bill"
    TO_BILL = "To Bill"
    TO_DELIVER = "To Deliver"
    COMPLETED = "Completed"
    CANCELLED = "Cancelled"
    CLOSED = "Closed"


VALID_TRANSITIONS = {
    OrderStatus.DRAFT: [
        OrderStatus.ON_HOLD,
        OrderStatus.TO_DELIVER_AND_BILL,
        OrderStatus.TO_BILL,
        OrderStatus.TO_DELIVER,
        OrderStatus.CANCELLED,
    ],
    OrderStatus.ON_HOLD: [
        OrderStatus.DRAFT,
        OrderStatus.TO_DELIVER_AND_BILL,
        OrderStatus.TO_BILL,
        OrderStatus.TO_DELIVER,
        OrderStatus.CANCELLED,
    ],
    OrderStatus.TO_DELIVER_AND_BILL: [
        OrderStatus.TO_BILL,
        OrderStatus.TO_DELIVER,
        OrderStatus.COMPLETED,
        OrderStatus.CLOSED,
        OrderStatus.CANCELLED,
    ],
    OrderStatus.TO_BILL: [
        OrderStatus.COMPLETED,
        OrderStatus.CLOSED,
        OrderStatus.CANCELLED,
    ],
    OrderStatus.TO_DELIVER: [
        OrderStatus.COMPLETED,
        OrderStatus.CLOSED,
        OrderStatus.CANCELLED,
    ],
    OrderStatus.COMPLETED: [
        OrderStatus.CLOSED,
    ],
    OrderStatus.CLOSED: [
        OrderStatus.TO_DELIVER_AND_BILL,
        OrderStatus.TO_BILL,
        OrderStatus.TO_DELIVER,
    ],
    OrderStatus.CANCELLED: [],
}


class InvalidStateTransition(frappe.ValidationError):
    pass


class OrderAlreadyProcessed(frappe.ValidationError):
    pass


def validate_transition(current_status, target_status):
    current = OrderStatus(current_status) if not isinstance(current_status, OrderStatus) else current_status
    target = OrderStatus(target_status) if not isinstance(target_status, OrderStatus) else target_status
    allowed = VALID_TRANSITIONS.get(current, [])
    if target not in allowed:
        frappe.throw(
            _("Invalid state transition from {0} to {1}").format(current.value, target.value),
            InvalidStateTransition,
        )
    return True


def compute_signature(order_name, action, **kwargs):
    payload = json.dumps({"order_name": order_name, "action": action, **kwargs}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def get_last_result(order_name, action):
    key = f"order_idempotent:{order_name}:{action}"
    cached = frappe.cache().get_value(key)
    return cached


def set_last_result(order_name, action, result, expires_sec=86400):
    key = f"order_idempotent:{order_name}:{action}"
    frappe.cache().set_value(key, result, expires_in_sec=expires_sec)


def check_idempotency(order_name, action, signature=None):
    cached = get_last_result(order_name, action)
    if cached:
        return True, cached
    return False, None


def record_state_change(order_name, from_status, to_status, action, result=None):
    doc = frappe.get_doc("Sales Order", order_name)
    if not hasattr(doc, "_state_history"):
        doc._state_history = []

    entry = {
        "timestamp": str(now_datetime()),
        "from_status": from_status,
        "to_status": to_status,
        "action": action,
        "result": result,
    }
    doc._state_history.append(entry)

    history_key = f"order_state_history:{order_name}"
    existing = frappe.cache().get_value(history_key) or []
    existing.append(entry)
    frappe.cache().set_value(history_key, existing, expires_in_sec=86400 * 7)

    return entry


def get_state_history(order_name):
    history_key = f"order_state_history:{order_name}"
    return frappe.cache().get_value(history_key) or []
