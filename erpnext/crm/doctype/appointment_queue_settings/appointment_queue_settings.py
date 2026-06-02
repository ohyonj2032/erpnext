# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe.model.document import Document


class AppointmentQueueSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.crm.doctype.appointment_rule.appointment_rule import AppointmentRule

		default_queue_timeout_minutes: DF.Int
		enable_auto_convert_to_order: DF.Check
		enable_queue_management: DF.Check
		enable_stock_reservation: DF.Check
		order_creation_delay_minutes: DF.Int
		reservation_timeout_minutes: DF.Int
		rules: DF.Table[AppointmentRule]
	# end: auto-generated types

	pass
