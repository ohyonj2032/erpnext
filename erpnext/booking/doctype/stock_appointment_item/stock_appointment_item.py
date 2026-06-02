# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class StockAppointmentItem(Document):
    # begin: auto-generated types
    # end: auto-generated types

    def before_save(self):
        """保存前处理：获取商品默认信息"""
        if self.item_code and not self.item_name:
            self.item_name = frappe.db.get_value("Item", self.item_code, "item_name")
        if self.item_code and not self.stock_uom:
            self.stock_uom = frappe.db.get_value("Item", self.item_code, "stock_uom")
        if self.item_code and not self.description:
            self.description = frappe.db.get_value("Item", self.item_code, "description")
