"""
事件总线：系统内部通信的"神经脉冲"
基于发布-订阅模式，所有模块通过事件总线解耦通信。
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable, Optional

from learning_agent.ai import Event

logger = logging.getLogger(__name__)

EventHandler = Callable[[Event], Awaitable[None]]


class EventBus:
    """
    线程安全的事件总线，支持：
    - 按事件类型订阅
    - 通配符订阅 ('*' 订阅所有事件)
    - 异步广播
    - 历史记录（用于回溯）
    """

    def __init__(self, history_limit: int = 1000):
        self._subscribers: dict[str, list[EventHandler]] = defaultdict(list)
        self._history: list[Event] = []
        self._history_limit = history_limit
        self._lock = asyncio.Lock()

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """订阅指定类型的事件。'*' 表示订阅所有事件。"""
        self._subscribers[event_type].append(handler)
        logger.debug(f"[EventBus] Handler subscribed to '{event_type}'")

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        """取消订阅。"""
        if event_type in self._subscribers:
            try:
                self._subscribers[event_type].remove(handler)
            except ValueError:
                pass

    async def publish(self, event: Event) -> None:
        """
        发布事件，异步通知所有订阅者（包括通配符订阅者）。
        订阅者执行是并行的（gather），但各自异常被隔离。
        """
        async with self._lock:
            self._history.append(event)
            if len(self._history) > self._history_limit:
                self._history.pop(0)

        handlers: list[EventHandler] = []
        handlers.extend(self._subscribers.get(event.type, []))
        handlers.extend(self._subscribers.get("*", []))

        if not handlers:
            return

        logger.debug(f"[EventBus] Publishing '{event.type}' to {len(handlers)} handler(s)")

        async def _invoke(handler: EventHandler) -> None:
            try:
                await handler(event)
            except Exception as e:
                logger.exception(f"[EventBus] Handler error for '{event.type}': {e}")

        await asyncio.gather(*[_invoke(h) for h in handlers], return_exceptions=True)

    def get_history(self, event_type: Optional[str] = None, limit: int = 100) -> list[Event]:
        """获取事件历史，可按类型过滤。"""
        events = self._history
        if event_type:
            events = [e for e in events if e.type == event_type]
        return events[-limit:]

    def clear_history(self) -> None:
        self._history.clear()
