import frappe
from frappe import _
from frappe.desk.notifications import clear_doctype_notifications

from erpnext.accounts.doctype.sales_invoice.sales_invoice import (
	unlink_inter_company_doc,
	update_linked_doc,
)


class SalesOrderLifecycle:
	def __init__(self, sales_order):
		self.sales_order = sales_order

	def on_submit(self):
		self._run_with_savepoint(
			"submit",
			(
				self._submit_order_state,
				self._submit_related_links,
				self._submit_stock,
			),
		)

	def on_cancel(self):
		self._run_with_savepoint(
			"cancel",
			(
				self._cancel_order_state,
				self._cancel_stock,
				self._cancel_related_links,
			),
		)

	def update_status(self, status):
		self._run_with_savepoint(
			"status_update",
			(
				lambda: self._update_status_order_state(status),
				self._update_status_related_state,
				self._update_status_notifications,
			),
		)

	def _run_with_savepoint(self, action, steps):
		savepoint = f"sales_order_{action}"
		frappe.db.savepoint(savepoint)
		try:
			for step in steps:
				step()
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			raise

	def _submit_order_state(self):
		self.sales_order.check_credit_limit()
		self.sales_order.update_reserved_qty()
		self.sales_order.delete_removed_delivery_schedule_items()
		frappe.get_cached_doc("Authorization Control").validate_approving_authority(
			self.sales_order.doctype,
			self.sales_order.company,
			self.sales_order.base_grand_total,
			self.sales_order,
		)
		self.sales_order.update_project()
		self.sales_order.update_prevdoc_status("submit")
		self.sales_order.update_blanket_order()

	def _submit_related_links(self):
		update_linked_doc(
			self.sales_order.doctype,
			self.sales_order.name,
			self.sales_order.inter_company_order_reference,
		)

		if self.sales_order.coupon_code:
			from erpnext.accounts.doctype.pricing_rule.utils import update_coupon_code_count

			update_coupon_code_count(self.sales_order.coupon_code, "used")

	def _submit_stock(self):
		if self.sales_order.get("reserve_stock") and not self.sales_order.get("is_subcontracted"):
			self.sales_order.create_stock_reservation_entries()

	def _cancel_order_state(self):
		if self.sales_order.status == "Closed":
			frappe.throw(_("Closed order cannot be cancelled. Unclose to cancel."))

		self.sales_order.delete_delivery_schedule_items()
		self.sales_order.check_nextdoc_docstatus()
		self.sales_order.update_reserved_qty()
		self.sales_order.update_project()
		self.sales_order.update_prevdoc_status("cancel")
		self.sales_order.db_set("status", "Cancelled")
		self.sales_order.update_blanket_order()

	def _cancel_stock(self):
		self.sales_order.cancel_stock_reservation_entries()

	def _cancel_related_links(self):
		unlink_inter_company_doc(
			self.sales_order.doctype,
			self.sales_order.name,
			self.sales_order.inter_company_order_reference,
		)

		if self.sales_order.coupon_code:
			from erpnext.accounts.doctype.pricing_rule.utils import update_coupon_code_count

			update_coupon_code_count(self.sales_order.coupon_code, "cancelled")

	def _update_status_order_state(self, status):
		self.sales_order.set_status(update=True, status=status)
		if status == "Draft" and self.sales_order.docstatus == 1:
			self.sales_order.check_credit_limit()

	def _update_status_related_state(self):
		self.sales_order.update_reserved_qty()
		self.sales_order.update_subcontracting_order_status()
		self.sales_order.update_blanket_order()

	def _update_status_notifications(self):
		self.sales_order.notify_update()
		clear_doctype_notifications(self.sales_order)
