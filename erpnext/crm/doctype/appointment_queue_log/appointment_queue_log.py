# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document


class AppointmentQueueLog(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		appointment: DF.Link
		action: DF.Data
		details: DF.LongText | None
		queue_position: DF.Int | None
		timestamp: DF.Datetime
	# end: auto-generated types

	pass
