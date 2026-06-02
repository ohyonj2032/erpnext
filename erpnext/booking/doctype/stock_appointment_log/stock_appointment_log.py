# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class StockAppointmentLog(Document):
    # begin: auto-generated types
    # end: auto-generated types

    def before_insert(self):
        """插入前处理"""
        if not self.timestamp:
            self.timestamp = frappe.utils.now()
        if not self.user:
            self.user = frappe.session.user
