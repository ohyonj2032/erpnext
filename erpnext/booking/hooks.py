# -*- coding: utf-8 -*-
# Copyright (c) 2024 Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

from __future__ import unicode_literals

# Add appointment doctypes to desk
app_include_js = []

# Scheduled jobs
scheduler_events = {
	"hourly": [
		"erpnext.booking.doctype.stock_appointment.stock_appointment.check_expired_appointments"
	]
}

# Add to sales order menu
after_install = [
	"erpnext.booking.setup.setup"
]
