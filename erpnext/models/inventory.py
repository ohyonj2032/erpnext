from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from frappe import _
from frappe.query_builder import DocType
from frappe.utils import now


class OptimisticLockError(Exception):
	def __init__(self, item_code: str, warehouse: str, expected_version: int, actual_version: int):
		self.item_code = item_code
		self.warehouse = warehouse
		self.expected_version = expected_version
		self.actual_version = actual_version
		super().__init__(
			_(
				"Optimistic lock conflict for item {0} in warehouse {1}: "
				"expected version {2}, got {3}. Please retry."
			).format(item_code, warehouse, expected_version, actual_version)
		)


class InsufficientStockError(Exception):
	def __init__(self, item_code: str, warehouse: str, requested: float, available: float):
		self.item_code = item_code
		self.warehouse = warehouse
		self.requested = requested
		self.available = available
		super().__init__(
			_(
				"Insufficient stock for item {0} in warehouse {1}: "
				"requested {2}, available {3}"
			).format(item_code, warehouse, requested, available)
		)


@dataclass
class InventoryChange:
	item_code: str
	warehouse: str
	quantity: float
	change_type: str
	reference_doctype: str | None = None
	reference_name: str | None = None
	timestamp: datetime | None = None
	version: int = 0
	transaction_id: str | None = None

	def to_dict(self) -> dict[str, Any]:
		return {
			"item_code": self.item_code,
			"warehouse": self.warehouse,
			"quantity": self.quantity,
			"change_type": self.change_type,
			"reference_doctype": self.reference_doctype,
			"reference_name": self.reference_name,
			"timestamp": self.timestamp.isoformat() if self.timestamp else None,
			"version": self.version,
			"transaction_id": self.transaction_id,
		}


@dataclass
class InventoryAdjustment:
	deductions: list[InventoryChange] = field(default_factory=list)
	additions: list[InventoryChange] = field(default_factory=list)
	transaction_id: str = ""
	financial_entry_id: str | None = None

	@property
	def total_deduction(self) -> float:
		return sum(abs(d.quantity) for d in self.deductions)

	@property
	def total_addition(self) -> float:
		return sum(a.quantity for a in self.additions)

	def to_dict(self) -> dict[str, Any]:
		return {
			"deductions": [d.to_dict() for d in self.deductions],
			"additions": [a.to_dict() for a in self.additions],
			"transaction_id": self.transaction_id,
			"financial_entry_id": self.financial_entry_id,
		}

	@classmethod
	def from_dict(cls, data: dict[str, Any]) -> InventoryAdjustment:
		deductions = [InventoryChange(**d) for d in data.get("deductions", [])]
		additions = [InventoryChange(**a) for a in data.get("additions", [])]
		return cls(
			deductions=deductions,
			additions=additions,
			transaction_id=data.get("transaction_id", ""),
			financial_entry_id=data.get("financial_entry_id"),
		)


class InventoryService:
	RETRY_LIMIT = 3

	@classmethod
	def get_bin_version(cls, item_code: str, warehouse: str) -> int:
		import frappe

		result = frappe.db.get_value(
			"Bin",
			{"item_code": item_code, "warehouse": warehouse},
			"modified",
		)
		if not result:
			return 0
		return hash(str(result)) & 0x7FFFFFFF

	@classmethod
	def reserve_stock(
		cls,
		item_code: str,
		warehouse: str,
		quantity: float,
		reference_doctype: str,
		reference_name: str,
		transaction_id: str,
	) -> InventoryChange:
		import frappe

		from erpnext.stock.doctype.bin.bin import get_actual_qty
		from erpnext.stock.stock_balance import get_reserved_qty

		bin_table = DocType("Bin")
		current_version = cls.get_bin_version(item_code, warehouse)
		actual_qty = get_actual_qty(item_code, warehouse)
		reserved_qty = get_reserved_qty(item_code, warehouse)

		available = actual_qty - reserved_qty
		if available < quantity:
			raise InsufficientStockError(item_code, warehouse, quantity, available)

		new_reserved_qty = reserved_qty + quantity

		bin_exists = frappe.db.exists("Bin", {"item_code": item_code, "warehouse": warehouse})
		if bin_exists:
			result = (
				frappe.qb.update(bin_table)
				.set(bin_table.reserved_qty, new_reserved_qty)
				.where(bin_table.item_code == item_code)
				.where(bin_table.warehouse == warehouse)
				.where(bin_table.reserved_qty == reserved_qty)
			).run()

			if result == 0:
				raise OptimisticLockError(
					item_code, warehouse, reserved_qty, new_reserved_qty
				)
		else:
			from erpnext.stock.utils import get_or_make_bin

			get_or_make_bin(item_code, warehouse)
			frappe.db.set_value(
				"Bin",
				{"item_code": item_code, "warehouse": warehouse},
				"reserved_qty",
				new_reserved_qty,
			)

		return InventoryChange(
			item_code=item_code,
			warehouse=warehouse,
			quantity=-quantity,
			change_type="reserve",
			reference_doctype=reference_doctype,
			reference_name=reference_name,
			timestamp=datetime.utcnow(),
			version=current_version + 1,
			transaction_id=transaction_id,
		)

	@classmethod
	def deduct_stock(
		cls,
		item_code: str,
		warehouse: str,
		quantity: float,
		reference_doctype: str,
		reference_name: str,
		transaction_id: str,
		voucher_type: str = "Delivery Note",
	) -> InventoryChange:
		import frappe
		from frappe.utils import flt

		from erpnext.stock.doctype.bin.bin import get_actual_qty

		bin_table = DocType("Bin")
		current_version = cls.get_bin_version(item_code, warehouse)
		actual_qty = get_actual_qty(item_code, warehouse)

		if flt(actual_qty) < flt(quantity):
			allow_negative = frappe.get_single_value(
				"Stock Settings", "allow_negative_stock"
			)
			if not allow_negative:
				raise InsufficientStockError(
					item_code, warehouse, quantity, actual_qty
				)

		new_actual_qty = flt(actual_qty) - flt(quantity)

		bin_exists = frappe.db.exists(
			"Bin", {"item_code": item_code, "warehouse": warehouse}
		)
		if bin_exists:
			result = (
				frappe.qb.update(bin_table)
				.set(bin_table.actual_qty, new_actual_qty)
				.where(bin_table.item_code == item_code)
				.where(bin_table.warehouse == warehouse)
				.where(bin_table.actual_qty == actual_qty)
			).run()

			if result == 0:
				raise OptimisticLockError(
					item_code, warehouse, actual_qty, new_actual_qty
				)

		change = InventoryChange(
			item_code=item_code,
			warehouse=warehouse,
			quantity=-quantity,
			change_type="deduct",
			reference_doctype=reference_doctype,
			reference_name=reference_name,
			timestamp=datetime.utcnow(),
			version=current_version + 1,
			transaction_id=transaction_id,
		)
		return change

	@classmethod
	def add_stock(
		cls,
		item_code: str,
		warehouse: str,
		quantity: float,
		reference_doctype: str,
		reference_name: str,
		transaction_id: str,
	) -> InventoryChange:
		import frappe
		from frappe.utils import flt

		from erpnext.stock.doctype.bin.bin import get_actual_qty

		bin_table = DocType("Bin")
		current_version = cls.get_bin_version(item_code, warehouse)
		actual_qty = get_actual_qty(item_code, warehouse)

		new_actual_qty = flt(actual_qty) + flt(quantity)

		bin_exists = frappe.db.exists(
			"Bin", {"item_code": item_code, "warehouse": warehouse}
		)
		if bin_exists:
			frappe.qb.update(bin_table).set(
				bin_table.actual_qty, new_actual_qty
			).where(bin_table.item_code == item_code).where(
				bin_table.warehouse == warehouse
			).where(
				bin_table.actual_qty == actual_qty
			).run()

		return InventoryChange(
			item_code=item_code,
			warehouse=warehouse,
			quantity=quantity,
			change_type="add",
			reference_doctype=reference_doctype,
			reference_name=reference_name,
			timestamp=datetime.utcnow(),
			version=current_version + 1,
			transaction_id=transaction_id,
		)

	@classmethod
	def execute_adjustment(cls, adjustment: InventoryAdjustment) -> InventoryAdjustment:
		import frappe

		executed = InventoryAdjustment(transaction_id=adjustment.transaction_id)
		savepoint = f"inv_adjust_{adjustment.transaction_id}"

		try:
			frappe.db.savepoint(savepoint)

			for change in adjustment.deductions:
				result = cls.deduct_stock(
					item_code=change.item_code,
					warehouse=change.warehouse,
					quantity=abs(change.quantity),
					reference_doctype=change.reference_doctype or "",
					reference_name=change.reference_name or "",
					transaction_id=adjustment.transaction_id,
				)
				executed.deductions.append(result)

			for change in adjustment.additions:
				result = cls.add_stock(
					item_code=change.item_code,
					warehouse=change.warehouse,
					quantity=change.quantity,
					reference_doctype=change.reference_doctype or "",
					reference_name=change.reference_name or "",
					transaction_id=adjustment.transaction_id,
				)
				executed.additions.append(result)

			frappe.db.release_savepoint(savepoint)
		except (OptimisticLockError, InsufficientStockError):
			frappe.db.rollback(save_point=savepoint)
			raise
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			raise

		return executed

	@classmethod
	def retry_deduct(
		cls,
		item_code: str,
		warehouse: str,
		quantity: float,
		reference_doctype: str,
		reference_name: str,
		transaction_id: str,
		max_retries: int = RETRY_LIMIT,
	) -> InventoryChange:
		for attempt in range(1, max_retries + 1):
			try:
				return cls.deduct_stock(
					item_code=item_code,
					warehouse=warehouse,
					quantity=quantity,
					reference_doctype=reference_doctype,
					reference_name=reference_name,
					transaction_id=transaction_id,
				)
			except OptimisticLockError:
				if attempt == max_retries:
					raise
		raise OptimisticLockError(item_code, warehouse, 0, 0)

	@classmethod
	def get_stock_summary(
		cls, item_code: str, warehouses: list[str] | None = None
	) -> dict[str, Any]:
		import frappe

		from erpnext.stock.doctype.bin.bin import get_actual_qty
		from erpnext.stock.stock_balance import get_reserved_qty

		filters = {"item_code": item_code}
		if warehouses:
			filters["warehouse"] = ["in", warehouses]

		bins = frappe.get_all(
			"Bin",
			filters=filters,
			fields=["item_code", "warehouse", "actual_qty", "reserved_qty"],
		)

		result: dict[str, Any] = {}
		for b in bins:
			result[b.warehouse] = {
				"actual_qty": b.actual_qty,
				"reserved_qty": b.reserved_qty,
				"available_qty": b.actual_qty - b.reserved_qty,
			}
		return result

	@classmethod
	def validate_for_reconciliation(
		cls, adjustment: InventoryAdjustment
	) -> dict[str, Any]:
		is_balanced = True
		inconsistencies: list[dict[str, Any]] = []

		for d in adjustment.deductions:
			available = cls.get_stock_summary(d.item_code, [d.warehouse])
			wh_info = available.get(d.warehouse, {})
			if wh_info.get("available_qty", 0) < abs(d.quantity):
				is_balanced = False
				inconsistencies.append(
					{
						"item_code": d.item_code,
						"warehouse": d.warehouse,
						"requested": abs(d.quantity),
						"available": wh_info.get("available_qty", 0),
						"type": "insufficient_stock",
					}
				)

		return {"balanced": is_balanced, "inconsistencies": inconsistencies}