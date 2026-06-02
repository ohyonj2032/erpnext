import operator

import frappe
from frappe import _
from frappe.utils import flt, cint


class InsufficientStockError(frappe.ValidationError):
    pass


class ConcurrentModificationError(frappe.ValidationError):
    pass


def get_bin_with_lock(item_code, warehouse):
    bin_name = frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse})
    if not bin_name:
        return None, None
    bin_data = frappe.db.get_value(
        "Bin",
        bin_name,
        ["name", "actual_qty", "reserved_qty", "projected_qty", "modified"],
        as_dict=True,
    )
    return bin_name, bin_data


def atomic_deduct_stock(item_code, warehouse, qty, voucher_type=None, voucher_no=None):
    bin_name, bin_data = get_bin_with_lock(item_code, warehouse)
    if not bin_data:
        frappe.throw(
            _("No bin record found for item {0} in warehouse {1}").format(item_code, warehouse),
            InsufficientStockError,
        )

    current_actual = flt(bin_data.actual_qty)
    current_reserved = flt(bin_data.reserved_qty)
    new_actual = current_actual - flt(qty)

    allow_negative = cint(frappe.get_single_value("Stock Settings", "allow_negative_stock"))
    is_negative = flt(qty) > current_actual

    if not allow_negative and is_negative:
        frappe.throw(
            _("Insufficient stock for item {0} in warehouse {1}: available={2}, requested={3}").format(
                item_code, warehouse, current_actual, qty
            ),
            InsufficientStockError,
        )

    new_projected = (
        new_actual
        + flt(bin_data.get("ordered_qty", 0))
        + flt(bin_data.get("indented_qty", 0))
        + flt(bin_data.get("planned_qty", 0))
        - current_reserved
    )

    frappe.db.set_value(
        "Bin",
        bin_name,
        {
            "actual_qty": new_actual,
            "projected_qty": new_projected,
        },
        update_modified=True,
    )

    return {
        "bin_name": bin_name,
        "previous_actual_qty": current_actual,
        "new_actual_qty": new_actual,
        "deducted_qty": qty,
        "item_code": item_code,
        "warehouse": warehouse,
    }


def atomic_reserve_stock(item_code, warehouse, qty):
    bin_name, bin_data = get_bin_with_lock(item_code, warehouse)
    if not bin_data:
        frappe.throw(
            _("No bin record found for item {0} in warehouse {1}").format(item_code, warehouse),
            InsufficientStockError,
        )

    current_reserved = flt(bin_data.reserved_qty)
    current_projected = flt(bin_data.projected_qty)
    new_reserved = current_reserved + flt(qty)

    available_for_reserve = current_projected
    if operator.lt(available_for_reserve, flt(qty)):
        frappe.throw(
            _(
                "Insufficient projected stock for reservation: item={0}, warehouse={1}, "
                "projected={2}, requested={3}"
            ).format(item_code, warehouse, current_projected, qty),
            InsufficientStockError,
        )

    new_projected = current_projected - flt(qty)

    frappe.db.set_value(
        "Bin",
        bin_name,
        {
            "reserved_qty": new_reserved,
            "projected_qty": new_projected,
        },
        update_modified=True,
    )

    return {
        "bin_name": bin_name,
        "previous_reserved_qty": current_reserved,
        "new_reserved_qty": new_reserved,
        "reserved_qty": qty,
        "item_code": item_code,
        "warehouse": warehouse,
    }


def check_optimistic_lock(bin_name, expected_modified):
    current_modified = frappe.db.get_value("Bin", bin_name, "modified")
    if str(current_modified) != str(expected_modified):
        frappe.throw(
            _("Concurrent modification detected for Bin {0}").format(bin_name),
            ConcurrentModificationError,
        )
    return True


def atomic_deduct_with_gl(item_code, warehouse, qty, company, voucher_type, voucher_no, cost_center=None):
    result = atomic_deduct_stock(item_code, warehouse, qty, voucher_type, voucher_no)

    valuation_rate = frappe.db.get_value("Bin", result["bin_name"], "valuation_rate") or 0
    stock_value_diff = flt(qty) * flt(valuation_rate)

    if stock_value_diff != 0 and company:
        expense_account = frappe.db.get_value(
            "Company", company, "stock_adjustment_account"
        ) or frappe.db.get_value("Account", {"company": company, "account_type": "Stock Adjustment"}, "name")

        if not expense_account:
            expense_account = frappe.db.get_value(
                "Account", {"company": company, "root_type": "Expense", "is_group": 0}, "name"
            )

        stock_in_hand = frappe.db.get_value(
            "Account",
            {"company": company, "account_type": "Stock", "is_group": 0},
            "name",
        )

        if expense_account and stock_in_hand:
            gl_entries = [
                {
                    "account": expense_account,
                    "debit": stock_value_diff if operator.gt(stock_value_diff, 0) else 0,
                    "credit": abs(stock_value_diff) if operator.lt(stock_value_diff, 0) else 0,
                    "against": stock_in_hand,
                    "cost_center": cost_center,
                    "voucher_type": voucher_type,
                    "voucher_no": voucher_no,
                    "company": company,
                },
                {
                    "account": stock_in_hand,
                    "debit": abs(stock_value_diff) if operator.lt(stock_value_diff, 0) else 0,
                    "credit": stock_value_diff if operator.gt(stock_value_diff, 0) else 0,
                    "against": expense_account,
                    "cost_center": cost_center,
                    "voucher_type": voucher_type,
                    "voucher_no": voucher_no,
                    "company": company,
                },
            ]
            result["gl_entries"] = gl_entries

    return result
