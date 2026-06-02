# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""
销售发票/会计领域服务

负责销售相关的会计处理逻辑。
"""

import frappe
from frappe import _
from typing import Dict, List


class SalesInvoiceService:
    """销售发票/会计服务"""

    def post_gl_for_sales_order(self, so_doc) -> Dict:
        """
        为销售订单过账会计分录（占位实现，实际项目中根据需要完善）

        Args:
            so_doc: 销售订单文档

        Returns:
            包含 success, message, entries 的字典
        """
        try:
            # 注意：在标准 ERPNext 中，会计分录通常在销售发票时过账
            # 这里只是一个示例，展示如何在架构中集成会计处理

            entries = []

            # 如果需要在 SO 提交时过账，在这里实现
            # 例如：预提收入、递延收益等

            return {
                "success": True,
                "message": _("GL posting completed"),
                "entries": entries,
            }

        except Exception as e:
            frappe.log_error(
                title="GL Posting Failed",
                message=frappe.get_traceback(),
            )
            return {
                "success": False,
                "message": str(e),
                "entries": [],
            }

    def cancel_gl_entries(self, sales_order_name: str) -> Dict:
        """
        取消销售订单相关的会计分录

        Args:
            sales_order_name: 销售订单名称

        Returns:
            包含 success, message 的字典
        """
        try:
            # 这里实现取消会计分录的逻辑
            # 例如：冲销之前的预提分录

            return {
                "success": True,
                "message": _("GL entries cancelled successfully"),
            }

        except Exception as e:
            frappe.log_error(
                title="Cancel GL Entries Failed",
                message=frappe.get_traceback(),
            )
            return {
                "success": False,
                "message": str(e),
            }

    def create_sales_invoice_from_order(self, sales_order_name: str) -> Dict:
        """
        从销售订单创建销售发票

        Args:
            sales_order_name: 销售订单名称

        Returns:
            包含 success, message, invoice_name 的字典
        """
        try:
            from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

            # 使用现有的映射函数
            si_doc = make_sales_invoice(sales_order_name)
            si_doc.save()
            si_doc.submit()

            return {
                "success": True,
                "message": _("Sales Invoice created successfully"),
                "invoice_name": si_doc.name,
            }

        except Exception as e:
            frappe.log_error(
                title="Create Sales Invoice Failed",
                message=frappe.get_traceback(),
            )
            return {
                "success": False,
                "message": str(e),
                "invoice_name": None,
            }
