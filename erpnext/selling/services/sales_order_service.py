# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""
销售订单应用服务

本服务作为销售订单业务流程的唯一编排入口，负责：
1. 协调库存预留、会计过账、通知发送等子流程
2. 处理事务一致性和补偿逻辑
3. 发布领域事件
"""

import frappe
from frappe import _
from typing import Dict, List, Optional, Tuple
import traceback

from erpnext.events import (
    publish,
    SalesOrderSubmitted,
    StockReserved,
    StockReservationFailed,
    GLPosted,
    GLPostFailed,
    NotificationSent,
)
from erpnext.stock.services import StockReservationService
from erpnext.accounts.services import SalesInvoiceService
from erpnext.notifications.services import NotificationService


class SalesOrderService:
    """销售订单应用服务"""

    def __init__(self):
        self.stock_service = StockReservationService()
        self.accounting_service = SalesInvoiceService()
        self.notification_service = NotificationService()
        self.compensation_log: List[Dict] = []

    def submit_sales_order(
        self,
        sales_order_name: str,
        enable_stock_reservation: bool = True,
        enable_accounting: bool = True,
        enable_notifications: bool = True,
    ) -> Tuple[bool, str]:
        """
        提交销售订单 - 完整业务流程编排

        Args:
            sales_order_name: 销售订单名称
            enable_stock_reservation: 是否启用库存预留
            enable_accounting: 是否启用会计处理
            enable_notifications: 是否启用通知

        Returns:
            (success: bool, message: str)
        """
        self.compensation_log = []
        so_doc = None

        try:
            # 步骤 1: 获取并验证销售订单
            so_doc = frappe.get_doc("Sales Order", sales_order_name)
            if so_doc.docstatus != 0:
                return False, _("Sales Order is already submitted or cancelled")

            # 步骤 2: 调用父类的基础提交逻辑（更新前置文档状态等）
            self._execute_base_submit(so_doc)

            # 步骤 3: 库存预留（同步，支持补偿）
            if enable_stock_reservation and so_doc.get("reserve_stock"):
                success, message = self._reserve_stock(so_doc)
                if not success:
                    self._compensate()
                    return False, message

            # 步骤 4: 会计处理（同步，支持补偿）
            if enable_accounting:
                success, message = self._post_gl_entries(so_doc)
                if not success:
                    self._compensate()
                    return False, message

            # 步骤 5: 发送通知（异步，不影响主流程）
            if enable_notifications:
                self._send_notifications_async(so_doc)

            # 步骤 6: 发布成功事件
            publish(SalesOrderSubmitted(doc_type="Sales Order", doc_name=sales_order_name, doc=so_doc))

            return True, _("Sales Order submitted successfully")

        except Exception as e:
            frappe.log_error(
                title="Sales Order Submission Failed",
                message=traceback.format_exc(),
            )
            self._compensate()
            return False, str(e)

    def cancel_sales_order(self, sales_order_name: str) -> Tuple[bool, str]:
        """
        取消销售订单

        Args:
            sales_order_name: 销售订单名称

        Returns:
            (success: bool, message: str)
        """
        try:
            so_doc = frappe.get_doc("Sales Order", sales_order_name)
            if so_doc.docstatus != 1:
                return False, _("Sales Order is not in submitted state")

            # 步骤 1: 取消库存预留
            if so_doc.get("reserve_stock"):
                self.stock_service.cancel_reservations(sales_order_name)

            # 步骤 2: 取消会计分录
            self.accounting_service.cancel_gl_entries(sales_order_name)

            # 步骤 3: 执行基础取消逻辑
            so_doc.cancel()

            return True, _("Sales Order cancelled successfully")

        except Exception as e:
            frappe.log_error(
                title="Sales Order Cancellation Failed",
                message=traceback.format_exc(),
            )
            return False, str(e)

    def _execute_base_submit(self, so_doc):
        """执行基础提交逻辑（从原 on_submit 中提取）"""
        # 调用原有的基础方法
        so_doc.check_credit_limit()
        so_doc.update_reserved_qty()
        so_doc.delete_removed_delivery_schedule_items()

        # 验证审批权限
        frappe.get_cached_doc("Authorization Control").validate_approving_authority(
            so_doc.doctype, so_doc.company, so_doc.base_grand_total, so_doc
        )

        so_doc.update_project()
        so_doc.update_prevdoc_status("submit")

        # 更新关联文档
        from erpnext.accounts.doctype.sales_invoice.sales_invoice import update_linked_doc

        update_linked_doc(so_doc.doctype, so_doc.name, so_doc.inter_company_order_reference)

        # 更新优惠券
        if so_doc.coupon_code:
            from erpnext.accounts.doctype.pricing_rule.utils import update_coupon_code_count

            update_coupon_code_count(so_doc.coupon_code, "used")

        so_doc.update_blanket_order()

        # 保存文档状态为已提交
        so_doc.docstatus = 1
        so_doc.save(ignore_permissions=True)

    def _reserve_stock(self, so_doc) -> Tuple[bool, str]:
        """预留库存"""
        try:
            result = self.stock_service.reserve_for_sales_order(so_doc)
            if result["success"]:
                # 记录补偿信息
                self.compensation_log.append(
                    {
                        "action": "reserve_stock",
                        "reverse_action": "cancel_reservations",
                        "args": {"sales_order_name": so_doc.name},
                    }
                )
                publish(
                    StockReserved(
                        doc_type="Stock Reservation Entry",
                        doc_name="",
                        doc=so_doc,
                        stock_reservation_entries=result.get("entries", []),
                    )
                )
                return True, ""
            else:
                publish(
                    StockReservationFailed(
                        doc_type="Sales Order",
                        doc_name=so_doc.name,
                        doc=so_doc,
                        error=result.get("message", "Unknown error"),
                        failed_items=result.get("failed_items", []),
                    )
                )
                return False, result.get("message", "Stock reservation failed")
        except Exception as e:
            return False, str(e)

    def _post_gl_entries(self, so_doc) -> Tuple[bool, str]:
        """过账会计分录"""
        try:
            result = self.accounting_service.post_gl_for_sales_order(so_doc)
            if result["success"]:
                # 记录补偿信息
                self.compensation_log.append(
                    {
                        "action": "post_gl",
                        "reverse_action": "cancel_gl_entries",
                        "args": {"sales_order_name": so_doc.name},
                    }
                )
                publish(
                    GLPosted(
                        doc_type="GL Entry",
                        doc_name="",
                        doc=so_doc,
                        gl_entries=result.get("entries", []),
                    )
                )
                return True, ""
            else:
                publish(
                    GLPostFailed(
                        doc_type="Sales Order",
                        doc_name=so_doc.name,
                        doc=so_doc,
                        error=result.get("message", "Unknown error"),
                    )
                )
                return False, result.get("message", "GL posting failed")
        except Exception as e:
            return False, str(e)

    def _send_notifications_async(self, so_doc):
        """异步发送通知"""
        try:
            frappe.enqueue(
                method="erpnext.selling.services.sales_order_service._send_notifications",
                queue="short",
                timeout=300,
                sales_order_name=so_doc.name,
            )
        except Exception as e:
            frappe.log_error(
                title="Notification Queue Failed",
                message=traceback.format_exc(),
            )

    def _compensate(self):
        """执行补偿逻辑，回滚已执行的操作"""
        frappe.db.rollback()

        for entry in reversed(self.compensation_log):
            try:
                if entry["reverse_action"] == "cancel_reservations":
                    self.stock_service.cancel_reservations(**entry["args"])
                elif entry["reverse_action"] == "cancel_gl_entries":
                    self.accounting_service.cancel_gl_entries(**entry["args"])
            except Exception as e:
                frappe.log_error(
                    title=f"Compensation Failed: {entry['reverse_action']}",
                    message=traceback.format_exc(),
                )

        frappe.db.commit()


def _send_notifications(sales_order_name: str):
    """实际发送通知的函数（用于异步调用）"""
    from erpnext.notifications.services import NotificationService

    service = NotificationService()
    result = service.send_sales_order_notifications(sales_order_name)
    if result["success"]:
        publish(
            NotificationSent(
                doc_type="Notification",
                doc_name="",
                notification_type="sales_order_submitted",
                recipients=result.get("recipients", []),
            )
        )
