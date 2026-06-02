# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import json
from datetime import datetime
from typing import Any, Optional

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder.functions import Count
from frappe.utils import cint, flt, get_link_to_form, getdate, now, now_datetime

from erpnext.selling.doctype.appointment_queue.queue_strategies import (
	get_strategy,
)


class AppointmentQueue(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.selling.doctype.appointment_queue.appointment_queue_status_log import (
			AppointmentQueueStatusLog,
		)

		contact_email: DF.Data | None
		contact_phone: DF.Data | None
		customer: DF.Link
		customer_name: DF.ReadOnly | None
		extension_data: DF.JSON | None
		item_code: DF.Link
		item_name: DF.ReadOnly | None
		notes: DF.LongText | None
		priority: DF.Literal["Low", "Medium", "High", "Urgent"]
		queue_number: DF.Data
		reservation_entry: DF.Link | None
		sales_order: DF.Link | None
		scheduled_date: DF.Date
		scheduled_time_slot: DF.Data | None
		service_qty: DF.Float
		status: DF.Literal[
			"Draft", "Queued", "Processing", "Completed", "Cancelled", "Timeout", "Rescheduled"
		]
		status_log: DF.Table[AppointmentQueueStatusLog]
		stock_uom: DF.Link | None
		warehouse: DF.Link
	# end: auto-generated types

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self._status_before_save = None

	def validate(self):
		self.validate_mandatory()
		self.validate_dates()
		self.validate_warehouse()
		self.validate_item()
		self.set_queue_number()

	def validate_mandatory(self):
		if not self.customer:
			frappe.throw(_("Customer is required"))
		if not self.item_code:
			frappe.throw(_("Item Code is required"))
		if not self.scheduled_date:
			frappe.throw(_("Scheduled Date is required"))
		if not self.warehouse:
			frappe.throw(_("Warehouse is required"))

	def validate_dates(self):
		if getdate(self.scheduled_date) < getdate():
			frappe.throw(_("Scheduled Date cannot be in the past"))

	def validate_warehouse(self):
		from erpnext.stock.utils import validate_disabled_warehouse, validate_warehouse_company

		validate_disabled_warehouse(self.warehouse)
		if self.company:
			validate_warehouse_company(self.warehouse, self.company)

	def validate_item(self):
		item = frappe.get_cached_value(
			"Item", self.item_code, ["is_stock_item", "is_service_item", "item_name"], as_dict=True
		)
		if not item:
			frappe.throw(_("Item {0} does not exist").format(self.item_code))

		self.item_name = item.item_name

	def set_queue_number(self):
		if self.queue_number:
			return
		date_part = getdate(self.scheduled_date).strftime("%Y%m%d")
		count = frappe.db.count(
			self.doctype, filters={"scheduled_date": self.scheduled_date, "name": ("!=", self.name or "---")}
		)
		self.queue_number = f"Q-{date_part}-{count + 1:04d}"

	def before_insert(self):
		self.set_status_if_not_set()

	def set_status_if_not_set(self):
		if not self.status or self.status == "Draft":
			self.status = "Queued"

	def _capture_status_before(self):
		self._status_before_save = frappe.db.get_value(self.doctype, self.name, "status") if self.name else None

	def onload(self):
		self._capture_status_before()

	def before_save(self):
		self._capture_status_before()

	def on_update(self):
		self._handle_status_transition()

	def _handle_status_transition(self):
		if not self._status_before_save:
			return
		if self._status_before_save == self.status:
			return
		if not self.flags.get("_status_logged"):
			self._log_status_change(self._status_before_save, self.status)
			self.flags["_status_logged"] = True

	def _log_status_change(self, from_status: str, to_status: str):
		log_row = self.append("status_log", {})
		log_row.from_status = from_status
		log_row.to_status = to_status
		log_row.changed_by = frappe.session.user
		log_row.changed_at = now()
		log_row.reason = self._get_transition_reason(from_status, to_status)

	def _get_transition_reason(self, from_status: str, to_status: str) -> str:
		reasons = {
			("Draft", "Queued"): "Queue entry created",
			("Queued", "Processing"): "Processing started",
			("Processing", "Completed"): "Processing completed",
			("Queued", "Cancelled"): "Cancelled by user",
			("Processing", "Cancelled"): "Cancelled during processing",
			("Queued", "Timeout"): "Queue timeout expired",
			("Processing", "Timeout"): "Processing timeout expired",
			("Queued", "Rescheduled"): "Rescheduled to new time slot",
			("Cancelled", "Queued"): "Re-queued",
		}
		return reasons.get((from_status, to_status), f"Status changed from {from_status} to {to_status}")

	# ------------------------------------------------------------------
	# Transaction-safe status transitions
	# ------------------------------------------------------------------

	def process_queue(self) -> None:
		"""Move from Queued -> Processing with inventory reservation in one transaction."""
		self._assert_status("Queued")

		savepoint = "apq_process"
		try:
			frappe.db.savepoint(savepoint)
			self._change_status("Processing")

			self._reserve_stock()
			self._create_sales_order()

			frappe.db.release_savepoint(savepoint)
		except Exception:
			frappe.db.rollback(savepoint)
			self._change_status("Queued")
			raise

	def complete_processing(self) -> None:
		"""Move from Processing -> Completed."""
		self._assert_status("Processing")
		self._change_status("Completed")

		strategy = self._get_active_strategy()
		if strategy:
			strategy.on_complete(self)

	def cancel(self, reason: Optional[str] = None) -> None:
		"""Cancel the queue entry and release inventory."""
		self._change_status("Cancelled")

		self._release_stock()
		self._cancel_sales_order()

		strategy = self._get_active_strategy()
		if strategy:
			strategy.on_cancel(self, reason)

	def handle_timeout(self) -> None:
		"""Handle timeout - release inventory and mark as Timeout."""
		self._change_status("Timeout")

		self._release_stock()

		strategy = self._get_active_strategy()
		if strategy:
			strategy.on_timeout(self)

	def reschedule(self, new_date: str, new_slot: Optional[str] = None) -> None:
		"""Reschedule to a new date/time slot."""
		self._assert_status_in(["Queued", "Processing"])

		old_date = self.scheduled_date
		old_slot = self.scheduled_time_slot
		old_status = self.status

		savepoint = "apq_reschedule"
		try:
			frappe.db.savepoint(savepoint)

			self.scheduled_date = new_date
			self.scheduled_time_slot = new_slot
			self.queue_number = ""  # Will regenerate on validate
			self.status = "Rescheduled"
			self._log_status_change(old_status, "Rescheduled")

			# Requeue after reschedule
			self.status = "Queued"
			self._log_status_change("Rescheduled", "Queued")

			self.save(ignore_permissions=True)
			frappe.db.release_savepoint(savepoint)
		except Exception:
			frappe.db.rollback(savepoint)
			self.scheduled_date = old_date
			self.scheduled_time_slot = old_slot
			self.status = old_status
			raise

		strategy = self._get_active_strategy()
		if strategy:
			strategy.on_reschedule(self, old_date, old_slot)

	# ------------------------------------------------------------------
	# Stock integration
	# ------------------------------------------------------------------

	def _reserve_stock(self):
		stock_item = self._is_stock_item()
		if not stock_item:
			return

		from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
			get_available_qty_to_reserve,
		)

		available_qty = get_available_qty_to_reserve(self.item_code, self.warehouse)
		if flt(available_qty) < flt(self.service_qty):
			frappe.throw(
				_("Insufficient stock for Item {0} in Warehouse {1}. Available: {2}, Required: {3}").format(
					self.item_code, self.warehouse, available_qty, self.service_qty
				)
			)

		sre = frappe.new_doc("Stock Reservation Entry")
		sre.item_code = self.item_code
		sre.warehouse = self.warehouse
		sre.voucher_type = self.doctype
		sre.voucher_no = self.name
		sre.voucher_qty = self.service_qty
		sre.reserved_qty = self.service_qty
		sre.company = self.company
		sre.stock_uom = self.stock_uom
		sre.reservation_based_on = "Qty"

		item_details = frappe.get_cached_value(
			"Item", self.item_code, ["has_serial_no", "has_batch_no"], as_dict=True
		)
		if item_details:
			sre.has_serial_no = cint(item_details.has_serial_no)
			sre.has_batch_no = cint(item_details.has_batch_no)

		sre.flags.ignore_permissions = True
		sre.insert()
		sre.submit()

		self.reservation_entry = sre.name
		self.db_set("reservation_entry", sre.name)

	def _release_stock(self):
		if not self.reservation_entry:
			return

		sre_doc = frappe.get_doc("Stock Reservation Entry", self.reservation_entry)
		if sre_doc.docstatus == 1:
			sre_doc.cancel()

		self.reservation_entry = None
		self.db_set("reservation_entry", None)

	# ------------------------------------------------------------------
	# Sales Order integration
	# ------------------------------------------------------------------

	def _create_sales_order(self):
		so = frappe.new_doc("Sales Order")
		so.customer = self.customer
		so.transaction_date = getdate(self.scheduled_date)
		so.delivery_date = getdate(self.scheduled_date)
		so.company = self.company
		so.set_warehouse = self.warehouse
		so.flags.ignore_permissions = True

		so_item = so.append("items", {})
		so_item.item_code = self.item_code
		so_item.item_name = self.item_name
		so_item.qty = self.service_qty
		so_item.warehouse = self.warehouse
		so_item.uom = self.stock_uom
		so_item.stock_uom = self.stock_uom
		so_item.conversion_factor = 1
		so_item.stock_qty = self.service_qty

		so.appointment_queue = self.name
		so.insert()
		so.submit()

		self.sales_order = so.name
		self.db_set("sales_order", so.name)

	def _cancel_sales_order(self):
		if not self.sales_order:
			return

		so = frappe.get_doc("Sales Order", self.sales_order)
		if so.docstatus == 1:
			so.cancel()

		self.sales_order = None
		self.db_set("sales_order", None)

	# ------------------------------------------------------------------
	# Helpers
	# ------------------------------------------------------------------

	def _assert_status(self, expected: str):
		if self.status != expected:
			frappe.throw(
				_("Appointment Queue {0} is in status '{1}', expected '{2}'").format(
					self.name, self.status, expected
				)
			)

	def _assert_status_in(self, expected_list: list):
		if self.status not in expected_list:
			frappe.throw(
				_("Appointment Queue {0} is in status '{1}', expected one of {2}").format(
					self.name, self.status, ", ".join(expected_list)
				)
			)

	def _change_status(self, new_status: str):
		old_status = self.status
		self.db_set("status", new_status)
		self.status = new_status
		self._log_status_change(old_status, new_status)

	def _get_active_strategy(self):
		ext_data = self._get_extension_data()
		strategy_name = ext_data.get("strategy") if ext_data else None
		if strategy_name:
			return get_strategy(strategy_name)
		return None

	def _get_extension_data(self) -> Optional[dict]:
		if not self.extension_data:
			return None
		if isinstance(self.extension_data, str):
			return json.loads(self.extension_data)
		return self.extension_data

	@property
	def company(self) -> Optional[str]:
		if not self.customer:
			return None

		default_company = frappe.defaults.get_user_default("Company")
		if default_company:
			return default_company

		customer_company = frappe.get_cached_value("Customer", self.customer, "company")
		return customer_company

	@staticmethod
	def get_open_queue_count(scheduled_date: str, warehouse: Optional[str] = None) -> int:
		filters = {
			"scheduled_date": scheduled_date,
			"status": ["in", ["Queued", "Processing"]],
		}
		if warehouse:
			filters["warehouse"] = warehouse
		return frappe.db.count("Appointment Queue", filters=filters)

	@staticmethod
	def get_queue_for_date(scheduled_date: str) -> list:
		return frappe.get_all(
			"Appointment Queue",
			filters={"scheduled_date": scheduled_date},
			fields=["name", "queue_number", "customer", "item_code", "status", "priority", "scheduled_time_slot", "creation"],
			order_by="priority desc, creation asc",
		)


@frappe.whitelist()
def bulk_process_queue(scheduled_date: str, warehouse: Optional[str] = None, limit: int = 10) -> dict:
	"""Process next N queued items for a given date."""
	filters = {
		"status": "Queued",
		"scheduled_date": scheduled_date,
	}
	if warehouse:
		filters["warehouse"] = warehouse

	queued_items = frappe.get_all(
		"Appointment Queue",
		filters=filters,
		fields=["name"],
		order_by="priority desc, creation asc",
		limit_page_length=limit,
	)

	processed = 0
	errors = []
	for item in queued_items:
		try:
			doc = frappe.get_doc("Appointment Queue", item.name)
			doc.process_queue()
			processed += 1
		except Exception as e:
			errors.append({"name": item.name, "error": str(e)})

	return {"processed": processed, "errors": errors}


@frappe.whitelist()
def release_timeout_queues(timeout_minutes: int = 120) -> dict:
	"""Release queues that have been stuck in Processing for too long."""
	threshold = frappe.utils.add_to_date(None, minutes=-timeout_minutes, as_datetime=True)

	entries = frappe.get_all(
		"Appointment Queue",
		filters={
			"status": ["in", ["Queued", "Processing"]],
			"modified": ["<=", threshold],
		},
		fields=["name"],
	)

	released = 0
	for entry in entries:
		try:
			doc = frappe.get_doc("Appointment Queue", entry.name)
			doc.handle_timeout()
			released += 1
		except Exception:
			continue

	return {"released": released}