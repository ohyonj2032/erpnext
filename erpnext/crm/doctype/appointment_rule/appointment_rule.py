# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document


class AppointmentRule(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		actions: DF.Code | None
		conditions: DF.Code | None
		is_active: DF.Check
		priority: DF.Int
		rule_name: DF.Data
		rule_type: DF.Literal["Cancellation", "Timeout Release", "Manual Reschedule", "Stock Reservation", "Auto Convert"]
		timeout_minutes: DF.Int
	# end: auto-generated types

	pass
