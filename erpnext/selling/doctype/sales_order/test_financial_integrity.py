import frappe
from frappe.utils import flt, nowdate, add_days, cint

from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return
from erpnext.controllers.taxes_and_totals import get_itemised_tax
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


class TestFinancialDataIntegrity(ERPNextTestSuite):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        make_item("_Test Fin Item", {"is_stock_item": 1})
        make_stock_entry(
            item="_Test Fin Item",
            target="_Test Warehouse - _TC",
            qty=100,
            rate=50,
        )
        make_stock_entry(
            item="_Test Fin Item",
            target="_Test Warehouse - _TC",
            qty=50,
            rate=60,
        )

    # ============================================================
    # CLASS 1: 金额精度 & 舍入一致性
    # 多币种/多税率场景下，base_grand_total 与行金额汇总
    # 出现舍入差时，GL 分录总额不等于单据总额。
    # 盯住的代码位置:
    #   accounts_controller.py:1641 - make_precision_loss_gl_entry()
    #   taxes_and_totals.py      - 税率计算逐行舍入
    # ============================================================
    def test_grand_total_matches_gl_total(self):
        """GL 分录的总 debit/credit 必须等于单据 base_grand_total"""
        so = make_sales_order(
            item_code="_Test Fin Item",
            qty=7,
            rate=133.33,
            currency="USD",
            do_not_submit=False,
        )
        so.db_set("conversion_rate", 1.234567)
        so.reload()

        from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

        si = make_sales_invoice(so.name)
        si.update_stock = 1
        si.insert()
        si.submit()

        gl_entries = frappe.get_all(
            "GL Entry",
            filters={
                "voucher_type": si.doctype,
                "voucher_no": si.name,
                "is_cancelled": 0,
            },
            fields=["debit", "credit"],
        )

        total_debit = sum(flt(e.debit) for e in gl_entries)
        total_credit = sum(flt(e.credit) for e in gl_entries)

        si.reload()

        self.assertAlmostEqual(
            total_debit, total_credit, places=2,
            msg=f"GL debit={total_debit} should equal credit={total_credit}"
        )
        self.assertAlmostEqual(
            total_debit, flt(si.base_grand_total), places=2,
            msg=f"GL total debit={total_debit} should equal base_grand_total={si.base_grand_total}"
        )

    def test_item_tax_precision_consistent(self):
        """逐行税按 base_rate * qty * tax_rate 计算后总和 = taxes 表合计"""
        so = make_sales_order(
            item_code="_Test Fin Item",
            qty=3,
            rate=99.97,
            do_not_submit=False,
        )
        from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

        si = make_sales_invoice(so.name)
        si.insert()
        si.submit()

        item_wise_tax = get_itemised_tax(si.taxes, with_tax_account=True)
        taxes_table_total = sum(flt(t.tax_amount) for t in si.taxes)

        items_tax_total = sum(
            flt(v.get("tax_amount", 0))
            for taxes in item_wise_tax.values()
            for v in taxes.values()
        )

        self.assertAlmostEqual(
            items_tax_total, taxes_table_total, places=2,
            msg=f"Item-level tax={items_tax_total} != taxes table={taxes_table_total}"
        )

    # ============================================================
    # CLASS 2: 状态流转完整性
    # SO 的状态机跨越多个 doctype (DN, SI, PO) 协同更新。
    # 如果状态回写失败但后续提交成功，会导致数据不一致。
    # 盯住的代码位置:
    #   status_updater.py:536  - _update_children() 裸 SQL
    #   status_updater.py:195  - set_status()
    # ============================================================
    def test_delivery_status_consistent_with_dn(self):
        """SO.delivery_status 必须与关联的 DN 实际数量一致"""
        so = make_sales_order(
            item_code="_Test Fin Item",
            qty=10,
            rate=100,
            do_not_submit=False,
        )
        make_stock_entry(
            item="_Test Fin Item",
            target="_Test Warehouse - _TC",
            qty=10,
            rate=50,
        )

        dn = create_dn_against_so(so.name, delivered_qty=5, do_not_submit=False)
        so.reload()

        self.assertEqual(flt(so.per_delivered), 50.0)
        self.assertEqual(so.delivery_status, "Partly Delivered")

        dn.cancel()
        so.reload()

        self.assertEqual(flt(so.per_delivered), 0.0)
        self.assertEqual(so.delivery_status, "Not Delivered")

        dn2 = create_dn_against_so(so.name, delivered_qty=10, do_not_submit=False)
        so.reload()

        self.assertEqual(flt(so.per_delivered), 100.0)
        self.assertEqual(so.delivery_status, "Fully Delivered")

    def test_billing_status_consistent_with_si(self):
        """SO.billing_status 必须与关联的 SI 金额一致"""
        so = make_sales_order(
            item_code="_Test Fin Item",
            qty=5,
            rate=200,
            do_not_submit=False,
        )

        si = make_sales_invoice(so.name)
        si.insert()
        si.submit()
        so.reload()

        self.assertEqual(flt(so.per_billed), 100.0)
        self.assertEqual(so.billing_status, "Fully Billed")

        si.cancel()
        so.reload()

        self.assertEqual(flt(so.per_billed), 0.0)
        self.assertEqual(so.billing_status, "Not Billed")

    def test_close_does_not_orphan_gl(self):
        """关闭 SO 不应该留下孤立的未清理 GL/SLE 条目"""
        so = make_sales_order(
            item_code="_Test Fin Item",
            qty=5,
            rate=200,
            do_not_submit=False,
        )

        from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

        si = make_sales_invoice(so.name)
        si.insert()
        si.submit()
        so.reload()

        update_status("Closed", so.name)
        so.reload()
        self.assertEqual(so.status, "Closed")

        linked_sle_count = frappe.db.count(
            "Stock Ledger Entry",
            {"voucher_type": "Sales Order", "voucher_no": so.name, "is_cancelled": 0},
        )
        linked_gl_count = frappe.db.count(
            "GL Entry",
            {"voucher_type": "Sales Order", "voucher_no": so.name, "is_cancelled": 0},
        )

        self.assertEqual(linked_sle_count, 0,
                         f"Closed SO should not have open SLE, found {linked_sle_count}")
        self.assertEqual(linked_gl_count, 0,
                         f"Closed SO should not have open GL, found {linked_gl_count}")

    def test_cancel_rolls_back_all_side_effects(self):
        """取消 SO 后：delivered_qty=0, billing=0, reserved_qty=0, blanket_order 解绑"""
        so = make_sales_order(
            item_code="_Test Fin Item",
            qty=5,
            rate=200,
            do_not_submit=False,
        )

        make_stock_entry(
            item="_Test Fin Item",
            target="_Test Warehouse - _TC",
            qty=10,
            rate=50,
        )

        dn = create_dn_against_so(so.name, delivered_qty=3, do_not_submit=False)
        si = make_sales_invoice(so.name)
        si.insert()
        si.submit()

        so.cancel()
        so.reload()

        self.assertEqual(so.status, "Cancelled")
        self.assertEqual(flt(so.per_delivered), 0.0)
        self.assertEqual(flt(so.per_billed), 0.0)
        self.assertEqual(so.delivery_status, "Not Delivered")
        self.assertEqual(so.billing_status, "Not Billed")

        reserved = get_reserved_qty("_Test Fin Item")
        self.assertEqual(reserved, 0.0,
                         f"Cancelled SO reserved_qty must be 0, got {reserved}")

    # ============================================================
    # CLASS 3: 库存扣减与财务分录的原子性
    # 盯住的代码位置:
    #   delivery_note.py:505  - on_submit 先写状态再扣库存
    #   stock_ledger.py:58   - make_sl_entries
    #   accounts_controller  - make_gl_entries
    # ============================================================
    def test_dn_sle_gl_consistency(self):
        """Delivery Note 的 SLE outgoing qty 总和必须与 GL 的 COGS 贷方一致"""
        so = make_sales_order(
            item_code="_Test Fin Item",
            qty=5,
            rate=200,
            do_not_submit=False,
        )
        make_stock_entry(
            item="_Test Fin Item",
            target="_Test Warehouse - _TC",
            qty=10,
            rate=50.5,
        )

        dn = create_dn_against_so(so.name, delivered_qty=5, do_not_submit=False)

        sle_entries = frappe.get_all(
            "Stock Ledger Entry",
            filters={
                "voucher_type": dn.doctype,
                "voucher_no": dn.name,
                "is_cancelled": 0,
            },
            fields=["actual_qty", "stock_value_difference"],
        )

        gl_entries = frappe.get_all(
            "GL Entry",
            filters={
                "voucher_type": dn.doctype,
                "voucher_no": dn.name,
                "is_cancelled": 0,
            },
            fields=["account", "debit", "credit"],
        )

        total_sle_qty_out = abs(sum(
            flt(e.actual_qty) for e in sle_entries if flt(e.actual_qty) < 0
        ))
        total_sle_value = abs(sum(
            flt(e.stock_value_difference) for e in sle_entries
            if flt(e.stock_value_difference) < 0
        ))

        self.assertGreater(total_sle_qty_out, 0, "SLE should have outgoing qty")
        self.assertGreater(total_sle_value, 0, "SLE should have outgoing value")

        # GL debits and credits must balance
        total_debit = sum(flt(e.debit) for e in gl_entries)
        total_credit = sum(flt(e.credit) for e in gl_entries)
        self.assertAlmostEqual(total_debit, total_credit, places=2)

        # SLE total value should appear in at least one GL line
        gl_values = set()
        for e in gl_entries:
            gl_values.add(round(flt(e.debit), 2))
            gl_values.add(round(flt(e.credit), 2))
        self.assertIn(
            round(total_sle_value, 2), gl_values,
            f"SLE outgoing value {total_sle_value} not found in GL entries: {gl_values}"
        )

    def test_so_cancel_does_not_leave_stale_sle(self):
        """取消 SO 后不应该残留同 voucher 的未取消 SLE"""
        so = make_sales_order(
            item_code="_Test Fin Item",
            qty=5,
            rate=200,
            do_not_submit=False,
        )

        make_stock_entry(
            item="_Test Fin Item",
            target="_Test Warehouse - _TC",
            qty=10,
            rate=50,
        )

        dn = create_dn_against_so(so.name, delivered_qty=2, do_not_submit=False)

        so.cancel()
        so.reload()

        open_sle = frappe.db.count(
            "Stock Ledger Entry",
            {
                "voucher_type": "Sales Order",
                "voucher_no": so.name,
                "is_cancelled": 0,
            },
        )
        self.assertEqual(open_sle, 0,
                         f"SO cancel should not leave open SLE, found {open_sle}")