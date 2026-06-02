# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from typing import Dict


def on_sales_order_submit(doc, method=None):
    """
    Sales Order 提交时的事件处理器
    通过 ERPNext 的 doc_events 钩子调用
    """
    try:
        from erpnext.selling.services import SalesOrderOrchestrator

        # 使用编排器处理提交流程
        orchestrator = SalesOrderOrchestrator(doc)
        orchestrator.submit()

        frappe.log_error(f"Sales Order {doc.name} submitted via orchestrator")

    except Exception as e:
        frappe.log_error(f"Error in on_sales_order_submit: {str(e)}")
        raise


def on_sales_order_cancel(doc, method=None):
    """
    Sales Order 取消时的事件处理器
    """
    try:
        # 取消库存预留
        from erpnext.stock.services import StockReservationService

        reservation_service = StockReservationService()
        reservation_service.cancel_reservation_for_sales_order(doc)

        # 清理其他关联资源
        _cleanup_on_cancel(doc)

        frappe.log_error(f"Sales Order {doc.name} cancelled successfully")

    except Exception as e:
        frappe.log_error(f"Error in on_sales_order_cancel: {str(e)}")


def _cleanup_on_cancel(doc):
    """
    取消时的清理工作
    """
    # 恢复优惠券
    if doc.coupon_code:
        try:
            from erpnext.accounts.doctype.pricing_rule.utils import update_coupon_code_count
            update_coupon_code_count(doc.coupon_code, "cancelled")
        except Exception as e:
            frappe.log_error(f"Failed to revert coupon code: {str(e)}")


@frappe.whitelist()
def handle_sales_order_notification(event_data: Dict):
    """
    处理 Sales Order 相关的通知（异步）
    """
    event_type = event_data.get("event_type")
    sales_order_name = event_data.get("sales_order_name")

    if not sales_order_name:
        return

    # 根据事件类型发送不同的通知
    if event_type == "submitted":
        _send_submission_notification(sales_order_name)
    elif event_type == "cancelled":
        _send_cancellation_notification(sales_order_name)


def _send_submission_notification(sales_order_name: str):
    """
    发送提交通知
    """
    try:
        doc = frappe.get_doc("Sales Order", sales_order_name)
        
        # 这里可以实现具体的通知逻辑
        # 比如发送邮件、创建待办事项等
        frappe.publish_realtime(
            "sales_order_submission_complete",
            {
                "name": doc.name,
                "customer": doc.customer,
                "grand_total": doc.grand_total
            },
            user=frappe.session.user
        )

    except Exception as e:
        frappe.log_error(f"Failed to send submission notification: {str(e)}")


def _send_cancellation_notification(sales_order_name: str):
    """
    发送取消通知
    """
    try:
        doc = frappe.get_doc("Sales Order", sales_order_name)
        
        frappe.publish_realtime(
            "sales_order_cancelled",
            {
                "name": doc.name,
                "customer": doc.customer,
                "reason": "Cancelled by user"
            },
            user=frappe.session.user
        )

    except Exception as e:
        frappe.log_error(f"Failed to send cancellation notification: {str(e)}")
