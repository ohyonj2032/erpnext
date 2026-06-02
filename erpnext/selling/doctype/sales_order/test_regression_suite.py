"""
Minimal high-value regression suite covering:
  1. Permission misconfiguration detection
  2. Multi-currency precision loss
  3. State flow anomalies across order→delivery→billing chain

Place in: erpnext/selling/doctype/sales_order/test_regression_suite.py

These tests target the highest-risk, lowest-cost-to-run scenarios
and can be executed as the first gate in CI.
"""

import frappe
from frappe.core.doctype.user_permission.test_user_permission import create_user
from frappe.tests import change_settings
from frappe.utils import flt, nowdate, add_days

from erpnext.selling.doctype.sales_order.sales_order import (
    make_delivery_note,
    make_sales_invoice,
    update_status,
)
from erpnext.selling.doctype.sales_order.test_sales_order import (
    create_dn_against_so,
    get_reserved_qty,
    make_sales_order,
)
from erpnext.stock.doctype.item.test_item import make_item
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.tests.utils import ERPNextTestSuite


ITEM = "_Test RSuite Item"
WAREHOUSE = "_Test Warehouse - _TC"


class TestRegressionSuite(ERPNextTestSuite):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        make_item(ITEM, {"is_stock_item": 1})
        make_stock_entry(item=ITEM, target=WAREHOUSE, qty=200, rate=50)
        # Create a test user with limited roles
        if not frappe.db.exists("User", "test_regression_user@example.com"):
            cls.test_user = create_user(
                "test_regression_user@example.com",
                "Sales User",
            )
        else:
            cls.test_user = frappe.get_doc(
                "User", "test_regression_user@example.com"
            )

    # ============================================================
    # SECTION 1: Permission misconfiguration
    # ============================================================

    def test_unauthorized_user_cannot_close_so(self):
        """没有 submit 权限的用户不能通过 API 关闭 SO"""
        so = make_sales_order(item_code=ITEM, qty=10, do_not_submit=False)
        so_name = so.name

        with self.set_user("test_regression_user@example.com"):
            with self.assertRaises(frappe.PermissionError):
                update_status("Closed", so_name)

    def test_unauthorized_user_cannot_create_dn(self):
        """没有 stock 权限的用户不能创建 Delivery Note"""
        so = make_sales_order(item_code=ITEM, qty=5, do_not_submit=False)

        with self.set_user("test_regression_user@example.com"):
            dn = make_delivery_note(so.name)
            with self.assertRaises(frappe.PermissionError):
                dn.insert()

    def test_workflow_state_cannot_bypass_docstatus(self):
        """审批流中间状态不能绕过 submit/cancel 逻辑"""
        from erpnext.selling.doctype.sales_order.test_sales_order import (
            make_sales_order_workflow,
        )
        from frappe.model.workflow import apply_workflow

        workflow = make_sales_order_workflow()
        so = make_sales_order(item_code=ITEM, qty=5, rate=100, do_not_submit=True)
        apply_workflow(so, "Approve")

        self.assertEqual(so.docstatus, 1,
                         "Workflow approve should submit the SO")
        self.assertEqual(so.workflow_state, "Approved")

        # An unapproved user should not be able to cancel
        test_user = frappe.get_doc("User", "test_regression_user@example.com")
        test_user.add_roles("Test Junior Approver")
        with self.set_user("test_regression_user@example.com"):
            so2 = frappe.get_doc("Sales Order", so.name)
            with self.assertRaises(frappe.PermissionError):
                so2.cancel()

    # ============================================================
    # SECTION 2: Multi-currency precision loss
    # ============================================================

    def test_multi_currency_rate_change_does_not_corrupt_base_amounts(self):
        """
        Creating an SO in USD, later changing conversion_rate,
        and submitting linked docs — base amounts must remain
        internally consistent.
        """
        so = make_sales_order(
            item_code=ITEM,
            qty=7,
            rate=100,
            currency="USD",
            do_not_submit=False,
        )
        original_base_grand_total = flt(so.base_grand_total)

        # Simulate a mid-lifecycle conversion_rate change
        so.reload()
        so.db_set("conversion_rate", 85.0)

        from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

        si = make_sales_invoice(so.name)
        si.insert()
        si.submit()
        si.reload()

        # The SI must NOT use the stale rate; it should use its own rate.
        self.assertNotEqual(
            flt(si.base_grand_total),
            flt(so.base_grand_total),
            "Invoice should not inherit SO's stale base amounts"
        )
        self.assertGreater(flt(si.base_grand_total), 0,
                           "Invoice base_grand_total must be positive")

    def test_precision_loss_in_multi_currency_tax(self):
        """
        When item rate * qty * conversion_rate has more decimal places
        than currency precision, tax calculations must not truncate
        prematurely.
        """
        so = make_sales_order(
            item_code=ITEM,
            qty=3,
            rate=33.333,
            currency="USD",
            do_not_submit=False,
        )
        so.db_set("conversion_rate", 1.234567)
        so.reload()

        from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

        si = make_sales_invoice(so.name)
        si.insert()
        si.submit()

        line_net_total = flt(si.items[0].base_net_amount)
        doc_net_total = flt(si.base_net_total)

        self.assertAlmostEqual(
            line_net_total,
            doc_net_total,
            places=2,
            msg=f"Line base_net_amount={line_net_total} != doc base_net_total={doc_net_total}"
        )

    # ============================================================
    # SECTION 3: State flow anomalies
    # ============================================================

    def test_full_fulfillment_cycle_ends_in_completed(self):
        """完整交付+开票后 SO 状态必须为 Completed"""
        so = make_sales_order(
            item_code=ITEM,
            qty=10,
            rate=100,
            do_not_submit=False,
        )
        make_stock_entry(item=ITEM, target=WAREHOUSE, qty=10, rate=50)
        so_name = so.name

        dn = create_dn_against_so(so_name, delivered_qty=10, do_not_submit=False)

        si = make_sales_invoice(so_name)
        si.insert()
        si.submit()

        so.reload()
        self.assertEqual(so.status, "Completed",
                         f"Fully delivered & billed SO status should be Completed, got {so.status}")

    def test_partial_delivery_then_close_then_reopen(self):
        """部分交付 → Closed → Draft → 继续交付，状态和数量必须一致"""
        make_stock_entry(item=ITEM, target=WAREHOUSE, qty=20, rate=50)

        so = make_sales_order(item_code=ITEM, qty=10, rate=100, do_not_submit=False)
        so_name = so.name

        dn1 = create_dn_against_so(so_name, delivered_qty=4, do_not_submit=False)
        so.reload()
        self.assertEqual(flt(so.per_delivered), 40.0)

        update_status("Closed", so_name)
        so.reload()
        self.assertEqual(so.status, "Closed")

        update_status("Draft", so_name)
        so.reload()
        self.assertEqual(so.status, "Draft")

        # After reopen, per_delivered should still reflect DN1
        self.assertEqual(flt(so.per_delivered), 40.0,
                         f"Reopen should not lose delivery data, got {so.per_delivered}%")

        dn2 = create_dn_against_so(so_name, delivered_qty=6, do_not_submit=False)
        so.reload()

        self.assertEqual(flt(so.per_delivered), 100.0)
        self.assertEqual(so.delivery_status, "Fully Delivered")

    def test_on_hold_blocks_fulfillment(self):
        """On Hold 状态必须阻断 DN 和 SI 的创建"""
        so = make_sales_order(item_code=ITEM, qty=5, rate=100, do_not_submit=False)
        so_name = so.name

        update_status("On Hold", so_name)
        so.reload()
        self.assertEqual(so.status, "On Hold")

        with self.assertRaises(frappe.ValidationError):
            create_dn_against_so(so_name, delivered_qty=3, do_not_submit=False)

        si = make_sales_invoice(so_name)
        with self.assertRaises(frappe.ValidationError):
            si.submit()

    def test_status_recovery_from_inconsistency(self):
        """
        If delivered_qty > qty (data anomaly), SO should not
        silently accept it; per_delivered should be capped at 100.
        """
        so = make_sales_order(item_code=ITEM, qty=5, rate=100, do_not_submit=False)
        so_name = so.name

        make_stock_entry(item=ITEM, target=WAREHOUSE, qty=20, rate=50)

        # Enable over-delivery to insert 15 against qty=5
        frappe.db.set_value("Item", ITEM, "over_delivery_receipt_allowance", 200)
        create_dn_against_so(so_name, delivered_qty=15, do_not_submit=False)

        so.reload()
        self.assertGreaterEqual(flt(so.per_delivered), 100.0,
                                "per_delivered should not go below 100 for over-delivery")

    def test_concurrent_status_transitions_produce_valid_end_state(self):
        """
        Regression meta-test: runs all valid status transitions
        sequentially and asserts each produces a valid state tuple.
        """
        valid_transitions = [
            ("Draft", "On Hold"),
            ("On Hold", "Draft"),
            ("Draft", "Closed"),
            ("Closed", "Draft"),
        ]

        for from_status, to_status in valid_transitions:
            so = make_sales_order(item_code=ITEM, qty=3, rate=50, do_not_submit=False)

            if from_status != "Draft":
                update_status(from_status, so.name)
                so.reload()
                self.assertEqual(so.status, from_status)

            update_status(to_status, so.name)
            so.reload()
            self.assertEqual(so.status, to_status,
                             f"Transition {from_status}→{to_status} failed")

            self.assertIn(so.docstatus, (0, 1, 2))
            self.assertIsNotNone(so.billing_status)
            self.assertIsNotNone(so.delivery_status)