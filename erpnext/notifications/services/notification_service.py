# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""
通知领域服务

负责发送各类通知（邮件、系统通知等）。
"""

import frappe
from frappe import _
from typing import Dict, List


class NotificationService:
    """通知服务"""

    def send_sales_order_notifications(self, sales_order_name: str) -> Dict:
        """
        发送销售订单相关通知

        Args:
            sales_order_name: 销售订单名称

        Returns:
            包含 success, message, recipients 的字典
        """
        try:
            so_doc = frappe.get_doc("Sales Order", sales_order_name)
            recipients = []

            # 获取客户联系人
            if so_doc.contact_person:
                recipients.append(so_doc.contact_person)

            # 可以添加更多接收者，如销售团队等
            if so_doc.get("sales_team"):
                for member in so_doc.sales_team:
                    if member.sales_person:
                        recipients.append(member.sales_person)

            # 发送系统通知
            if recipients:
                self._send_system_notification(
                    recipients=recipients,
                    subject=_("Sales Order {0} Submitted").format(sales_order_name),
                    message=_("Sales Order {0} has been submitted successfully.").format(sales_order_name),
                    document_type="Sales Order",
                    document_name=sales_order_name,
                )

            # 发送邮件通知（可选）
            # self._send_email_notification(so_doc)

            return {
                "success": True,
                "message": _("Notifications sent successfully"),
                "recipients": recipients,
            }

        except Exception as e:
            frappe.log_error(
                title="Send Notifications Failed",
                message=frappe.get_traceback(),
            )
            return {
                "success": False,
                "message": str(e),
                "recipients": [],
            }

    def _send_system_notification(
        self,
        recipients: List[str],
        subject: str,
        message: str,
        document_type: str = None,
        document_name: str = None,
    ):
        """发送系统通知"""
        for recipient in recipients:
            try:
                notification = frappe.get_doc(
                    {
                        "doctype": "Notification Log",
                        "subject": subject,
                        "email_content": message,
                        "for_user": recipient,
                        "document_type": document_type,
                        "document_name": document_name,
                        "type": "Alert",
                    }
                )
                notification.insert(ignore_permissions=True)
            except Exception as e:
                frappe.log_error(
                    title="Create Notification Log Failed",
                    message=frappe.get_traceback(),
                )

    def _send_email_notification(self, so_doc):
        """发送邮件通知（可选实现）"""
        # 这里可以实现邮件发送逻辑
        pass
