# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""
事件处理器 - 提供基础处理器类和装饰器
"""

from abc import ABC, abstractmethod
from typing import Callable, Type
from .events import DocumentEvent
from .event_bus import get_event_bus


class EventHandler(ABC):
    """事件处理器基类"""

    def __init__(self, event_class: Type[DocumentEvent]):
        self.event_class = event_class
        self.subscribe()

    def subscribe(self):
        """订阅事件"""
        get_event_bus().subscribe(self.event_class, self.handle)

    @abstractmethod
    def handle(self, event: DocumentEvent):
        """处理事件的抽象方法"""
        pass


def event_handler(event_class: Type[DocumentEvent]):
    """事件处理器装饰器"""

    def decorator(func: Callable):
        get_event_bus().subscribe(event_class, func)
        return func

    return decorator


def execute_handler(handler: Callable, event: DocumentEvent):
    """执行事件处理器（用于异步执行）"""
    handler(event)
