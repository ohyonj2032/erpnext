// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Appointment Queue", {
	refresh: function (frm) {
		frm.trigger("set_dynamic_buttons");

		if (frm.doc.sales_order) {
			frm.add_custom_button(__("Sales Order"), () => {
				frappe.set_route("Form", "Sales Order", frm.doc.sales_order);
			});
		}

		if (frm.doc.reservation_entry) {
			frm.add_custom_button(__("Stock Reservation"), () => {
				frappe.set_route("Form", "Stock Reservation Entry", frm.doc.reservation_entry);
			});
		}
	},

	set_dynamic_buttons: function (frm) {
		frm.clear_custom_buttons();
		frm.page.clear_inner_toolbar();

		if (frm.doc.docstatus !== 1) return;

		const status = frm.doc.status;

		if (status === "Queued") {
			frm.add_custom_button(__("Process Now"), () => {
				frm.call("process_queue").then((r) => {
					if (!r.exc) frm.reload_doc();
				});
			}).addClass("btn-primary");

			frm.add_custom_button(__("Reschedule"), () => {
				frm.trigger("show_reschedule_dialog");
			});

			frm.add_custom_button(__("Cancel"), () => {
				frm.trigger("show_cancel_dialog");
			});

		} else if (status === "Processing") {
			frm.add_custom_button(__("Complete"), () => {
				frm.call("complete_processing").then((r) => {
					if (!r.exc) frm.reload_doc();
				});
			}).addClass("btn-primary");

			frm.add_custom_button(__("Reschedule"), () => {
				frm.trigger("show_reschedule_dialog");
			});

			frm.add_custom_button(__("Cancel"), () => {
				frm.trigger("show_cancel_dialog");
			});
		}
	},

	show_cancel_dialog: function (frm) {
		const dialog = new frappe.ui.Dialog({
			title: __("Cancel Queue Entry"),
			fields: [
				{
					fieldname: "reason",
					fieldtype: "Small Text",
					label: __("Cancellation Reason"),
				},
			],
			primary_action_label: __("Confirm Cancel"),
			primary_action(values) {
				frm.call("cancel", { reason: values.reason }).then((r) => {
					if (!r.exc) {
						dialog.hide();
						frm.reload_doc();
					}
				});
			},
		});
		dialog.show();
	},

	show_reschedule_dialog: function (frm) {
		const dialog = new frappe.ui.Dialog({
			title: __("Reschedule Queue Entry"),
			fields: [
				{
					fieldname: "new_date",
					fieldtype: "Date",
					label: __("New Scheduled Date"),
					reqd: 1,
				},
				{
					fieldname: "new_time_slot",
					fieldtype: "Data",
					label: __("New Time Slot"),
					description: __("e.g. 14:00-15:00"),
				},
			],
			primary_action_label: __("Confirm Reschedule"),
			primary_action(values) {
				frm.call("reschedule", {
					new_date: values.new_date,
					new_slot: values.new_time_slot,
				}).then((r) => {
					if (!r.exc) {
						dialog.hide();
						frm.reload_doc();
					}
				});
			},
		});
		dialog.show();
	},

	item_code: function (frm) {
		if (frm.doc.item_code) {
			frappe.call({
				method: "erpnext.stock.doctype.item.item.get_item_defaults",
				args: {
					item_code: frm.doc.item_code,
					company: frm.doc.company || frappe.defaults.get_default("Company"),
				},
				callback: function (r) {
					if (r.message && r.message.default_warehouse) {
						frm.set_value("warehouse", r.message.default_warehouse);
					}
				},
			});
		}
	},

	customer: function (frm) {
		if (frm.doc.customer) {
			frappe.call({
				method: "frappe.client.get",
				args: {
					doctype: "Customer",
					name: frm.doc.customer,
				},
				callback: function (r) {
					if (r.message) {
						frm.set_value("customer_name", r.message.customer_name);
					}
				},
			});
		}
	},
});