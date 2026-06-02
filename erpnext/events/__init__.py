# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""
事件系统 - ERPNext 事件驱动架构核心

本模块提供了完整的事件系统，用于解耦各个模块。
"""

from .event_bus import EventBus, subscribe, publish, get_event_bus
from .events import (
    DocumentEvent,
    SalesOrderSubmitted,
    SalesOrderCancelled,
    StockReserved,
    StockReservationFailed,
    GLPosted,
    GLPostFailed,
    NotificationSent,
)
from .handlers import EventHandler

__all__ = [
    "EventBus",
    "subscribe",
    "publish",
    "get_event_bus",
    "DocumentEvent",
    "SalesOrderSubmitted",
    "SalesOrderCancelled",
    "StockReserved",
    "StockReservationFailed",
    "GLPosted",
    "GLPostFailed",
    "NotificationSent",
    "EventHandler",
]
