# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""
事件总线 - 实现发布/订阅模式
"""

import frappe
from typing import Callable, Dict, List, Type
import traceback
from .events import DocumentEvent


class EventBus:
    """事件总线类"""

    _instance: "EventBus" = None
    _subscribers: Dict[str, List[Callable]] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._subscribers = {}
        return cls._instance

    def subscribe(self, event_class: Type[DocumentEvent], handler: Callable) -> None:
        """订阅事件"""
        event_name = event_class.__name__
        if event_name not in self._subscribers:
            self._subscribers[event_name] = []
        if handler not in self._subscribers[event_name]:
            self._subscribers[event_name].append(handler)

    def unsubscribe(self, event_class: Type[DocumentEvent], handler: Callable) -> None:
        """取消订阅事件"""
        event_name = event_class.__name__
        if event_name in self._subscribers:
            if handler in self._subscribers[event_name]:
                self._subscribers[event_name].remove(handler)

    def publish(self, event: DocumentEvent, async_mode: bool = False) -> None:
        """发布事件"""
        event_name = event.__class__.__name__
        if event_name not in self._subscribers:
            return

        for handler in self._subscribers[event_name]:
            try:
                if async_mode:
                    frappe.enqueue(
                        method="erpnext.events.handlers.execute_handler",
                        queue="short",
                        timeout=300,
                        handler=handler,
                        event=event,
                    )
                else:
                    handler(event)
            except Exception as e:
                frappe.log_error(
                    title=f"Event Handler Error: {handler.__name__}",
                    message=traceback.format_exc(),
                )
                # 记录但不抛出，确保一个处理器失败不影响其他处理器


# 单例访问
_event_bus = EventBus()


def get_event_bus() -> EventBus:
    """获取事件总线单例"""
    return _event_bus


def subscribe(event_class: Type[DocumentEvent], handler: Callable) -> None:
    """订阅事件的便捷方法"""
    get_event_bus().subscribe(event_class, handler)


def publish(event: DocumentEvent, async_mode: bool = False) -> None:
    """发布事件的便捷方法"""
    get_event_bus().publish(event, async_mode)
