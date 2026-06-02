from frappe.utils import flt


def snapshot_bin(bin_doc):
    return {
        "name": bin_doc.name,
        "item_code": bin_doc.item_code,
        "warehouse": bin_doc.warehouse,
        "actual_qty": flt(bin_doc.actual_qty),
        "reserved_qty": flt(bin_doc.reserved_qty),
        "version": int(bin_doc.get("version") or 0),
    }


def ensure_expected_version(bin_doc, expected_version):
    current_version = int(bin_doc.get("version") or 0)
    if expected_version is None:
        return current_version
    if int(expected_version) != current_version:
        raise ValueError(f"expected version {expected_version}, found {current_version}")
    return current_version


def apply_atomic_deduction(bin_doc, qty):
    current_version = int(bin_doc.get("version") or 0)
    before_qty = flt(bin_doc.actual_qty)
    bin_doc.actual_qty = before_qty - flt(qty)
    bin_doc.version = current_version + 1
    if hasattr(bin_doc, "set_projected_qty"):
        bin_doc.set_projected_qty()
    return {
        "before_qty": before_qty,
        "after_qty": flt(bin_doc.actual_qty),
        "version_before": current_version,
        "version_after": int(bin_doc.version or 0),
    }
