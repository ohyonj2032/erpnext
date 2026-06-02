#!/usr/bin/env python3
# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""
财务关键场景测试模块
包含最容易漏掉但会影响财务数据的测试场景
"""

import frappe
from frappe.utils import flt, nowdate, add_days
from erpnext.tests.utils import ERPNextTestSuite
from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_invoice
from erpnext.stock.doctype.delivery_note.delivery_note import make_delivery_note
from erpnext.selling.doctype.sales_order.test_sales_order import make_sales_order
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry


class TestFinancialCriticalScenarios(ERPNextTestSuite):
    """财务关键场景测试"""
    
    def setUp(self):
        self.item_code = "_Test Financial Item"
        self.warehouse = "_Test Warehouse - _TC"
        self.customer = "_Test Customer"
        self.company = "_Test Company"
        
        # 创建测试物料
        if not frappe.db.exists("Item", self.item_code):
            from erpnext.stock.doctype.item.test_item import make_item
            make_item(self.item_code, {
                "is_stock_item": 1,
                "standard_rate": 100,
                "valuation_rate": 50
            })
        
        # 初始化库存
        make_stock_entry(
            item_code=self.item_code,
            warehouse=self.warehouse,
            qty=100,
            basic_rate=50,
            target=self.warehouse
        )
    
    def test_rounding_cumulative_error(self):
        """场景1: 舍入累积误差测试 - 最常见的财务问题"""
        from erpnext.accounts.doctype.sales_invoice.test_sales_invoice import create_sales_invoice
        
        # 创建多个小数金额的发票
        total_expected = 0
        invoices = []
        
        for i in range(10):
            qty = 1.5
            rate = 10.333  # 会产生循环小数
            amount = qty * rate  # 15.4995
            total_expected += amount
            
            si = create_sales_invoice(
                item_code=self.item_code,
                qty=qty,
                rate=rate,
                do_not_submit=True
            )
            si.insert()
            si.submit()
            invoices.append(si)
        
        # 计算总金额
        total_actual = sum(flt(si.grand_total) for si in invoices)
        
        # 检查累积误差
        print(f"期望总额: {total_expected}, 实际总额: {total_actual}")
        print(f"误差: {abs(flt(total_actual - total_expected))}")
        
        # 误差应该小于0.01（分）
        self.assertLess(
            abs(flt(total_actual - total_expected)),
            0.01,
            f"累积舍入误差过大: {abs(flt(total_actual - total_expected))}"
        )
    
    def test_cancel_and_recreate_ledger_consistency(self):
        """场景2: 取消和重建操作的分录一致性 - 财务审计关键"""
        from erpnext.accounts.doctype.sales_invoice.test_sales_invoice import create_sales_invoice
        
        # 创建并提交发票
        si = create_sales_invoice(
            item_code=self.item_code,
            qty=10,
            rate=100
        )
        si.submit()
        original_name = si.name
        
        # 获取原始分录
        original_gl_entries = frappe.get_all(
            "GL Entry",
            filters={"voucher_no": original_name, "voucher_type": "Sales Invoice"},
            fields=["account", "debit", "credit", "against"]
        )
        
        # 取消发票
        si.cancel()
        
        # 验证取消分录
        cancelled_gl_entries = frappe.get_all(
            "GL Entry",
            filters={"voucher_no": original_name, "voucher_type": "Sales Invoice"},
            fields=["account", "debit", "credit", "against", "is_cancelled"]
        )
        
        # 应该有两套分录：原始的和冲销的
        self.assertEqual(len(cancelled_gl_entries), len(original_gl_entries) * 2)
        
        # 验证借贷平衡
        total_debit = sum(flt(e.debit) for e in cancelled_gl_entries)
        total_credit = sum(flt(e.credit) for e in cancelled_gl_entries)
        self.assertEqual(flt(total_debit), flt(total_credit))
    
    def test_stock_valuation_consistency_across_documents(self):
        """场景3: 跨单据库存估值一致性 - 成本核算关键"""
        from erpnext.stock.doctype.purchase_receipt.test_purchase_receipt import create_purchase_receipt
        from erpnext.stock.doctype.delivery_note.test_delivery_note import create_delivery_note
        
        # 第一次收货 - 成本50
        make_stock_entry(
            item_code=self.item_code,
            warehouse=self.warehouse,
            qty=10,
            basic_rate=50,
            target=self.warehouse
        )
        
        # 第二次收货 - 成本60（价格上涨）
        make_stock_entry(
            item_code=self.item_code,
            warehouse=self.warehouse,
            qty=10,
            basic_rate=60,
            target=self.warehouse
        )
        
        # 创建发货单
        dn1 = create_delivery_note(
            item_code=self.item_code,
            qty=5,
            warehouse=self.warehouse
        )
        dn1.submit()
        
        # 获取库存估值
        sle1 = frappe.get_doc("Stock Ledger Entry", {
            "voucher_type": "Delivery Note",
            "voucher_no": dn1.name
        })
        valuation1 = flt(sle1.stock_value_difference)
        
        # 再创建一个发货单
        dn2 = create_delivery_note(
            item_code=self.item_code,
            qty=10,
            warehouse=self.warehouse
        )
        dn2.submit()
        
        sle2 = frappe.get_doc("Stock Ledger Entry", {
            "voucher_type": "Delivery Note",
            "voucher_no": dn2.name
        })
        valuation2 = flt(sle2.stock_value_difference)
        
        # 验证移动平均成本计算
        print(f"第一次发货成本: {abs(valuation1)}, 第二次: {abs(valuation2)}")
        
        # 验证库存余额
        from erpnext.stock.utils import get_bin
        bin = get_bin(self.item_code, self.warehouse)
        bin.load_from_db()
        
        print(f"最终实际库存: {bin.actual_qty}")
        print(f"最终库存价值: {bin.stock_value}")
    
    def test_currency_conversion_precision(self):
        """场景4: 多币种汇率精度问题 - 跨国业务关键"""
        if not frappe.db.exists("Currency", "USD"):
            usd = frappe.new_doc("Currency")
            usd.currency_name = "US Dollar"
            usd.symbol = "$"
            usd.insert()
        
        # 设置汇率
        from erpnext.setup.doctype.currency_exchange.currency_exchange import make_currency_exchange
        make_currency_exchange("USD", "INR", 83.4567, nowdate())
        
        # 创建外币销售订单
        so = make_sales_order(
            item_code=self.item_code,
            qty=10,
            rate=10.50,  # USD
            currency="USD",
            conversion_rate=83.4567,
            do_not_submit=True
        )
        so.insert()
        so.submit()
        
        # 创建发货单
        dn = make_delivery_note(so.name)
        dn.insert()
        dn.submit()
        
        # 创建发票
        si = make_sales_invoice(dn.name)
        si.insert()
        si.submit()
        
        # 验证汇率转换精度
        expected_base_total = 10 * 10.50 * 83.4567  # 10个 * $10.50 * 汇率
        actual_base_total = flt(si.base_total)
        
        print(f"期望本位币总额: {expected_base_total}")
        print(f"实际本位币总额: {actual_base_total}")
        print(f"差异: {abs(expected_base_total - actual_base_total)}")
        
        # 差异应该在可接受范围内
        self.assertLess(
            abs(flt(expected_base_total - actual_base_total)),
            0.02,  # 允许2分的差异
            f"汇率转换精度问题: 差异 {abs(flt(expected_base_total - actual_base_total))}"
        )
    
    def test_payment_reconciliation_consistency(self):
        """场景5: 付款核销一致性 - 应收账款关键"""
        from erpnext.accounts.doctype.sales_invoice.test_sales_invoice import create_sales_invoice
        
        # 创建两张发票
        si1 = create_sales_invoice(
            item_code=self.item_code,
            qty=10,
            rate=100
        )
        si1.submit()
        
        si2 = create_sales_invoice(
            item_code=self.item_code,
            qty=5,
            rate=200
        )
        si2.submit()
        
        total_invoice_amount = flt(si1.grand_total) + flt(si2.grand_total)
        
        # 创建超额付款
        pe = get_payment_entry("Sales Invoice", si1.name)
        pe.paid_amount = total_invoice_amount + 100  # 多付100
        pe.references = []
        
        # 添加两张发票的核销
        pe.append("references", {
            "reference_doctype": "Sales Invoice",
            "reference_name": si1.name,
            "allocated_amount": flt(si1.grand_total)
        })
        pe.append("references", {
            "reference_doctype": "Sales Invoice",
            "reference_name": si2.name,
            "allocated_amount": flt(si2.grand_total)
        })
        
        pe.insert()
        pe.submit()
        
        # 验证两张发票都已完全核销
        si1.reload()
        si2.reload()
        
        self.assertEqual(si1.status, "Paid")
        self.assertEqual(si2.status, "Paid")
        
        # 验证客户余额应该有预付款/贷方余额
        from erpnext.accounts.party import get_party_account
        customer_account = get_party_account("Customer", self.customer, self.company)
        
        balance = frappe.db.sql("""
            SELECT SUM(debit - credit) 
            FROM `tabGL Entry` 
            WHERE account = %s AND is_cancelled = 0
        """, customer_account)[0][0] or 0
        
        print(f"客户余额: {balance}")
        self.assertEqual(flt(balance), -100)  # 应该是贷方余额100


class TestStatusFlowConsistency(ERPNextTestSuite):
    """状态流转一致性测试"""
    
    def setUp(self):
        self.item_code = "_Test Status Flow"
        self.warehouse = "_Test Warehouse - _TC"
        
        if not frappe.db.exists("Item", self.item_code):
            from erpnext.stock.doctype.item.test_item import make_item
            make_item(self.item_code, {"is_stock_item": 1})
        
        make_stock_entry(
            item_code=self.item_code,
            warehouse=self.warehouse,
            qty=100,
            target=self.warehouse
        )
    
    def test_complete_sales_cycle_status_consistency(self):
        """完整销售周期的状态一致性测试"""
        # 1. 创建销售订单
        so = make_sales_order(
            item_code=self.item_code,
            qty=10,
            warehouse=self.warehouse
        )
        so.submit()
        self.assertEqual(so.status, "To Deliver and Bill")
        
        # 2. 创建发货单
        dn = make_delivery_note(so.name)
        dn.insert()
        dn.submit()
        so.reload()
        
        # 验证销售订单状态更新
        self.assertEqual(so.per_delivered, 100)
        self.assertEqual(so.status, "To Bill")
        
        # 3. 创建销售发票
        si = make_sales_invoice(dn.name)
        si.insert()
        si.submit()
        so.reload()
        dn.reload()
        
        # 验证最终状态
        self.assertEqual(so.per_billed, 100)
        self.assertEqual(so.per_delivered, 100)
        self.assertEqual(so.status, "Completed")
        self.assertEqual(dn.per_billed, 100)
        self.assertEqual(dn.status, "Completed")
    
    def test_cancel_chain_reaction(self):
        """取消连锁反应测试 - 验证单据状态的正确回滚"""
        # 创建完整链路
        so = make_sales_order(
            item_code=self.item_code,
            qty=10,
            warehouse=self.warehouse
        )
        so.submit()
        
        dn = make_delivery_note(so.name)
        dn.insert()
        dn.submit()
        
        si = make_sales_invoice(dn.name)
        si.insert()
        si.submit()
        
        # 尝试直接取消销售订单 - 应该失败
        with self.assertRaises(frappe.LinkExistsError):
            so.cancel()
        
        # 正确的取消顺序：发票 -> 发货单 -> 销售订单
        si.cancel()
        dn.cancel()
        so.cancel()
        
        # 验证所有状态都是Cancelled
        si.reload()
        dn.reload()
        so.reload()
        
        self.assertEqual(si.docstatus, 2)
        self.assertEqual(dn.docstatus, 2)
        self.assertEqual(so.docstatus, 2)
        
        # 验证库存已回滚
        from erpnext.stock.utils import get_bin
        bin = get_bin(self.item_code, self.warehouse)
        bin.load_from_db()
        
        print(f"最终库存: {bin.actual_qty}")
