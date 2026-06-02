# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from typing import Optional

import frappe
from frappe.utils import get_link_to_form


class BaseQueueStrategy:
	"""Base class for all queue extension strategies.

	Subclass this to implement custom behavior for:
	- on_cancel:      Called when a queue entry is cancelled
	- on_timeout:     Called when a queue entry times out
	- on_reschedule:  Called when a queue entry is rescheduled
	- on_complete:    Called when processing completes

	Each strategy is identified by a unique name and can store
	its own configuration in the queue's `extension_data` JSON field.
	"""

	name = "default"

	@classmethod
	def get_name(cls) -> str:
		return cls.name

	def on_cancel(self, queue_doc, reason: Optional[str] = None):
		pass

	def on_timeout(self, queue_doc):
		pass

	def on_reschedule(self, queue_doc, old_date: str, old_slot: Optional[str] = None):
		pass

	def on_complete(self, queue_doc):
		pass


class PenaltyStrategy(BaseQueueStrategy):
	"""Strategy: apply penalty fee on cancellation or timeout."""

	name = "penalty"

	def on_cancel(self, queue_doc, reason: Optional[str] = None):
		self._create_penalty_entry(queue_doc, reason or "Cancellation penalty")

	def on_timeout(self, queue_doc):
		self._create_penalty_entry(queue_doc, "Timeout penalty (no-show)")

	def _create_penalty_entry(self, queue_doc, reason: str):
		penalty_amount = queue_doc._get_extension_data().get("penalty_amount", 0) if queue_doc._get_extension_data() else 0
		if not penalty_amount:
			return

		penalty_entry = frappe.new_doc("Journal Entry")
		penalty_entry.voucher_type = "Debit Note"
		penalty_entry.company = queue_doc.company
		penalty_entry.posting_date = frappe.utils.nowdate()

		customer_account = frappe.get_cached_value("Customer", queue_doc.customer, "customer_account")
		if customer_account:
			debit_line = penalty_entry.append("accounts", {})
			debit_line.account = customer_account
			debit_line.debit_in_account_currency = penalty_amount

		penalty_account = frappe.get_cached_value("Company", queue_doc.company, "default_income_account")
		if penalty_account:
			credit_line = penalty_entry.append("accounts", {})
			credit_line.account = penalty_account
			credit_line.credit_in_account_currency = penalty_amount

		penalty_entry.flags.ignore_permissions = True
		penalty_entry.insert()
		penalty_entry.submit()

		frappe.msgprint(
			_("Penalty amount {0} applied via {1}").format(
				penalty_amount, get_link_to_form(penalty_entry.doctype, penalty_entry.name)
			)
		)


class NotificationStrategy(BaseQueueStrategy):
	"""Strategy: send notifications on queue state changes."""

	name = "notification"

	def on_cancel(self, queue_doc, reason: Optional[str] = None):
		self._notify(queue_doc, f"Your queue entry {queue_doc.queue_number} has been cancelled.")
		if reason:
			self._notify(queue_doc, f"Reason: {reason}")

	def on_timeout(self, queue_doc):
		self._notify(queue_doc, f"Your queue entry {queue_doc.queue_number} has timed out.")

	def on_reschedule(self, queue_doc, old_date: str, old_slot: Optional[str] = None):
		self._notify(
			queue_doc,
			f"Your queue entry {queue_doc.queue_number} has been rescheduled "
			f"from {old_date} {old_slot or ''} to {queue_doc.scheduled_date} {queue_doc.scheduled_time_slot or ''}.",
		)

	def on_complete(self, queue_doc):
		self._notify(queue_doc, f"Your queue entry {queue_doc.queue_number} has been completed.")

	def _notify(self, queue_doc, message: str):
		if queue_doc.contact_email:
			frappe.sendmail(
				recipients=[queue_doc.contact_email],
				subject=_("Queue Update: {0}").format(queue_doc.queue_number),
				message=message,
			)


class AuditLogStrategy(BaseQueueStrategy):
	"""Strategy: write detailed audit log for each state transition."""

	name = "audit_log"

	def on_cancel(self, queue_doc, reason: Optional[str] = None):
		self._write_audit(queue_doc, "CANCEL", reason)

	def on_timeout(self, queue_doc):
		self._write_audit(queue_doc, "TIMEOUT")

	def on_reschedule(self, queue_doc, old_date: str, old_slot: Optional[str] = None):
		self._write_audit(
			queue_doc,
			"RESCHEDULE",
			f"from {old_date} {old_slot or ''} to {queue_doc.scheduled_date} {queue_doc.scheduled_time_slot or ''}",
		)

	def on_complete(self, queue_doc):
		self._write_audit(queue_doc, "COMPLETE")

	def _write_audit(self, queue_doc, action: str, detail: Optional[str] = None):
		log = frappe.get_doc(
			{
				"doctype": "Appointment Queue Status Log",
				"parent": queue_doc.name,
				"parentfield": "status_log",
				"parenttype": "Appointment Queue",
				"from_status": queue_doc._status_before_save or queue_doc.status,
				"to_status": queue_doc.status,
				"changed_by": "System",
				"changed_at": frappe.utils.now(),
				"reason": f"[{action}] {detail or ''}",
			}
		)
		log.db_insert()


class CompositeStrategy(BaseQueueStrategy):
	"""Strategy: compose multiple strategies together.

	Usage:
	    In extension_data JSON, set:
	    {"strategy": "composite", "strategies": ["penalty", "notification"]}
	"""

	name = "composite"

	def __init__(self):
		self._strategies = []

	def _load_strategies(self, queue_doc):
		if self._strategies:
			return
		ext = queue_doc._get_extension_data() or {}
		names = ext.get("strategies", [])
		self._strategies = [get_strategy(n) for n in names if get_strategy(n)]

	def on_cancel(self, queue_doc, reason: Optional[str] = None):
		self._load_strategies(queue_doc)
		for s in self._strategies:
			s.on_cancel(queue_doc, reason)

	def on_timeout(self, queue_doc):
		self._load_strategies(queue_doc)
		for s in self._strategies:
			s.on_timeout(queue_doc)

	def on_reschedule(self, queue_doc, old_date: str, old_slot: Optional[str] = None):
		self._load_strategies(queue_doc)
		for s in self._strategies:
			s.on_reschedule(queue_doc, old_date, old_slot)

	def on_complete(self, queue_doc):
		self._load_strategies(queue_doc)
		for s in self._strategies:
			s.on_complete(queue_doc)


# Registry of available strategies
_strategy_registry: dict = {}


def register_strategy(cls):
	_strategy_registry[cls.name] = cls
	return cls


def get_strategy(name: str) -> Optional[BaseQueueStrategy]:
	if name in _strategy_registry:
		return _strategy_registry[name]()
	return None


def get_all_strategies() -> list:
	return list(_strategy_registry.keys())


# Register built-in strategies
register_strategy(BaseQueueStrategy)
register_strategy(PenaltyStrategy)
register_strategy(NotificationStrategy)
register_strategy(AuditLogStrategy)
register_strategy(CompositeStrategy)