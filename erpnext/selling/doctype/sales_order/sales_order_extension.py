
# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""
Sales Order Extension - State Machine and Concurrency Features
Adds fields and methods for state tracking, optimistic locking, and idempotency
"""

import frappe
from frappe import _
from frappe.utils import now
from datetime import datetime


def extend_sales_order_class():
    """
    Extend the Sales Order class with concurrency and state machine features
    """
    from erpnext.selling.doctype.sales_order.sales_order import SalesOrder

    # Store the original onload
    original_onload = SalesOrder.onload

    def onload(self):
        original_onload(self)
        # Add version information for optimistic locking
        if hasattr(self, 'version'):
            self.set_onload('current_version', self.version)

    SalesOrder.onload = onload

    # Store the original before_save
    original_before_save = SalesOrder.before_save

    def before_save(self):
        original_before_save(self)
        # Update version for optimistic locking
        if self.get('version') is None:
            self.version = 1
        else:
            self.version = int(self.version) + 1

        # Track last status change
        if self.get_doc_before_save() and self.get_doc_before_save().status != self.status:
            self.last_status = self.get_doc_before_save().status
            self.status_changed_at = now()

    SalesOrder.before_save = before_save

    # Store the original before_submit
    original_before_submit = SalesOrder.before_submit if hasattr(SalesOrder, 'before_submit') else None

    def before_submit(self):
        if original_before_submit:
            original_before_submit(self)
        # Validate state transition
        if self.get_doc_before_save():
            self._validate_state_transition(
                self.get_doc_before_save().status,
                self.status
            )

    SalesOrder.before_submit = before_submit

    # Add new methods
    def _validate_state_transition(self, from_status, to_status):
        """
        Validate if the state transition is allowed
        """
        # Define valid state transitions
        valid_transitions = {
            "Draft": ["On Hold", "To Deliver and Bill", "To Bill", "To Deliver", "Completed", "Cancelled"],
            "On Hold": ["To Deliver and Bill", "To Bill", "To Deliver", "Completed", "Cancelled", "Draft"],
            "To Pay": ["To Deliver and Bill", "To Bill", "To Deliver", "Completed", "Cancelled"],
            "To Deliver and Bill": ["To Bill", "To Deliver", "Completed", "Cancelled"],
            "To Bill": ["Completed", "Cancelled"],
            "To Deliver": ["Completed", "Cancelled"],
            "Completed": ["Closed", "Cancelled"],
            "Closed": ["Cancelled"],
            "Cancelled": []
        }

        if from_status and to_status and from_status != to_status:
            if to_status not in valid_transitions.get(from_status, []):
                frappe.throw(
                    _("Invalid status transition from {0} to {1}").format(from_status, to_status),
                    title=_("Status Validation Error")
                )

    SalesOrder._validate_state_transition = _validate_state_transition

    def record_idempotent_operation(self, idempotency_key, operation, result):
        """
        Record an idempotent operation result for future reference
        """
        if not frappe.db.exists("Idempotent Operation Log", {"idempotency_key": idempotency_key}):
            log = frappe.get_doc({
                "doctype": "Idempotent Operation Log",
                "reference_doctype": self.doctype,
                "reference_name": self.name,
                "operation": operation,
                "idempotency_key": idempotency_key,
                "result": str(result),
                "created_at": now()
            })
            log.insert(ignore_permissions=True)

    SalesOrder.record_idempotent_operation = record_idempotent_operation

    @frappe.whitelist()
    def can_change_status_to(self, new_status):
        """
        Check if the order can transition to the given status
        """
        try:
            self._validate_state_transition(self.status, new_status)
            return {"can_change": True}
        except Exception as e:
            return {"can_change": False, "reason": str(e)}

    SalesOrder.can_change_status_to = can_change_status_to


def update_sales_order_docfield():
    """
    Add new fields to Sales Order DocType
    """
    # Check if fields already exist
    if frappe.db.exists("DocField", {"parent": "Sales Order", "fieldname": "version"}):
        return

    # Add version field for optimistic locking
    frappe.get_doc({
        "doctype": "DocField",
        "parent": "Sales Order",
        "parenttype": "DocType",
        "parentfield": "fields",
        "fieldname": "version",
        "label": "Version",
        "fieldtype": "Int",
        "default": 1,
        "readonly": 1,
        "hidden": 1,
        "in_list_view": 0,
        "idx": 9999
    }).insert(ignore_permissions=True)

    # Add last_status field
    frappe.get_doc({
        "doctype": "DocField",
        "parent": "Sales Order",
        "parenttype": "DocType",
        "parentfield": "fields",
        "fieldname": "last_status",
        "label": "Last Status",
        "fieldtype": "Data",
        "readonly": 1,
        "hidden": 1,
        "idx": 9998
    }).insert(ignore_permissions=True)

    # Add status_changed_at field
    frappe.get_doc({
        "doctype": "DocField",
        "parent": "Sales Order",
        "parenttype": "DocType",
        "parentfield": "fields",
        "fieldname": "status_changed_at",
        "label": "Status Changed At",
        "fieldtype": "Datetime",
        "readonly": 1,
        "hidden": 1,
        "idx": 9997
    }).insert(ignore_permissions=True)

    # Add last_operation_signature field
    frappe.get_doc({
        "doctype": "DocField",
        "parent": "Sales Order",
        "parenttype": "DocType",
        "parentfield": "fields",
        "fieldname": "last_operation_signature",
        "label": "Last Operation Signature",
        "fieldtype": "Data",
        "readonly": 1,
        "hidden": 1,
        "idx": 9996
    }).insert(ignore_permissions=True)

    frappe.db.commit()


def create_idempotent_operation_log_doctype():
    """
    Create Idempotent Operation Log DoType to track idempotent operations
    """
    if frappe.db.exists("DocType", "Idempotent Operation Log"):
        return

    doc = frappe.get_doc({
        "doctype": "DocType",
        "name": "Idempotent Operation Log",
        "module": "Selling",
        "custom": 1,
        "is_submittable": 0,
        "istable": 0,
        "editable_grid": 1,
        "sort_field": "creation",
        "sort_order": "DESC",
        "track_changes": 1,
        "fields": [
            {
                "doctype": "DocField",
                "fieldname": "reference_doctype",
                "label": "Reference Doctype",
                "fieldtype": "Link",
                "options": "DocType",
                "reqd": 1,
                "in_list_view": 1,
                "idx": 1
            },
            {
                "doctype": "DocField",
                "fieldname": "reference_name",
                "label": "Reference Name",
                "fieldtype": "Dynamic Link",
                "options": "reference_doctype",
                "reqd": 1,
                "in_list_view": 1,
                "idx": 2
            },
            {
                "doctype": "DocField",
                "fieldname": "operation",
                "label": "Operation",
                "fieldtype": "Data",
                "reqd": 1,
                "in_list_view": 1,
                "idx": 3
            },
            {
                "doctype": "DocField",
                "fieldname": "idempotency_key",
                "label": "Idempotency Key",
                "fieldtype": "Data",
                "reqd": 1,
                "in_list_view": 1,
                "idx": 4
            },
            {
                "doctype": "DocField",
                "fieldname": "result",
                "label": "Result",
                "fieldtype": "Text",
                "idx": 5
            },
            {
                "doctype": "DocField",
                "fieldname": "created_at",
                "label": "Created At",
                "fieldtype": "Datetime",
                "reqd": 1,
                "in_list_view": 1,
                "idx": 6
            }
        ],
        "permissions": [
            {
                "role": "System Manager",
                "read": 1,
                "write": 1,
                "create": 1,
                "delete": 1
            }
        ]
    })
    doc.insert(ignore_permissions=True)
    frappe.db.commit()


@frappe.whitelist()
def migrate_sales_order():
    """
    Run migration for Sales Order extension
    """
    if not frappe.session.user == "Administrator":
        frappe.throw(_("Only Administrator can run this migration"))

    create_idempotent_operation_log_doctype()
    update_sales_order_docfield()

    # Initialize version for existing orders
    orders = frappe.get_all("Sales Order", filters=[["version", "is", "not set"]], limit=1000)
    for order in orders:
        frappe.db.set_value("Sales Order", order.name, "version", 1)

    frappe.db.commit()
    return {"status": "success", "message": _("Migration completed successfully")}


def extend_sales_order_after_install():
    """
    Hook to call after app install
    """
    try:
        extend_sales_order_class()
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Sales Order Extension Error")

