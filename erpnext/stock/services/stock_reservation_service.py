# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from typing import Dict, List, Optional
from dataclasses import dataclass


@dataclass
class ReservationResult:
    success: bool
    reservation_entries: List[str] = None
    error_message: Optional[str] = None

    def __post_init__(self):
        self.reservation_entries = self.reservation_entries or []


class StockReservationService:
    """
    库存预留领域服务
    封装所有与库存预留相关的业务逻辑
    """

    def reserve_for_sales_order(self, sales_order: object) -> ReservationResult:
        """
        为 Sales Order 预留库存
        """
        result = ReservationResult(success=False)

        try:
            # 调用原有的创建库存预留条目的方法
            reservation_entries = self._create_reservation_entries(sales_order)
            
            result.reservation_entries = reservation_entries
            result.success = True

            frappe.msgprint(_("Stock Reservation Entries Created"), alert=True, indicator="green")

        except Exception as e:
            result.error_message = str(e)
            frappe.log_error(f"Stock reservation failed for Sales Order {sales_order.name}: {str(e)}")
            raise

        return result

    def cancel_reservation_for_sales_order(self, sales_order: object) -> None:
        """
        取消 Sales Order 的库存预留
        """
        try:
            # 调用原有的取消库存预留条目的方法
            if hasattr(sales_order, 'cancel_stock_reservation_entries'):
                sales_order.cancel_stock_reservation_entries(notify=False)
            else:
                from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
                    cancel_stock_reservation_entries,
                )
                cancel_stock_reservation_entries(
                    voucher_type=sales_order.doctype, 
                    voucher_no=sales_order.name,
                    notify=False
                )

            frappe.msgprint(_("Stock Reservation Entries Cancelled"), alert=True, indicator="orange")

        except Exception as e:
            frappe.log_error(f"Failed to cancel stock reservation for Sales Order {sales_order.name}: {str(e)}")
            # 取消操作失败不应该阻止回滚，只是记录日志

    def _create_reservation_entries(self, sales_order: object) -> List[str]:
        """
        创建库存预留条目
        """
        reservation_entries = []

        # 首先尝试直接调用 Sales Order 的方法
        if hasattr(sales_order, 'create_stock_reservation_entries'):
            sales_order.create_stock_reservation_entries(notify=False)
            
            # 获取创建的预留条目
            from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
                StockReservationEntry,
            )
            entries = frappe.get_all(
                "Stock Reservation Entry",
                filters={
                    "voucher_type": sales_order.doctype,
                    "voucher_no": sales_order.name,
                    "docstatus": 1
                },
                pluck="name"
            )
            reservation_entries = entries

        else:
            # 备用方案：直接从 stock_reservation_entry 模块调用
            from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
                create_stock_reservation_entries_for_sales_order,
            )
            if 'create_stock_reservation_entries_for_sales_order' in globals():
                create_stock_reservation_entries_for_sales_order(sales_order)

        return reservation_entries

    def check_available_stock(self, item_code: str, warehouse: str, qty: float, company: str) -> bool:
        """
        检查可用库存
        """
        from erpnext.stock.stock_balance import get_reserved_qty
        
        available_qty = frappe.get_cached_value(
            "Bin", 
            {"item_code": item_code, "warehouse": warehouse}, 
            "actual_qty"
        ) or 0.0
        
        reserved_qty = get_reserved_qty(item_code, warehouse)
        
        return (available_qty - reserved_qty) >= qty

    def get_reservation_status(self, voucher_type: str, voucher_no: str) -> List[Dict]:
        """
        获取预留状态
        """
        entries = frappe.get_all(
            "Stock Reservation Entry",
            filters={
                "voucher_type": voucher_type,
                "voucher_no": voucher_no,
                "docstatus": ["!=", 2]
            },
            fields=["name", "item_code", "warehouse", "reserved_qty", "delivered_qty", "status"]
        )
        
        return entries
