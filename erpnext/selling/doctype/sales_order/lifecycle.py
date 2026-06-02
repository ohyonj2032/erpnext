from dataclasses import dataclass
from typing import Callable

import frappe

from erpnext.selling.doctype.sales_order.services import (
	SalesOrderAccountingService,
	SalesOrderNotificationService,
	SalesOrderStateService,
	SalesOrderStockService,
)


@dataclass(frozen=True)
class SalesOrderLifecycleStep:
	name: str
	execute: Callable[[], None]
	rollback: Callable[[], None] | None = None


class SalesOrderLifecycle:
	def __init__(self, sales_order):
		self.sales_order = sales_order
		self.state_service = SalesOrderStateService(sales_order)
		self.accounting_service = SalesOrderAccountingService(sales_order)
		self.stock_service = SalesOrderStockService(sales_order)
		self.notification_service = SalesOrderNotificationService(sales_order)

	def on_submit(self):
		self._run_with_savepoint("submit", self._get_submit_steps())

	def on_cancel(self):
		self._run_with_savepoint("cancel", self._get_cancel_steps())

	def update_status(self, status):
		self._run_with_savepoint("status_update", self._get_status_update_steps(status))

	def _get_submit_steps(self):
		return (
			SalesOrderLifecycleStep(
				"order_state",
				self.state_service.on_submit,
				self.state_service.rollback_submit,
			),
			SalesOrderLifecycleStep(
				"accounting",
				self.accounting_service.on_submit,
				self.accounting_service.rollback_submit,
			),
			SalesOrderLifecycleStep(
				"stock",
				self.stock_service.on_submit,
				self.stock_service.rollback_submit,
			),
		)

	def _get_cancel_steps(self):
		return (
			SalesOrderLifecycleStep(
				"order_state",
				self.state_service.on_cancel,
				self.state_service.rollback_cancel,
			),
			SalesOrderLifecycleStep(
				"stock",
				self.stock_service.on_cancel,
				self.stock_service.rollback_cancel,
			),
			SalesOrderLifecycleStep(
				"accounting",
				self.accounting_service.on_cancel,
				self.accounting_service.rollback_cancel,
			),
		)

	def _get_status_update_steps(self, status):
		return (
			SalesOrderLifecycleStep(
				"order_state",
				lambda: self.state_service.on_status_update(status),
				lambda: self.state_service.rollback_status_update(status),
			),
			SalesOrderLifecycleStep(
				"stock",
				self.stock_service.on_status_update,
				self.stock_service.rollback_status_update,
			),
			SalesOrderLifecycleStep(
				"notifications",
				self.notification_service.on_status_update,
				self.notification_service.rollback_status_update,
			),
		)

	def _run_with_savepoint(self, action, steps):
		savepoint = f"sales_order_{action}"
		completed_steps = []
		frappe.db.savepoint(savepoint)
		try:
			for step in steps:
				step.execute()
				completed_steps.append(step)
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			self._rollback_completed_steps(action, reversed(completed_steps))
			raise

	def _rollback_completed_steps(self, action, steps):
		for step in steps:
			if not step.rollback:
				continue

			try:
				step.rollback()
			except Exception:
				frappe.log_error(
					title=f"Sales Order {action} rollback failed at {step.name}",
					message=frappe.get_traceback(),
				)
