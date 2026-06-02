# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""
事件定义 - 定义系统中所有事件
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional
import uuid


@dataclass
class DocumentEvent:
    """基础文档事件"""

    doc_type: str
    doc_name: str
    doc: Optional[Any] = None
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "doc_type": self.doc_type,
            "doc_name": self.doc_name,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
        }


@dataclass
class SalesOrderSubmitted(DocumentEvent):
    """销售订单提交事件"""

    def __init__(self, doc_type: str, doc_name: str, doc: Optional[Any] = None, **kwargs):
        super().__init__(doc_type="Sales Order", doc_name=doc_name, doc=doc, metadata=kwargs)


@dataclass
class SalesOrderCancelled(DocumentEvent):
    """销售订单取消事件"""

    def __init__(self, doc_type: str, doc_name: str, doc: Optional[Any] = None, **kwargs):
        super().__init__(doc_type="Sales Order", doc_name=doc_name, doc=doc, metadata=kwargs)


@dataclass
class StockReserved(DocumentEvent):
    """库存预留成功事件"""

    stock_reservation_entries: list = field(default_factory=list)

    def __init__(self, doc_type: str, doc_name: str, doc: Optional[Any] = None, **kwargs):
        super().__init__(doc_type="Stock Reservation Entry", doc_name=doc_name, doc=doc, metadata=kwargs)


@dataclass
class StockReservationFailed(DocumentEvent):
    """库存预留失败事件"""

    error: str = ""
    failed_items: list = field(default_factory=list)

    def __init__(self, doc_type: str, doc_name: str, doc: Optional[Any] = None, **kwargs):
        super().__init__(doc_type="Sales Order", doc_name=doc_name, doc=doc, metadata=kwargs)
        self.error = kwargs.get("error", "")
        self.failed_items = kwargs.get("failed_items", [])


@dataclass
class GLPosted(DocumentEvent):
    """会计分录过账成功事件"""

    gl_entries: list = field(default_factory=list)

    def __init__(self, doc_type: str, doc_name: str, doc: Optional[Any] = None, **kwargs):
        super().__init__(doc_type="GL Entry", doc_name=doc_name, doc=doc, metadata=kwargs)


@dataclass
class GLPostFailed(DocumentEvent):
    """会计分录过账失败事件"""

    error: str = ""

    def __init__(self, doc_type: str, doc_name: str, doc: Optional[Any] = None, **kwargs):
        super().__init__(doc_type="Sales Order", doc_name=doc_name, doc=doc, metadata=kwargs)
        self.error = kwargs.get("error", "")


@dataclass
class NotificationSent(DocumentEvent):
    """通知发送成功事件"""

    notification_type: str = ""
    recipients: list = field(default_factory=list)

    def __init__(self, doc_type: str, doc_name: str, doc: Optional[Any] = None, **kwargs):
        super().__init__(doc_type="Notification", doc_name=doc_name, doc=doc, metadata=kwargs)
        self.notification_type = kwargs.get("notification_type", "")
        self.recipients = kwargs.get("recipients", [])
