from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Any

STATUS_OPTIONS = [
	"",
	"Draft",
	"On Hold",
	"To Pay",
	"To Deliver and Bill",
	"To Bill",
	"To Deliver",
	"Completed",
	"Cancelled",
	"Closed",
	"Inventory Locked",
	"Inventory Adjusted",
	"Partly Fulfilled",
	"Fulfillment Failed",
]


class OrderState(str, Enum):
	DRAFT = "Draft"
	ON_HOLD = "On Hold"
	TO_PAY = "To Pay"
	TO_DELIVER_AND_BILL = "To Deliver and Bill"
	TO_BILL = "To Bill"
	TO_DELIVER = "To Deliver"
	COMPLETED = "Completed"
	CANCELLED = "Cancelled"
	CLOSED = "Closed"
	INVENTORY_LOCKED = "Inventory Locked"
	INVENTORY_ADJUSTED = "Inventory Adjusted"
	PARTLY_FULFILLED = "Partly Fulfilled"
	FULFILLMENT_FAILED = "Fulfillment Failed"

	@classmethod
	def from_frappe_status(cls, status: str) -> OrderState:
		mapping = {
			"Draft": cls.DRAFT,
			"On Hold": cls.ON_HOLD,
			"To Pay": cls.TO_PAY,
			"To Deliver and Bill": cls.TO_DELIVER_AND_BILL,
			"To Bill": cls.TO_BILL,
			"To Deliver": cls.TO_DELIVER,
			"Completed": cls.COMPLETED,
			"Cancelled": cls.CANCELLED,
			"Closed": cls.CLOSED,
		}
		return mapping.get(status, cls.DRAFT)

	def to_frappe_status(self) -> str:
		return self.value


class OrderStateMachine:
	TRANSITIONS: dict[OrderState, set[OrderState]] = {
		OrderState.DRAFT: {
			OrderState.ON_HOLD,
			OrderState.TO_DELIVER_AND_BILL,
			OrderState.TO_BILL,
			OrderState.TO_DELIVER,
			OrderState.CANCELLED,
		},
		OrderState.ON_HOLD: {
			OrderState.DRAFT,
			OrderState.CLOSED,
			OrderState.CANCELLED,
		},
		OrderState.TO_DELIVER_AND_BILL: {
			OrderState.TO_BILL,
			OrderState.TO_DELIVER,
			OrderState.COMPLETED,
			OrderState.CLOSED,
			OrderState.ON_HOLD,
			OrderState.INVENTORY_LOCKED,
		},
		OrderState.TO_BILL: {
			OrderState.COMPLETED,
			OrderState.CLOSED,
			OrderState.ON_HOLD,
		},
		OrderState.TO_DELIVER: {
			OrderState.TO_DELIVER_AND_BILL,
			OrderState.COMPLETED,
			OrderState.CLOSED,
			OrderState.ON_HOLD,
		},
		OrderState.COMPLETED: {
			OrderState.CLOSED,
		},
		OrderState.INVENTORY_LOCKED: {
			OrderState.INVENTORY_ADJUSTED,
			OrderState.FULFILLMENT_FAILED,
			OrderState.TO_DELIVER_AND_BILL,
			OrderState.PARTLY_FULFILLED,
		},
		OrderState.INVENTORY_ADJUSTED: {
			OrderState.COMPLETED,
			OrderState.FULFILLMENT_FAILED,
			OrderState.PARTLY_FULFILLED,
		},
		OrderState.PARTLY_FULFILLED: {
			OrderState.COMPLETED,
			OrderState.INVENTORY_ADJUSTED,
			OrderState.FULFILLMENT_FAILED,
		},
		OrderState.FULFILLMENT_FAILED: {
			OrderState.TO_DELIVER_AND_BILL,
			OrderState.ON_HOLD,
			OrderState.CLOSED,
		},
		OrderState.CANCELLED: set(),
		OrderState.CLOSED: set(),
	}

	@classmethod
	def can_transition(cls, current: OrderState, target: OrderState) -> bool:
		return target in cls.TRANSITIONS.get(current, set())

	@classmethod
	def validate_transition(cls, current: OrderState, target: OrderState) -> None:
		if not cls.can_transition(current, target):
			from frappe import _

			raise InvalidStateTransitionError(
				_("Cannot transition from {0} to {1}").format(
					current.value, target.value
				)
			)

	@classmethod
	def get_allowed_transitions(cls, current: OrderState) -> set[OrderState]:
		return cls.TRANSITIONS.get(current, set())


class InvalidStateTransitionError(Exception):
	pass


class OrderEvent:
	def __init__(
		self,
		order_id: str,
		event_type: str,
		previous_state: OrderState | None = None,
		new_state: OrderState | None = None,
		payload: dict[str, Any] | None = None,
		result: dict[str, Any] | None = None,
		timestamp: datetime | None = None,
		signature: str | None = None,
	):
		self.order_id = order_id
		self.event_type = event_type
		self.previous_state = previous_state
		self.new_state = new_state
		self.payload = payload or {}
		self.result = result or {}
		self.timestamp = timestamp or datetime.utcnow()
		self.signature = signature or self._compute_signature()

	def _compute_signature(self) -> str:
		canonical = json.dumps(
			{
				"order_id": self.order_id,
				"event_type": self.event_type,
				"payload": self.payload,
			},
			sort_keys=True,
			default=str,
		)
		return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

	def to_dict(self) -> dict[str, Any]:
		return {
			"order_id": self.order_id,
			"event_type": self.event_type,
			"previous_state": self.previous_state.value if self.previous_state else None,
			"new_state": self.new_state.value if self.new_state else None,
			"payload": self.payload,
			"result": self.result,
			"timestamp": self.timestamp.isoformat(),
			"signature": self.signature,
		}

	@classmethod
	def from_dict(cls, data: dict[str, Any]) -> OrderEvent:
		return cls(
			order_id=data["order_id"],
			event_type=data["event_type"],
			previous_state=OrderState(data["previous_state"]) if data.get("previous_state") else None,
			new_state=OrderState(data["new_state"]) if data.get("new_state") else None,
			payload=data.get("payload", {}),
			result=data.get("result", {}),
			timestamp=datetime.fromisoformat(data["timestamp"]) if data.get("timestamp") else None,
			signature=data.get("signature"),
		)


class OrderChangeLog:
	"""Replayable change log for order status transitions."""

	def __init__(self, order_id: str):
		self.order_id = order_id
		self.events: list[OrderEvent] = []

	def append(self, event: OrderEvent) -> None:
		self.events.append(event)

	def replay(self) -> list[OrderEvent]:
		return list(self.events)

	def last_event(self) -> OrderEvent | None:
		return self.events[-1] if self.events else None

	def to_json(self) -> str:
		return json.dumps([e.to_dict() for e in self.events], indent=2)

	@classmethod
	def from_json(cls, order_id: str, json_str: str) -> OrderChangeLog:
		log = cls(order_id)
		events = json.loads(json_str)
		for e in events:
			log.append(OrderEvent.from_dict(e))
		return log