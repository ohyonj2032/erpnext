import threading
import contextlib
import json
from sqlalchemy.orm import Session
from sqlalchemy.exc import StaleDataError, IntegrityError
from models.order import Order, OrderState, OrderHistory
from models.inventory import Inventory, LedgerEntry

class OrderProcessingError(Exception):
    pass

class IdempotencyError(Exception):
    pass

# Simulated distributed lock for concurrency control
_locks = {}
_lock_mutex = threading.Lock()

@contextlib.contextmanager
def acquire_lock(order_id: str):
    with _lock_mutex:
        if order_id not in _locks:
            _locks[order_id] = threading.Lock()
        lock = _locks[order_id]
    
    acquired = lock.acquire(timeout=5.0)
    if not acquired:
        raise OrderProcessingError(f"Could not acquire lock for order {order_id}")
    try:
        yield
    finally:
        lock.release()

class OrderService:
    def __init__(self, session: Session):
        self.session = session

    def process_order(self, order_id: str, signature: str) -> dict:
        """
        Process the order: deduct inventory and record finance ledger.
        Ensures idempotency and avoids race conditions.
        """
        with acquire_lock(order_id):
            # 1. Fetch Order and check idempotency signature
            order = self.session.query(Order).filter_by(order_id=order_id).first()
            if not order:
                raise OrderProcessingError(f"Order {order_id} not found")

            if order.signature == signature:
                # Idempotency hit: return the last result
                return order.last_result
            
            if order.signature is not None and order.signature != signature:
                # If a different signature is already processed and order is not in a retryable state
                if order.status in [OrderState.COMPLETED, OrderState.PROCESSING]:
                    raise IdempotencyError("Order already processed with a different signature")

            # 2. State Machine Check
            if order.status not in [OrderState.DRAFT, OrderState.FAILED]:
                raise OrderProcessingError(f"Cannot process order in state {order.status.value}")

            # State transition to PROCESSING
            self._transition_state(order, OrderState.PROCESSING, "Started processing")
            
            try:
                # 3. Inventory Deduction & Financial Ledger (Transactional)
                total_amount = 0
                for item in order.items:
                    # Fetch inventory with optimistic locking (version field is handled by SQLAlchemy)
                    inventory = self.session.query(Inventory).filter_by(item_code=item.item_code).first()
                    if not inventory:
                        raise OrderProcessingError(f"Inventory not found for {item.item_code}")
                    
                    if inventory.actual_qty < item.qty:
                        raise OrderProcessingError(f"Insufficient stock for {item.item_code}")
                    
                    inventory.actual_qty -= item.qty
                    
                    # Calculate amount (mock calculation: qty * 100)
                    amount = item.qty * 100
                    total_amount += amount

                # Record ledger
                ledger = LedgerEntry(
                    order_id=order_id,
                    account="sales",
                    amount=total_amount
                )
                self.session.add(ledger)
                
                # 4. State transition to COMPLETED
                self._transition_state(order, OrderState.COMPLETED, "Processing successful")
                
                # Save Idempotency Result
                result = {"status": "success", "order_id": order_id, "amount_recorded": total_amount}
                order.signature = signature
                order.last_result = result

                self.session.commit()
                return result

            except Exception as e:
                self.session.rollback()
                
                # We need a new transaction to save the FAILED state
                order = self.session.query(Order).filter_by(order_id=order_id).first()
                if order:
                    self._transition_state(order, OrderState.FAILED, str(e))
                    order.signature = signature
                    order.last_result = {"status": "error", "message": str(e)}
                    self.session.commit()
                
                if isinstance(e, StaleDataError):
                    raise OrderProcessingError("Concurrency conflict during inventory deduction (Optimistic Lock Failed)")
                raise OrderProcessingError(f"Processing failed: {str(e)}")

    def _transition_state(self, order: Order, new_state: OrderState, reason: str):
        old_state = order.status
        order.status = new_state
        history = OrderHistory(
            order=order,
            previous_state=old_state,
            new_state=new_state,
            reason=reason
        )
        self.session.add(history)
