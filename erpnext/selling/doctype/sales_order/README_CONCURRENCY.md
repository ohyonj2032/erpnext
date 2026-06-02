# Order & Inventory Concurrency Control

This implementation provides concurrency control, idempotency, and state machine validation for order and inventory operations in ERPNext.

## Key Features

1. **Distributed Locking**: Prevents concurrent modifications to the same order
2. **Idempotent Operations**: Ensures repeated calls don't cause duplicate effects
3. **State Machine**: Validates all status transitions
4. **Optimistic Locking**: Prevents race conditions in inventory updates
5. **Atomic Transactions**: Ensures order and inventory operations stay consistent
6. **Audit Trail**: Records all state changes

## File Changes

### New Files
1. `erpnext/selling/doctype/sales_order/order_service.py` - Service layer with concurrency control
2. `erpnext/tests/functional/test_order_inventory_race.py` - Concurrency tests
3. `erpnext/tests/fixtures/initial_data.json` - Test data fixtures
4. `erpnext/selling/doctype/sales_order/README_CONCURRENCY.md` - This document

### Modified Files
1. `erpnext/stock/doctype/bin/bin.py` - Added version field for optimistic locking

## Database Tables

Three new tables are created:
- `tabOrder Lock`: Distributed locking mechanism
- `tabOrder Idempotency Record`: Tracks idempotent operation results
- `tabOrder State Change`: Audit trail for order status changes
- Added `version` column to `tabBin` for optimistic locking

## Usage Examples

### 1. Update Order Status (with Locking)

```python
from erpnext.selling.doctype.sales_order.order_service import OrderService

# Update order status safely with automatic locking
result = OrderService.update_order_status(
    order_name="SO-00001",
    new_status="To Deliver and Bill"
)
```

### 2. Idempotent Operations

```python
from erpnext.selling.doctype.sales_order.order_service import (
    OrderService,
    generate_idempotency_key
)

# Generate an idempotency key
idempotency_key = generate_idempotency_key("SO-00001", "update_status")

# First call - executes the operation
result1 = OrderService.update_order_status(
    order_name="SO-00001",
    new_status="To Deliver",
    idempotency_key=idempotency_key
)

# Second call with same key - returns cached result without re-executing
result2 = OrderService.update_order_status(
    order_name="SO-00001",
    new_status="To Deliver",
    idempotency_key=idempotency_key
)
```

### 3. Process Order with Inventory

```python
from erpnext.selling.doctype.sales_order.order_service import OrderService

# Atomic operation: updates order status AND deducts inventory in single transaction
result = OrderService.process_order_with_inventory(
    order_name="SO-00001"
)
```

## Running Tests

### Functional Concurrency Tests

```bash
# Run all concurrency tests
bench run-tests erpnext.tests.functional.test_order_inventory_race

# Run specific test
bench run-tests erpnext.tests.functional.test_order_inventory_race.TestOrderInventoryRace.test_concurrent_inventory_deduction
```

### Test Coverage

- Concurrent status updates with locking
- Idempotent operation handling
- Invalid state transition validation
- Concurrent inventory deduction
- Locking mechanism behavior
- Full state machine transitions

## State Machine Diagram

```
Draft <--> On Hold
  |           |
  v           v
To Deliver and Bill <--> To Bill
  |
  v
To Deliver
  |
  v
Completed --> Closed <--> Draft
  ^
Cancelled (terminal state)
```

## Performance Considerations

- Lock timeout: 30 seconds (configurable)
- Optimistic locking minimizes contention
- Idempotency records prevent duplicate processing
- Database-level transactions ensure atomicity

## Monitoring

Key metrics to monitor:
- Lock acquisition time
- Lock wait timeouts
- Optimistic lock failures
- Idempotent request rate
- State transition distribution

## Rollback and Recovery

All critical operations are wrapped in transactions. If any step fails, the entire operation is rolled back to maintain consistency.

The order state change history provides a complete audit trail for debugging and recovery.

## Migration

To set up the database tables:

```python
from erpnext.selling.doctype.sales_order.order_service import setup_order_tables

setup_order_tables()
```
