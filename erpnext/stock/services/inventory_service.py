
# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""
Inventory Service - Concurrency and Atomic Operations
Handles atomic inventory deductions with optimistic locking and transaction safety
"""

import frappe
from frappe import _
from frappe.utils import flt
from typing import Dict, List, Optional, Tuple


class InventoryService:
    """
    Service class for inventory operations with concurrency control
    """

    @classmethod
    def atomic_deduct_inventory(
        cls,
        item_code: str,
        warehouse: str,
        qty: float,
        reservation_id: str = None,
        transaction_id: str = None,
        **kwargs
    ) -> Dict:
        """
        Atomically deduct inventory with optimistic locking

        Args:
            item_code: Item to deduct
            warehouse: Warehouse location
            qty: Quantity to deduct (positive number)
            reservation_id: Optional stock reservation ID
            transaction_id: Optional transaction ID for idempotency

        Returns:
            Result dict with status and details

        Raises:
            frappe.ValidationError: If inventory is insufficient or concurrency conflict
        """
        return frappe.db.transaction(
            cls._atomic_deduct_inventory_impl,
            item_code=item_code,
            warehouse=warehouse,
            qty=qty,
            reservation_id=reservation_id,
            transaction_id=transaction_id,
            **kwargs
        )

    @classmethod
    def _atomic_deduct_inventory_impl(
        cls,
        item_code: str,
        warehouse: str,
        qty: float,
        reservation_id: str = None,
        transaction_id: str = None,
        **kwargs
    ) -> Dict:
        """
        Implementation of atomic deduction - runs within a transaction
        """
        # Check for idempotency
        if transaction_id:
            existing_log = frappe.db.get_value(
                "Inventory Transaction Log",
                {"transaction_id": transaction_id},
                ["name", "result"]
            )
            if existing_log:
                return {
                    "status": "success",
                    "is_idempotent": True,
                    "log_name": existing_log[0],
                    "result": frappe.parse_json(existing_log[1])
                }

        # Get Bin with FOR UPDATE to lock it
        bin_name = frappe.db.get_value(
            "Bin",
            {"item_code": item_code, "warehouse": warehouse},
            for_update=True
        )

        if not bin_name:
            frappe.throw(
                _("No stock balance found for Item {0} in Warehouse {1}").format(item_code, warehouse),
                title=_("Inventory Error")
            )

        # Get current bin details
        bin_doc = frappe.get_doc("Bin", bin_name)
        current_version = bin_doc.get("version", 1)

        # Check if we were given an expected version
        expected_version = kwargs.get("expected_version")
        if expected_version and expected_version != current_version:
            frappe.throw(
                _("Concurrent modification detected. Bin {0} has been updated by another process.").format(bin_name),
                title=_("Concurrency Conflict")
            )

        # Check stock availability
        available_qty = flt(bin_doc.actual_qty) - flt(bin_doc.reserved_qty)
        if available_qty < flt(qty):
            frappe.throw(
                _("Insufficient stock. Available: {0}, Required: {1}").format(available_qty, qty),
                title=_("Insufficient Stock")
            )

        # Perform the deduction
        from erpnext.stock.stock_ledger import get_previous_sle

        # Create stock ledger entry (simplified)
        sle = frappe.get_doc({
            "doctype": "Stock Ledger Entry",
            "item_code": item_code,
            "warehouse": warehouse,
            "posting_date": kwargs.get("posting_date", frappe.utils.today()),
            "posting_time": kwargs.get("posting_time", frappe.utils.nowtime()),
            "voucher_type": "Inventory Deduction",
            "voucher_no": kwargs.get("voucher_no", transaction_id or frappe.generate_hash()),
            "actual_qty": -flt(qty),
            "qty_after_transaction": flt(bin_doc.actual_qty) - flt(qty),
            "is_cancelled": 0,
            "valuation_rate": bin_doc.valuation_rate or 0.0,
            "stock_value_difference": -flt(qty) * flt(bin_doc.valuation_rate or 0.0),
        })
        sle.insert(ignore_permissions=True)

        # Update Bin
        bin_doc.actual_qty = flt(bin_doc.actual_qty) - flt(qty)
        bin_doc.version = current_version + 1
        bin_doc.set_projected_qty()
        bin_doc.save(ignore_permissions=True)

        # Log the transaction
        result = {
            "status": "success",
            "item_code": item_code,
            "warehouse": warehouse,
            "deducted_qty": qty,
            "new_actual_qty": bin_doc.actual_qty,
            "bin_version": bin_doc.version,
            "stock_ledger_entry": sle.name,
            "transaction_id": transaction_id
        }

        if transaction_id:
            cls._log_transaction(
                transaction_id=transaction_id,
                operation="atomic_deduct_inventory",
                item_code=item_code,
                warehouse=warehouse,
                qty=qty,
                result=result
            )

        return result

    @classmethod
    def _log_transaction(cls, transaction_id: str, operation: str, **kwargs):
        """
        Log inventory transaction for idempotency and audit
        """
        if not frappe.db.exists("DocType", "Inventory Transaction Log"):
            cls._create_transaction_log_doctype()

        log = frappe.get_doc({
            "doctype": "Inventory Transaction Log",
            "transaction_id": transaction_id,
            "operation": operation,
            "item_code": kwargs.get("item_code"),
            "warehouse": kwargs.get("warehouse"),
            "qty": kwargs.get("qty"),
            "result": frappe.as_json(kwargs.get("result")),
            "created_at": frappe.utils.now()
        })
        log.insert(ignore_permissions=True)

    @classmethod
    def _create_transaction_log_doctype(cls):
        """
        Create Inventory Transaction Log Doctype if it doesn't exist
        """
        if frappe.db.exists("DocType", "Inventory Transaction Log"):
            return

        doc = frappe.get_doc({
            "doctype": "DocType",
            "name": "Inventory Transaction Log",
            "module": "Stock",
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
                    "fieldname": "transaction_id",
                    "label": "Transaction ID",
                    "fieldtype": "Data",
                    "reqd": 1,
                    "in_list_view": 1,
                    "idx": 1
                },
                {
                    "doctype": "DocField",
                    "fieldname": "operation",
                    "label": "Operation",
                    "fieldtype": "Data",
                    "reqd": 1,
                    "in_list_view": 1,
                    "idx": 2
                },
                {
                    "doctype": "DocField",
                    "fieldname": "item_code",
                    "label": "Item Code",
                    "fieldtype": "Link",
                    "options": "Item",
                    "in_list_view": 1,
                    "idx": 3
                },
                {
                    "doctype": "DocField",
                    "fieldname": "warehouse",
                    "label": "Warehouse",
                    "fieldtype": "Link",
                    "options": "Warehouse",
                    "in_list_view": 1,
                    "idx": 4
                },
                {
                    "doctype": "DocField",
                    "fieldname": "qty",
                    "label": "Quantity",
                    "fieldtype": "Float",
                    "idx": 5
                },
                {
                    "doctype": "DocField",
                    "fieldname": "result",
                    "label": "Result",
                    "fieldtype": "Text",
                    "idx": 6
                },
                {
                    "doctype": "DocField",
                    "fieldname": "created_at",
                    "label": "Created At",
                    "fieldtype": "Datetime",
                    "reqd": 1,
                    "in_list_view": 1,
                    "idx": 7
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

    @classmethod
    def batch_atomic_deduct(
        cls,
        items: List[Dict],
        transaction_id: str = None,
        **kwargs
    ) -> Dict:
        """
        Batch atomic deduction for multiple items in one transaction

        Args:
            items: List of dicts with item_code, warehouse, qty
            transaction_id: Optional transaction ID for idempotency

        Returns:
            Result dict with overall status and individual results
        """
        return frappe.db.transaction(
            cls._batch_atomic_deduct_impl,
            items=items,
            transaction_id=transaction_id,
            **kwargs
        )

    @classmethod
    def _batch_atomic_deduct_impl(
        cls,
        items: List[Dict],
        transaction_id: str = None,
        **kwargs
    ) -> Dict:
        """
        Implementation of batch deduction
        """
        results = []
        success = True
        errors = []

        for item in items:
            try:
                result = cls._atomic_deduct_inventory_impl(
                    item_code=item["item_code"],
                    warehouse=item["warehouse"],
                    qty=item["qty"],
                    transaction_id=None  # Don't use idempotency for individual items in batch
                )
                results.append({
                    "item_code": item["item_code"],
                    "warehouse": item["warehouse"],
                    "result": result
                })
            except Exception as e:
                success = False
                errors.append({
                    "item_code": item["item_code"],
                    "warehouse": item["warehouse"],
                    "error": str(e)
                })

        if not success:
            frappe.throw(
                _("Batch deduction failed. Errors: {0}").format("; ".join([e["error"] for e in errors])),
                title=_("Batch Inventory Error")
            )

        if transaction_id:
            cls._log_transaction(
                transaction_id=transaction_id,
                operation="batch_atomic_deduct",
                items_count=len(items),
                result={"results": results}
            )

        return {
            "status": "success",
            "results": results,
            "items_count": len(items),
            "transaction_id": transaction_id
        }

    @classmethod
    def get_inventory_status(cls, item_code: str, warehouse: str) -> Dict:
        """
        Get current inventory status with version information
        """
        bin_name = frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse})
        if not bin_name:
            return {
                "item_code": item_code,
                "warehouse": warehouse,
                "exists": False
            }

        bin_doc = frappe.get_doc("Bin", bin_name)
        return {
            "item_code": item_code,
            "warehouse": warehouse,
            "exists": True,
            "actual_qty": bin_doc.actual_qty,
            "projected_qty": bin_doc.projected_qty,
            "reserved_qty": bin_doc.reserved_qty,
            "version": bin_doc.get("version", 1)
        }


def extend_bin_class():
    """
    Extend Bin class with optimistic locking
    """
    from erpnext.stock.doctype.bin.bin import Bin

    original_before_save = Bin.before_save

    def before_save(self):
        original_before_save(self)
        # Update version
        if self.get("version") is None:
            self.version = 1
        else:
            self.version = int(self.version) + 1

    Bin.before_save = before_save

    original_onload = Bin.onload

    def onload(self):
        if original_onload:
            original_onload(self)
        self.set_onload('current_version', self.get("version", 1))

    Bin.onload = onload

    def check_version(self, expected_version):
        """
        Check if current version matches expected version
        """
        if self.get("version", 1) != expected_version:
            frappe.throw(
                _("Version mismatch. Expected {0}, got {1}").format(expected_version, self.get("version", 1)),
                title=_("Concurrency Conflict")
            )

    Bin.check_version = check_version


def update_bin_docfield():
    """
    Add version field to Bin DocType
    """
    if frappe.db.exists("DocField", {"parent": "Bin", "fieldname": "version"}):
        return

    frappe.get_doc({
        "doctype": "DocField",
        "parent": "Bin",
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
    frappe.db.commit()


@frappe.whitelist()
def migrate_bin():
    """
    Run migration for Bin extension
    """
    if not frappe.session.user == "Administrator":
        frappe.throw(_("Only Administrator can run this migration"))

    update_bin_docfield()
    extend_bin_class()

    # Initialize version for existing bins
    bins = frappe.get_all("Bin", filters=[["version", "is", "not set"]], limit=1000)
    for bin_doc in bins:
        frappe.db.set_value("Bin", bin_doc.name, "version", 1)

    frappe.db.commit()
    return {"status": "success", "message": _("Bin migration completed successfully")}

