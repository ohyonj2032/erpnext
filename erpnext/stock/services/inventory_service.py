# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import json
import hashlib

import frappe
from frappe import _
from frappe.utils import flt, now

from erpnext.models.inventory import (
    InventoryVersionMismatchError,
    apply_atomic_deduction,
    ensure_expected_version,
    snapshot_bin,
)


class InventoryOperationError(frappe.ValidationError):
    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result or {}


class InventoryService:
    IDEMPOTENCY_PREFIX = "inventory_idempotency_"
    _bootstrapped = False

    @classmethod
    def bootstrap(cls):
        if cls._bootstrapped:
            return
        try:
            update_bin_docfield()
            extend_bin_class()
            cls._create_transaction_log_doctype()
        except Exception:
            frappe.clear_messages()
        cls._bootstrapped = True

    @classmethod
    def _idempotency_cache_key(cls, transaction_id):
        return f"{cls.IDEMPOTENCY_PREFIX}{transaction_id}"

    @classmethod
    def _build_transaction_id(cls, reference_doctype, reference_name, deductions, accounting_entries):
        payload = json.dumps(
            {
                "reference_doctype": reference_doctype,
                "reference_name": reference_name,
                "deductions": deductions,
                "accounting_entries": accounting_entries or [],
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    @classmethod
    def _get_cached_transaction(cls, transaction_id):
        cached = frappe.cache().get(cls._idempotency_cache_key(transaction_id))
        if cached:
            result = dict(cached)
            result["is_idempotent"] = True
            return result

        if not frappe.db.exists("DocType", "Inventory Transaction Log"):
            return None

        log_result = frappe.db.get_value(
            "Inventory Transaction Log",
            {"transaction_id": transaction_id},
            ["status", "result"],
            as_dict=True,
        )
        if not log_result or not log_result.result:
            return None

        result = json.loads(log_result.result)
        result["is_idempotent"] = True
        frappe.cache().set(cls._idempotency_cache_key(transaction_id), result, expire=3600)
        return result

    @classmethod
    def _build_reconciliation_snapshot(cls, deduction_results, accounting_entries=None):
        deduction_results = deduction_results or []
        accounting_entries = accounting_entries or []
        return {
            "inventory_qty": sum(flt(row.get("deducted_qty") or row.get("qty") or 0) for row in deduction_results),
            "inventory_rows": len(deduction_results),
            "ledger_amount": sum(flt(entry.get("ledger_amount") or 0) for entry in accounting_entries),
            "ledger_rows": len(accounting_entries),
        }

    @classmethod
    def atomic_deduct_inventory(
        cls,
        item_code,
        warehouse,
        qty,
        reservation_id=None,
        transaction_id=None,
        manage_transaction=True,
        **kwargs,
    ):
        result = cls.apply_inventory_and_accounting(
            reference_doctype=kwargs.get("reference_doctype", "Inventory"),
            reference_name=kwargs.get("reference_name") or reservation_id or transaction_id or item_code,
            deductions=[{"item_code": item_code, "warehouse": warehouse, "qty": qty}],
            accounting_entries=kwargs.get("accounting_entries") or [],
            transaction_id=transaction_id,
            manage_transaction=manage_transaction,
            simulate_failure=kwargs.get("simulate_failure", False),
        )
        if result.get("deductions"):
            return result["deductions"][0]
        return result

    @classmethod
    def apply_inventory_and_accounting(
        cls,
        reference_doctype,
        reference_name,
        deductions,
        accounting_entries=None,
        transaction_id=None,
        manage_transaction=True,
        simulate_failure=False,
    ):
        cls.bootstrap()
        transaction_id = transaction_id or cls._build_transaction_id(
            reference_doctype,
            reference_name,
            deductions,
            accounting_entries,
        )

        cached = cls._get_cached_transaction(transaction_id)
        if cached:
            return cached

        started_transaction = False
        try:
            if manage_transaction:
                frappe.db.begin()
                started_transaction = True

            deduction_results = [cls._deduct_one(row) for row in deductions]

            if simulate_failure:
                raise InventoryOperationError(
                    _("Simulated inventory/accounting rollback for {0} {1}").format(
                        reference_doctype, reference_name
                    )
                )

            result = {
                "status": "success",
                "reference_doctype": reference_doctype,
                "reference_name": reference_name,
                "transaction_id": transaction_id,
                "deductions": deduction_results,
                "accounting_entries": accounting_entries or [],
                "reconciliation": cls._build_reconciliation_snapshot(
                    deduction_results,
                    accounting_entries,
                ),
                "rolled_back": False,
                "timestamp": now(),
            }
            cls._log_transaction(
                transaction_id=transaction_id,
                reference_doctype=reference_doctype,
                reference_name=reference_name,
                status="success",
                result=result,
            )
            if started_transaction:
                frappe.db.commit()
            frappe.cache().set(cls._idempotency_cache_key(transaction_id), result, expire=3600)
            return result
        except Exception as exc:
            if started_transaction:
                frappe.db.rollback()
            failure = {
                "status": "failed",
                "reference_doctype": reference_doctype,
                "reference_name": reference_name,
                "transaction_id": transaction_id,
                "deductions": deductions,
                "accounting_entries": accounting_entries or [],
                "reconciliation": cls._build_reconciliation_snapshot(deductions, accounting_entries),
                "rolled_back": True,
                "error": str(exc),
                "timestamp": now(),
            }
            if manage_transaction:
                cls._record_failed_transaction(
                    transaction_id=transaction_id,
                    reference_doctype=reference_doctype,
                    reference_name=reference_name,
                    result=failure,
                )
                frappe.cache().set(cls._idempotency_cache_key(transaction_id), failure, expire=3600)
            raise InventoryOperationError(str(exc), result=failure)

    @classmethod
    def _deduct_one(cls, row):
        item_code = row["item_code"]
        warehouse = row["warehouse"]
        qty = flt(row.get("qty"))
        expected_version = row.get("expected_version")

        bin_name = frappe.db.get_value(
            "Bin",
            {"item_code": item_code, "warehouse": warehouse},
            "name",
            for_update=True,
        )
        if not bin_name:
            raise InventoryOperationError(
                _("No stock balance found for Item {0} in Warehouse {1}").format(item_code, warehouse)
            )

        bin_doc = frappe.get_doc("Bin", bin_name)
        current_version = int(bin_doc.get("version") or 0)
        try:
            ensure_expected_version(bin_doc, expected_version)
        except InventoryVersionMismatchError:
            raise InventoryOperationError(
                _("Concurrent modification detected for Bin {0}. Expected version {1}, found {2}").format(
                    bin_name, expected_version, current_version
                )
            )

        available_qty = flt(bin_doc.actual_qty) - flt(bin_doc.reserved_qty)
        if available_qty < qty:
            raise InventoryOperationError(
                _("Insufficient stock. Available: {0}, Required: {1}").format(available_qty, qty)
            )

        mutation = apply_atomic_deduction(bin_doc, qty)
        bin_doc.save(ignore_permissions=True)
        snapshot = snapshot_bin(bin_doc)

        return {
            "status": "success",
            "item_code": item_code,
            "warehouse": warehouse,
            "deducted_qty": qty,
            **mutation,
            "bin_snapshot": snapshot,
        }

    @classmethod
    def _log_transaction(cls, transaction_id, reference_doctype, reference_name, status, result):
        if not frappe.db.exists("DocType", "Inventory Transaction Log"):
            return

        log_name = frappe.db.get_value(
            "Inventory Transaction Log",
            {"transaction_id": transaction_id},
            "name",
        )
        payload = {
            "transaction_id": transaction_id,
            "reference_doctype": reference_doctype,
            "reference_name": reference_name,
            "status": status,
            "result": frappe.as_json(result),
            "created_at": now(),
        }
        if log_name:
            frappe.db.set_value(
                "Inventory Transaction Log",
                log_name,
                {
                    "status": status,
                    "result": payload["result"],
                    "created_at": payload["created_at"],
                },
                update_modified=False,
            )
        else:
            frappe.get_doc({"doctype": "Inventory Transaction Log", **payload}).insert(ignore_permissions=True)

    @classmethod
    def _record_failed_transaction(cls, transaction_id, reference_doctype, reference_name, result):
        try:
            cls._log_transaction(
                transaction_id=transaction_id,
                reference_doctype=reference_doctype,
                reference_name=reference_name,
                status="failed",
                result=result,
            )
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"Failed inventory rollback log for {transaction_id}")

    @classmethod
    def _create_transaction_log_doctype(cls):
        if frappe.db.exists("DocType", "Inventory Transaction Log"):
            return

        frappe.get_doc(
            {
                "doctype": "DocType",
                "name": "Inventory Transaction Log",
                "module": "Stock",
                "custom": 1,
                "is_submittable": 0,
                "istable": 0,
                "editable_grid": 1,
                "track_changes": 1,
                "fields": [
                    {
                        "doctype": "DocField",
                        "fieldname": "transaction_id",
                        "label": "Transaction ID",
                        "fieldtype": "Data",
                        "reqd": 1,
                        "in_list_view": 1,
                    },
                    {
                        "doctype": "DocField",
                        "fieldname": "reference_doctype",
                        "label": "Reference Doctype",
                        "fieldtype": "Link",
                        "options": "DocType",
                    },
                    {
                        "doctype": "DocField",
                        "fieldname": "reference_name",
                        "label": "Reference Name",
                        "fieldtype": "Dynamic Link",
                        "options": "reference_doctype",
                    },
                    {
                        "doctype": "DocField",
                        "fieldname": "status",
                        "label": "Status",
                        "fieldtype": "Data",
                        "in_list_view": 1,
                    },
                    {
                        "doctype": "DocField",
                        "fieldname": "result",
                        "label": "Result",
                        "fieldtype": "Long Text",
                    },
                    {
                        "doctype": "DocField",
                        "fieldname": "created_at",
                        "label": "Created At",
                        "fieldtype": "Datetime",
                        "in_list_view": 1,
                    },
                ],
                "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1}],
            }
        ).insert(ignore_permissions=True)
        frappe.db.commit()

    @classmethod
    def batch_atomic_deduct(cls, items, transaction_id=None, **kwargs):
        return cls.apply_inventory_and_accounting(
            reference_doctype=kwargs.get("reference_doctype", "Inventory Batch"),
            reference_name=kwargs.get("reference_name") or transaction_id or "batch",
            deductions=items,
            accounting_entries=kwargs.get("accounting_entries") or [],
            transaction_id=transaction_id,
            manage_transaction=kwargs.get("manage_transaction", True),
            simulate_failure=kwargs.get("simulate_failure", False),
        )

    @classmethod
    def get_inventory_status(cls, item_code, warehouse):
        cls.bootstrap()
        bin_name = frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "name")
        if not bin_name:
            return {"item_code": item_code, "warehouse": warehouse, "exists": False}

        bin_doc = frappe.get_doc("Bin", bin_name)
        return {
            "item_code": item_code,
            "warehouse": warehouse,
            "exists": True,
            "actual_qty": flt(bin_doc.actual_qty),
            "projected_qty": flt(bin_doc.projected_qty),
            "reserved_qty": flt(bin_doc.reserved_qty),
            "version": int(bin_doc.get("version") or 0),
        }


def extend_bin_class():
    from erpnext.stock.doctype.bin.bin import Bin

    if getattr(Bin, "_inventory_race_extension_installed", False):
        return

    original_before_save = getattr(Bin, "before_save", None)
    original_onload = getattr(Bin, "onload", None)

    def before_save(self):
        if original_before_save:
            original_before_save(self)
        if self.get("version") is None:
            self.version = 0

    def onload(self):
        if original_onload:
            original_onload(self)
        self.set_onload("current_version", int(self.get("version") or 0))

    def check_version(self, expected_version):
        if int(self.get("version") or 0) != int(expected_version):
            raise InventoryOperationError(
                _("Version mismatch. Expected {0}, found {1}").format(expected_version, self.get("version") or 0)
            )

    Bin.before_save = before_save
    Bin.onload = onload
    Bin.check_version = check_version
    Bin._inventory_race_extension_installed = True


def update_bin_docfield():
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    create_custom_fields(
        {
            "Bin": [
                {
                    "fieldname": "version",
                    "label": "Version",
                    "fieldtype": "Int",
                    "default": 0,
                    "read_only": 1,
                    "hidden": 1,
                    "insert_after": "reserved_qty",
                }
            ]
        },
        update=True,
    )
    frappe.clear_cache(doctype="Bin")


@frappe.whitelist()
def migrate_bin():
    if frappe.session.user != "Administrator":
        frappe.throw(_("Only Administrator can run this migration"))

    update_bin_docfield()
    extend_bin_class()
    InventoryService._create_transaction_log_doctype()

    for bin_name in frappe.get_all("Bin", pluck="name"):
        version = frappe.db.get_value("Bin", bin_name, "version")
        if version is None:
            frappe.db.set_value("Bin", bin_name, "version", 0, update_modified=False)

    frappe.db.commit()
    return {"status": "success", "message": _("Bin migration completed successfully")}
