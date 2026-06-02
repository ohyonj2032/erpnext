# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""
库存预留领域服务

负责库存预留的核心业务逻辑。
"""

import frappe
from frappe import _
from typing import Dict, List
from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
    StockReservation,
    create_stock_reservation_entries_for_so_items,
)


class StockReservationService:
    """库存预留服务"""

    def reserve_for_sales_order(self, so_doc) -> Dict:
        """
        为销售订单预留库存

        Args:
            so_doc: 销售订单文档对象

        Returns:
            包含 success, message, entries 的字典
        """
        try:
            # 验证库存预留设置
            if not frappe.get_cached_value("Stock Settings", None, "enable_stock_reservation"):
                return {
                    "success": True,
                    "message": _("Stock reservation is disabled"),
                    "entries": [],
                }

            entries = []
            failed_items = []

            # 使用现有的 StockReservation 类创建预留记录
            if so_doc.get("reserve_stock") and not so_doc.get("is_subcontracted"):
                # 创建库存预留条目
                stock_reservation = StockReservation(doc=so_doc)
                is_created = stock_reservation.make_stock_reservation_entries()

                if is_created:
                    # 获取创建的预留记录
                    sre_list = frappe.get_all(
                        "Stock Reservation Entry",
                        filters={"voucher_type": "Sales Order", "voucher_no": so_doc.name},
                        fields=["name", "item_code", "warehouse", "reserved_qty"],
                    )
                    entries = sre_list

            return {
                "success": True,
                "message": _("Stock reserved successfully") if entries else _("No items to reserve"),
                "entries": entries,
                "failed_items": failed_items,
            }

        except Exception as e:
            frappe.log_error(
                title="Stock Reservation Failed",
                message=frappe.get_traceback(),
            )
            return {
                "success": False,
                "message": str(e),
                "entries": [],
                "failed_items": [],
            }

    def cancel_reservations(self, sales_order_name: str) -> Dict:
        """
        取消销售订单的库存预留

        Args:
            sales_order_name: 销售订单名称

        Returns:
            包含 success, message 的字典
        """
        try:
            # 获取所有相关的库存预留记录
            sre_list = frappe.get_all(
                "Stock Reservation Entry",
                filters={"voucher_type": "Sales Order", "voucher_no": sales_order_name, "docstatus": 1},
                pluck="name",
            )

            for sre_name in sre_list:
                sre_doc = frappe.get_doc("Stock Reservation Entry", sre_name)
                sre_doc.cancel()

            return {
                "success": True,
                "message": _("Stock reservations cancelled successfully"),
                "cancelled_count": len(sre_list),
            }

        except Exception as e:
            frappe.log_error(
                title="Cancel Stock Reservation Failed",
                message=frappe.get_traceback(),
            )
            return {
                "success": False,
                "message": str(e),
            }

    def check_availability(
        self,
        item_code: str,
        warehouse: str,
        qty: float,
    ) -> Dict:
        """
        检查库存可用性

        Args:
            item_code: 物料编码
            warehouse: 仓库
            qty: 数量

        Returns:
            包含 available, available_qty, message 的字典
        """
        try:
            from erpnext.stock.utils import get_stock_balance

            available_qty = get_stock_balance(item_code, warehouse)
            return {
                "available": available_qty >= qty,
                "available_qty": available_qty,
                "requested_qty": qty,
                "message": _("Sufficient stock available") if available_qty >= qty else _("Insufficient stock"),
            }
        except Exception as e:
            return {
                "available": False,
                "available_qty": 0,
                "requested_qty": qty,
                "message": str(e),
            }
