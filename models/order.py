from sqlalchemy import Column, Integer, String, Enum, ForeignKey, JSON, DateTime
from sqlalchemy.orm import relationship
import enum
import datetime
from . import Base

class OrderState(enum.Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

class Order(Base):
    __tablename__ = "orders"
    
    id = Column(Integer, primary_key=True)
    order_id = Column(String(50), unique=True, nullable=False)
    status = Column(Enum(OrderState), default=OrderState.DRAFT, nullable=False)
    
    # Idempotency fields
    signature = Column(String(100), unique=True, nullable=True) # Request signature
    last_result = Column(JSON, nullable=True) # Result of last idempotent operation
    
    # Audit trail / Replayable state change history
    history = relationship("OrderHistory", back_populates="order", cascade="all, delete-orphan", order_by="OrderHistory.id")
    
    # Items
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")

class OrderItem(Base):
    __tablename__ = "order_items"
    
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    item_code = Column(String(50), nullable=False)
    qty = Column(Integer, nullable=False)
    
    order = relationship("Order", back_populates="items")

class OrderHistory(Base):
    __tablename__ = "order_history"
    
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    previous_state = Column(Enum(OrderState), nullable=True)
    new_state = Column(Enum(OrderState), nullable=False)
    changed_at = Column(DateTime, default=datetime.datetime.utcnow)
    reason = Column(String(200), nullable=True)
    
    order = relationship("Order", back_populates="history")
