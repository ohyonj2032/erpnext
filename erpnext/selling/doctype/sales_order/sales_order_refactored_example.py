# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt
#
# 重构后的 Sales Order 示例 - 展示如何使用新的编排器
# 【注意】这是一个示例文件，不要直接替换原文件！

import frappe
from frappe import _
from erpnext.controllers.selling_controller import SellingController


class SalesOrder(SellingController):
    """
    重构后的 Sales Order 类
    将复杂的提交流程委托给编排器
    """

    def on_submit(self):
        """
        提交方法 - 使用新的编排器
        """
        # 检查是否启用新编排器
        use_new_orchestrator = self._should_use_new_orchestrator()

        if use_new_orchestrator:
            self._on_submit_new()
        else:
            self._on_submit_legacy()

    def _on_submit_new(self):
        """
        新的提交流程 - 使用编排器
        """
        try:
            from erpnext.selling.services import SalesOrderOrchestrator

            orchestrator = SalesOrderOrchestrator(self)
            orchestrator.submit()

        except Exception as e:
            frappe.log_error(f"New orchestrator failed, falling back to legacy: {str(e)}")
            self._on_submit_legacy()

    def _on_submit_legacy(self):
        """
        原有的提交流程 - 保留作为降级方案
        """
        super().update_prevdoc_status()
        self.check_credit_limit()
        self.update_reserved_qty()
        self.delete_removed_delivery_schedule_items()

        frappe.get_cached_doc("Authorization Control").validate_approving_authority(
            self.doctype, self.company, self.base_grand_total, self
        )
        self.update_project()
        self.update_prevdoc_status("submit")

        self.update_blanket_order()

        from erpnext.accounts.doctype.sales_invoice.sales_invoice import update_linked_doc
        update_linked_doc(self.doctype, self.name, self.inter_company_order_reference)
        
        if self.coupon_code:
            from erpnext.accounts.doctype.pricing_rule.utils import update_coupon_code_count
            update_coupon_code_count(self.coupon_code, "used")

        if self.get("reserve_stock") and not self.get("is_subcontracted"):
            self.create_stock_reservation_entries()

    def on_cancel(self):
        """
        取消方法
        """
        use_new_orchestrator = self._should_use_new_orchestrator()

        if use_new_orchestrator:
            self._on_cancel_new()
        else:
            self._on_cancel_legacy()

    def _on_cancel_new(self):
        """
        新的取消流程
        """
        try:
            from erpnext.selling.events import on_sales_order_cancel
            on_sales_order_cancel(self)
        except Exception as e:
            frappe.log_error(f"New cancel flow failed, falling back to legacy: {str(e)}")
            self._on_cancel_legacy()

    def _on_cancel_legacy(self):
        """
        原有的取消流程
        """
        self.ignore_linked_doctypes = (
            "GL Entry",
            "Stock Ledger Entry",
            "Payment Ledger Entry",
            "Advance Payment Ledger Entry",
            "Unreconcile Payment",
            "Unreconcile Payment Entries",
        )
        super().on_cancel()
        super().update_prevdoc_status()
        if self.status == "Closed":
            frappe.throw(_("Closed order cannot be cancelled. Unclose to cancel."))

        self.delete_delivery_schedule_items()
        self.check_nextdoc_docstatus()
        self.update_reserved_qty()
        self.update_project()
        self.update_prevdoc_status("cancel")

        self.db_set("status", "Cancelled")

        self.update_blanket_order()
        self.cancel_stock_reservation_entries()

        from erpnext.accounts.doctype.sales_invoice.sales_invoice import unlink_inter_company_doc
        unlink_inter_company_doc(self.doctype, self.name, self.inter_company_order_reference)
        
        if self.coupon_code:
            from erpnext.accounts.doctype.pricing_rule.utils import update_coupon_code_count
            update_coupon_code_count(self.coupon_code, "cancelled")

    def _should_use_new_orchestrator(self) -> bool:
        """
        检查是否应该使用新编排器
        可以通过配置或特性开关控制
        """
        # 方式 1: 通过系统设置
        try:
            return frappe.get_cached_value(
                "Selling Settings", 
                None, 
                "use_new_sales_order_orchestrator"
            ) or False
        except Exception:
            pass

        # 方式 2: 通过全局标志
        if hasattr(frappe.local, 'flags') and frappe.local.flags.use_new_orchestrator:
            return True

        # 方式 3: 通过环境变量
        import os
        if os.getenv('ERP_USE_NEW_ORCHESTRATOR') == '1':
            return True

        # 默认使用旧方式（安全起见）
        return False
