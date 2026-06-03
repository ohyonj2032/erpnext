# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.utils import now

from erpnext.models.order import (
    ORDER_STATUS_TRANSITIONS,
    OrderLifecycleState,
    can_transition,
    get_last_result,
    get_order_statuses,
    get_status_change_log,
    record_status_change as record_status_change_entry,
)


def get_status_transition_map():
    return ORDER_STATUS_TRANSITIONS


def extend_sales_order_class():
    from erpnext.selling.doctype.sales_order.sales_order import SalesOrder

    if getattr(SalesOrder, "_order_race_extension_installed", False):
        return

    original_onload = getattr(SalesOrder, "onload", None)
    original_before_save = getattr(SalesOrder, "before_save", None)
    original_before_submit = getattr(SalesOrder, "before_submit", None)

    def onload(self):
        if original_onload:
            original_onload(self)
        self.set_onload("current_version", int(self.get("version") or 0))
        self.set_onload("available_statuses", get_order_statuses())
        self.set_onload("last_result", self.get_last_operation_result())
        self.set_onload("status_change_log", self.replay_status_change_log())

    def before_save(self):
        if original_before_save:
            original_before_save(self)
        previous = self.get_doc_before_save()
        current_version = int((previous and previous.get("version")) or self.get("version") or 0)
        if not getattr(self.flags, "skip_status_version_increment", False):
            self.version = current_version + 1
        elif self.get("version") is None:
            self.version = current_version
        if previous and previous.status != self.status:
            self.last_status = previous.status
            self.status_changed_at = now()

    def before_submit(self):
        if original_before_submit:
            original_before_submit(self)
        previous = self.get_doc_before_save()
        from_status = previous.status if previous else None
        if from_status and not can_transition(from_status, self.status):
            frappe.throw(
                _("Invalid status transition from {0} to {1}").format(from_status, self.status),
                title=_("Status Validation Error"),
            )

    def can_change_status_to(self, new_status):
        target_state = new_status.value if isinstance(new_status, OrderLifecycleState) else new_status
        return {"can_change": can_transition(self.status, target_state)}

    def get_last_operation_result(self):
        return get_last_result(self)

    def replay_status_change_log(self):
        return get_status_change_log(self)

    def record_status_change(
        self,
        previous_status,
        new_status,
        signature=None,
        result=None,
        rolled_back=False,
        metadata=None,
    ):
        return record_status_change_entry(
            self,
            previous_status=previous_status,
            new_status=new_status,
            signature=signature,
            result=result,
            rolled_back=rolled_back,
            metadata=metadata,
        )

    SalesOrder.onload = onload
    SalesOrder.before_save = before_save
    SalesOrder.before_submit = before_submit
    SalesOrder.can_change_status_to = can_change_status_to
    SalesOrder.get_last_operation_result = get_last_operation_result
    SalesOrder.replay_status_change_log = replay_status_change_log
    SalesOrder.record_status_change = record_status_change
    SalesOrder._order_race_extension_installed = True


def update_sales_order_docfield():
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    create_custom_fields(
        {
            "Sales Order": [
                {
                    "fieldname": "version",
                    "label": "Version",
                    "fieldtype": "Int",
                    "default": 0,
                    "read_only": 1,
                    "hidden": 1,
                    "insert_after": "status",
                },
                {
                    "fieldname": "last_status",
                    "label": "Last Status",
                    "fieldtype": "Data",
                    "read_only": 1,
                    "hidden": 1,
                    "insert_after": "version",
                },
                {
                    "fieldname": "status_changed_at",
                    "label": "Status Changed At",
                    "fieldtype": "Datetime",
                    "read_only": 1,
                    "hidden": 1,
                    "insert_after": "last_status",
                },
                {
                    "fieldname": "signature",
                    "label": "Signature",
                    "fieldtype": "Data",
                    "read_only": 1,
                    "hidden": 1,
                    "insert_after": "status_changed_at",
                },
                {
                    "fieldname": "last_result",
                    "label": "Last Result",
                    "fieldtype": "Long Text",
                    "read_only": 1,
                    "hidden": 1,
                    "insert_after": "signature",
                },
                {
                    "fieldname": "status_change_log",
                    "label": "Status Change Log",
                    "fieldtype": "Long Text",
                    "read_only": 1,
                    "hidden": 1,
                    "insert_after": "last_result",
                },
            ]
        },
        update=True,
    )
    frappe.clear_cache(doctype="Sales Order")


def create_idempotent_operation_log_doctype():
    return None


@frappe.whitelist()
def migrate_sales_order():
    if frappe.session.user != "Administrator":
        frappe.throw(_("Only Administrator can run this migration"))

    update_sales_order_docfield()
    extend_sales_order_class()

    for order_name in frappe.get_all("Sales Order", pluck="name"):
        version = frappe.db.get_value("Sales Order", order_name, "version")
        if version is None:
            frappe.db.set_value("Sales Order", order_name, "version", 0, update_modified=False)

    frappe.db.commit()
    return {"status": "success", "message": _("Migration completed successfully")}


def extend_sales_order_after_install():
    extend_sales_order_class()
