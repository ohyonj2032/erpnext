# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now, add_days, get_datetime
from typing import Optional, List, Dict
from datetime import datetime


class StockAppointment(Document):
    # begin: auto-generated types
    # end: auto-generated types

    def validate(self):
        """验证预约数据"""
        self.validate_appointment_time()
        self.validate_items()
        self.validate_status_transition()

    def before_insert(self):
        """插入前处理：生成预约编号、初始化状态"""
        if not self.appointment_no:
            self.appointment_no = frappe.model.naming.make_autoname(self, "naming_series")
        if not self.status:
            self.status = "Pending"

    def before_save(self):
        """保存前处理：计算排队信息"""
        self.update_queue_position()

    def on_submit(self):
        """提交时：锁定库存、创建关联记录、触发审计"""
        self.reserve_stock()
        self.create_appointment_log("Submitted")
        frappe.db.commit()

    def on_cancel(self):
        """取消时：释放库存、取消关联记录、触发审计"""
        self.release_stock()
        self.create_appointment_log("Cancelled")
        frappe.db.commit()

    def validate_appointment_time(self):
        """验证预约时间的合理性"""
        if not self.scheduled_time:
            frappe.throw(_("预约时间是必填项"))

        if get_datetime(self.scheduled_time) < get_datetime(now()):
            frappe.throw(_("预约时间不能早于当前时间"))

        if self.expiry_time:
            if get_datetime(self.expiry_time) <= get_datetime(self.scheduled_time):
                frappe.throw(_("过期时间必须晚于预约时间"))

    def validate_items(self):
        """验证预约的商品项"""
        if not self.get("items"):
            frappe.throw(_("请至少添加一个预约商品"))

        for item in self.items:
            if not item.item_code:
                frappe.throw(_("第 {0} 行：商品代码是必填项").format(item.idx))
            if not item.qty or item.qty <= 0:
                frappe.throw(_("第 {0} 行：数量必须大于 0").format(item.idx))
            if not item.warehouse:
                frappe.throw(_("第 {0} 行：仓库是必填项").format(item.idx))

    def validate_status_transition(self):
        """验证状态转换的合理性"""
        allowed_transitions = {
            "Pending": ["Confirm", "Cancelled"],
            "Confirmed": ["Fulfilled", "Cancelled", "Rescheduled"],
            "Fulfilled": [],
            "Cancelled": [],
            "Expired": [],
            "Rescheduled": ["Confirmed", "Cancelled"]
        }

        if self.get_doc_before_save() and self.get_doc_before_save().status:
            old_status = self.get_doc_before_save().status
            if old_status != self.status:
                if self.status not in allowed_transitions.get(old_status, []):
                    frappe.throw(
                        _("不允许从状态 {0} 直接转换到状态 {1}")
                        .format(old_status, self.status)
                    )

    def update_queue_position(self):
        """更新排队位置信息"""
        if self.scheduled_time:
            count = frappe.db.count(
                "Stock Appointment Item",
                filters={
                    "parent": ("!=", self.name) if self.name else (">", ""),
                    "warehouse": ("in", [item.warehouse for item in self.items]),
                    "scheduled_time": ("<=", self.scheduled_time),
                    "docstatus": ["<", 2]
                }
            )
            self.queue_position = count + 1

    def reserve_stock(self):
        """创建库存预留记录"""
        from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
            create_stock_reservation_entries
        )

        for item in self.items:
            if item.reserve_stock:
                # 创建库存预留记录
                sre = frappe.new_doc("Stock Reservation Entry")
                sre.voucher_type = "Stock Appointment"
                sre.voucher_no = self.name
                sre.voucher_detail_no = item.name
                sre.item_code = item.item_code
                sre.warehouse = item.warehouse
                sre.reserved_qty = item.qty
                sre.voucher_qty = item.qty
                sre.company = self.company
                sre.stock_uom = item.stock_uom
                sre.insert(ignore_permissions=True)
                sre.submit()

                item.stock_reservation_entry = sre.name

    def release_stock(self):
        """释放库存预留"""
        from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
            cancel_stock_reservation_entries
        )

        for item in self.items:
            if item.stock_reservation_entry:
                cancel_stock_reservation_entries(
                    voucher_type="Stock Reservation Entry",
                    voucher_no=item.stock_reservation_entry
                )

    def create_appointment_log(self, action: str):
        """创建预约审计日志"""
        log = frappe.new_doc("Stock Appointment Log")
        log.appointment = self.name
        log.action = action
        log.timestamp = now()
        log.user = frappe.session.user
        log.insert(ignore_permissions=True)

    @frappe.whitelist()
    def confirm_appointment(self):
        """确认预约"""
        if self.status == "Pending":
            self.status = "Confirmed"
            self.confirmed_by = frappe.session.user
            self.confirmed_time = now()
            self.save()
            self.create_appointment_log("Confirmed")
            frappe.msgprint(_("预约已确认"), alert=True)

    @frappe.whitelist()
    def cancel_appointment(self, reason: Optional[str] = None):
        """取消预约"""
        if self.status in ["Pending", "Confirmed", "Rescheduled"]:
            self.status = "Cancelled"
            self.cancel_reason = reason
            self.cancelled_by = frappe.session.user
            self.cancelled_time = now()
            self.save()
            self.cancel()
            frappe.msgprint(_("预约已取消"), alert=True)

    @frappe.whitelist()
    def reschedule_appointment(self, new_scheduled_time: str):
        """改签预约"""
        if self.status in ["Pending", "Confirmed"]:
            old_time = self.scheduled_time
            self.scheduled_time = new_scheduled_time
            self.old_scheduled_time = old_time
            self.status = "Rescheduled"
            self.rescheduled_by = frappe.session.user
            self.rescheduled_time = now()
            self.save()
            self.create_appointment_log("Rescheduled")
            frappe.msgprint(_("预约已改签至 {0}").format(new_scheduled_time), alert=True)

    @frappe.whitelist()
    def fulfill_appointment(self):
        """完成预约（完成取货/服务）"""
        if self.status == "Confirmed":
            self.status = "Fulfilled"
            self.fulfilled_by = frappe.session.user
            self.fulfilled_time = now()
            self.save()
            self.create_appointment_log("Fulfilled")
            frappe.msgprint(_("预约已完成"), alert=True)


@frappe.whitelist()
def check_expired_appointments():
    """定时任务：检查并处理过期预约"""
    appointments = frappe.get_all(
        "Stock Appointment",
        filters={
            "status": ["in", ["Pending", "Confirmed"]],
            "expiry_time": ("<", now()),
            "docstatus": 1
        },
        fields=["name"]
    )

    for appointment in appointments:
        doc = frappe.get_doc("Stock Appointment", appointment.name)
        doc.status = "Expired"
        doc.save()
        doc.create_appointment_log("Expired")
        doc.release_stock()

    frappe.db.commit()
    return len(appointments)
