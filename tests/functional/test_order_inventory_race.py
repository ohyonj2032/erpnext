import pytest
import threading
import json
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import Base
from models.order import Order, OrderItem, OrderState, OrderHistory
from models.inventory import Inventory, LedgerEntry
from services.order_service import OrderService, OrderProcessingError

# Use an in-memory SQLite database, allowing multi-threading
engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=pytest.importorskip("sqlalchemy.pool").StaticPool
)
SessionLocal = sessionmaker(bind=engine, autoflush=False)

@pytest.fixture(scope="function")
def db_session():
    Base.metadata.create_all(engine)
    session = SessionLocal()
    
    # Load fixtures
    fixture_path = os.path.join(os.path.dirname(__file__), "../fixtures/initial_data.json")
    with open(fixture_path, "r") as f:
        data = json.load(f)
        
        for inv_data in data["inventory"]:
            session.add(Inventory(**inv_data))
            
        for order_data in data["orders"]:
            order = Order(order_id=order_data["order_id"])
            session.add(order)
            for item_data in order_data["items"]:
                session.add(OrderItem(order=order, item_code=item_data["item_code"], qty=item_data["qty"]))
    
    session.commit()
    yield session
    session.close()
    Base.metadata.drop_all(engine)

def test_idempotent_submission(db_session):
    service = OrderService(db_session)
    order_id = "ORD-2023-001"
    sig = "sig-12345"
    
    # First submission
    res1 = service.process_order(order_id, sig)
    assert res1["status"] == "success"
    
    # Duplicate submission with same signature
    res2 = service.process_order(order_id, sig)
    assert res1 == res2
    
    # Inventory should only be deducted once
    inv = db_session.query(Inventory).filter_by(item_code="ITEM-001").first()
    assert inv.actual_qty == 90 # 100 - 10
    
    # Check history
    order = db_session.query(Order).filter_by(order_id=order_id).first()
    assert len(order.history) == 2 # DRAFT -> PROCESSING, PROCESSING -> COMPLETED

def test_inventory_insufficient_rollback(db_session):
    service = OrderService(db_session)
    order_id = "ORD-2023-002"
    
    # Make inventory insufficient intentionally
    inv = db_session.query(Inventory).filter_by(item_code="ITEM-001").first()
    inv.actual_qty = 50
    db_session.commit()
    
    with pytest.raises(OrderProcessingError, match="Insufficient stock"):
        service.process_order(order_id, "sig-fail")
        
    # Check state and rollback
    order = db_session.query(Order).filter_by(order_id=order_id).first()
    assert order.status == OrderState.FAILED
    assert order.last_result["status"] == "error"
    
    # Inventory should remain untouched (rolled back)
    db_session.refresh(inv)
    assert inv.actual_qty == 50
    
    # Ledger should be empty
    ledgers = db_session.query(LedgerEntry).filter_by(order_id=order_id).all()
    assert len(ledgers) == 0

def test_concurrent_processing_race_condition(db_session):
    # This tests the pessimistic lock ensuring two threads cannot process the same order simultaneously
    # and also tests optimistic locking if two different orders try to deduct the same inventory
    
    # Let's create two new orders that compete for the same inventory
    # ITEM-002 has 50 qty. Order A needs 30, Order B needs 30. Total 60 > 50.
    
    order_a = Order(order_id="ORD-A")
    db_session.add(order_a)
    db_session.add(OrderItem(order=order_a, item_code="ITEM-002", qty=30))
    
    order_b = Order(order_id="ORD-B")
    db_session.add(order_b)
    db_session.add(OrderItem(order=order_b, item_code="ITEM-002", qty=30))
    
    db_session.commit()
    
    results = []
    
    def process_a():
        session = SessionLocal()
        svc = OrderService(session)
        try:
            res = svc.process_order("ORD-A", "sig-A")
            results.append(("A", "success"))
        except Exception as e:
            results.append(("A", str(e)))
        finally:
            session.close()

    def process_b():
        session = SessionLocal()
        svc = OrderService(session)
        try:
            res = svc.process_order("ORD-B", "sig-B")
            results.append(("B", "success"))
        except Exception as e:
            results.append(("B", str(e)))
        finally:
            session.close()
            
    t1 = threading.Thread(target=process_a)
    t2 = threading.Thread(target=process_b)
    
    t1.start()
    t2.start()
    
    t1.join()
    t2.join()
    
    # One should succeed, one should fail due to insufficient stock or concurrent update
    successes = [r for r in results if r[1] == "success"]
    failures = [r for r in results if r[1] != "success"]
    
    assert len(successes) == 1
    assert len(failures) == 1
    assert "Insufficient stock" in failures[0][1] or "Concurrency conflict" in failures[0][1]
    
    # Verify final inventory
    inv = db_session.query(Inventory).filter_by(item_code="ITEM-002").first()
    assert inv.actual_qty == 20 # 50 - 30 = 20
