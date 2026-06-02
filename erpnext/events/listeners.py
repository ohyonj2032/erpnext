# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""
事件监听器 - 监听并响应各种领域事件
"""

import frappe
from frappe import _

from erpnext.events import (
    subscribe,
    SalesOrderSubmitted,
    StockReserved,
    GLPosted,
    NotificationSent,
)


@subscribe(SalesOrderSubmitted)
def on_sales_order_submitted(event):
    """销售订单提交事件处理器"""
    frappe.logger().info(
        f"Sales Order {event.doc_name} submitted successfully",
        extra={"event_id": event.event_id},
    )


@subscribe(StockReserved)
def on_stock_reserved(event):
    """库存预留成功事件处理器"""
    frappe.logger().info(
        f"Stock reserved for {event.doc_type} {event.doc_name}",
        extra={"event_id": event.event_id},
    )


@subscribe(GLPosted)
def on_gl_posted(event):
    """会计分录过账成功事件处理器"""
    frappe.logger().info(
        f"GL entries posted for {event.doc_type} {event.doc_name}",
        extra={"event_id": event.event_id},
    )


@subscribe(NotificationSent)
def on_notification_sent(event):
    """通知发送成功事件处理器"""
    frappe.logger().info(
        f"Notification {event.notification_type} sent to {len(event.recipients)} recipients",
        extra={"event_id": event.event_id},
    )


def register_listeners():
    """注册所有事件监听器（在应用启动时调用）"""
    # 监听器已通过 @subscribe 装饰器自动注册
    pass
