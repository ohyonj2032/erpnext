# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""
预约/排队模块工具函数
"""

import frappe
from frappe import _
from frappe.model.mapper import get_mapped_doc
from typing import Dict, List, Optional
from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
    create_stock_reservation_entries,
    cancel_stock_reservation_entries
)


def create_appointment_from_sales_order(source_name: str, target_doc=None):
    """
    从销售订单创建预约
    
    Args:
        source_name: 销售订单名称
        target_doc: 目标文档（可选）
    
    Returns:
        新创建的预约文档
    """
    def set_missing_values(source, target):
        target.company = source.company
        target.customer = source.customer
        target.customer_name = source.customer_name
        target.sales_order = source.name
        target.status = "Pending"

    def update_item(source, target, source_parent):
        target.reserve_stock = 1 if source_parent.reserve_stock else 0
        target.description = source.description
        target.stock_uom = source.stock_uom

    doc = get_mapped_doc(
        "Sales Order",
        source_name,
        {
            "Sales Order": {
                "doctype": "Stock Appointment",
                "field_map": {
                    "name": "sales_order",
                    "customer": "customer",
                    "customer_name": "customer_name",
                    "company": "company",
                    "transaction_date": "transaction_date"
                },
                "validation": {"docstatus": ["=", 1]},
                "postprocess": set_missing_values
            },
            "Sales Order Item": {
                "doctype": "Stock Appointment Item",
                "field_map": {
                    "item_code": "item_code",
                    "item_name": "item_name",
                    "qty": "qty",
                    "warehouse": "warehouse",
                    "description": "description"
                },
                "postprocess": update_item
            }
        },
        target_doc,
        set_missing_values
    )
    
    return doc


def create_delivery_note_from_appointment(source_name: str, target_doc=None):
    """
    从预约创建交货单
    
    Args:
        source_name: 预约名称
        target_doc: 目标文档（可选）
    
    Returns:
        新创建的交货单文档
    """
    appointment = frappe.get_doc("Stock Appointment", source_name)
    
    if appointment.status != "Confirmed":
        frappe.throw(_("只有已确认的预约才能创建交货单"))
    
    if not appointment.sales_order:
        frappe.throw(_("该预约没有关联的销售订单"))

    def set_missing_values(source, target):
        target.stock_appointment = source_name
        target.company = source.company

    doc = get_mapped_doc(
        "Sales Order",
        appointment.sales_order,
        {
            "Sales Order": {
                "doctype": "Delivery Note",
                "validation": {"docstatus": ["=", 1]},
                "postprocess": set_missing_values
            },
            "Sales Order Item": {
                "doctype": "Delivery Note Item",
                "field_map": {
                    "item_code": "item_code",
                    "item_name": "item_name",
                    "qty": "qty",
                    "warehouse": "warehouse",
                    "description": "description"
                }
            }
        },
        target_doc
    )
    
    return doc


def get_appointments_by_sales_order(sales_order: str) -> List[Dict]:
    """
    获取与指定销售订单关联的所有预约
    
    Args:
        sales_order: 销售订单名称
    
    Returns:
        预约信息列表
    """
    appointments = frappe.get_all(
        "Stock Appointment",
        filters={
            "sales_order": sales_order,
            "docstatus": ["<", 2]
        },
        fields=["name", "appointment_no", "status", "scheduled_time", "queue_position"]
    )
    return appointments


def get_queue_info(warehouse: str, scheduled_time: str) -> Dict:
    """
    获取指定仓库和时间的排队信息
    
    Args:
        warehouse: 仓库
        scheduled_time: 预约时间
    
    Returns:
        排队信息字典
    """
    count = frappe.db.count(
        "Stock Appointment Item",
        filters={
            "warehouse": warehouse,
            "parenttype": "Stock Appointment",
            "scheduled_time": ("<=", scheduled_time),
            "docstatus": ["<", 2]
        }
    )
    
    upcoming = frappe.get_all(
        "Stock Appointment",
        filters={
            "docstatus": 1,
            "status": ["in", ["Pending", "Confirmed"]],
            "scheduled_time": (">=", scheduled_time)
        },
        fields=["name", "appointment_no", "scheduled_time", "status"],
        order_by="scheduled_time asc",
        limit=10
    )
    
    return {
        "current_position": count + 1,
        "upcoming_appointments": upcoming
    }


def validate_booking_capacity(warehouse: str, scheduled_time: str) -> bool:
    """
    验证指定仓库和时间是否还有预约容量
    
    Args:
        warehouse: 仓库
        scheduled_time: 预约时间
    
    Returns:
        是否有容量
    """
    settings = frappe.get_cached_doc("Booking Settings")
    max_capacity = settings.max_appointments_per_timeslot or 10
    
    count = frappe.db.count(
        "Stock Appointment",
        filters={
            "docstatus": 1,
            "status": ["in", ["Pending", "Confirmed"]],
            "scheduled_time": scheduled_time
        }
    )
    
    return count < max_capacity
