from frappe.utils import flt


class InventoryVersionMismatchError(ValueError):
    pass


def normalize_version(value):
    return int(value or 0)


def snapshot_bin(bin_doc):
    return {
        "name": bin_doc.name,
        "item_code": bin_doc.item_code,
        "warehouse": bin_doc.warehouse,
        "actual_qty": flt(bin_doc.actual_qty),
        "reserved_qty": flt(bin_doc.reserved_qty),
        "available_qty": flt(bin_doc.actual_qty) - flt(bin_doc.reserved_qty),
        "version": normalize_version(bin_doc.get("version")),
    }


def ensure_expected_version(bin_doc, expected_version):
    current_version = normalize_version(bin_doc.get("version"))
    if expected_version is None:
        return current_version
    if normalize_version(expected_version) != current_version:
        raise InventoryVersionMismatchError(
            f"expected version {expected_version}, found {current_version}"
        )
    return current_version


def build_deduction_mutation(bin_doc, qty):
    current_version = normalize_version(bin_doc.get("version"))
    before_qty = flt(bin_doc.actual_qty)
    deducted_qty = flt(qty)
    after_qty = before_qty - deducted_qty
    return {
        "before_qty": before_qty,
        "after_qty": after_qty,
        "deducted_qty": deducted_qty,
        "version_before": current_version,
        "version_after": current_version + 1,
    }


def apply_atomic_deduction(bin_doc, qty):
    mutation = build_deduction_mutation(bin_doc, qty)
    bin_doc.actual_qty = mutation["after_qty"]
    bin_doc.version = mutation["version_after"]
    if hasattr(bin_doc, "set_projected_qty"):
        bin_doc.set_projected_qty()
    return mutation
