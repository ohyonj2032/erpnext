from sqlalchemy import Column, Integer, String, DateTime
import datetime
from . import Base

class Inventory(Base):
    __tablename__ = "inventory"
    
    id = Column(Integer, primary_key=True)
    item_code = Column(String(50), unique=True, nullable=False)
    actual_qty = Column(Integer, default=0, nullable=False)
    
    # Optimistic locking field
    version = Column(Integer, default=1, nullable=False)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
    
    __mapper_args__ = {
        "version_id_col": version
    }

class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    
    id = Column(Integer, primary_key=True)
    order_id = Column(String(50), nullable=False)
    account = Column(String(50), nullable=False)
    amount = Column(Integer, nullable=False) # Financial amount (e.g. cents)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
