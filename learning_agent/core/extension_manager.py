"""
扩展管理器：管理扩展的生命周期、加载、激活、停用。
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from learning_agent.core.event_bus import EventBus
from learning_agent.core.hook_system import HookPoint, HookSystem
from learning_agent.core.tool_registry import ToolRegistry
from learning_agent.models import Event, ToolDefinition

logger = logging.getLogger(__name__)

ActivateFn = Callable[["ExtensionContext"], Awaitable[None]]
DeactivateFn = Callable[[], Awaitable[None]]


class ExtensionContext:
    """
    每个扩展在激活时收到的上下文对象。
    扩展通过此对象注册 Hook、订阅事件、注册工具。
    """

    def __init__(
        self,
        extension_id: str,
        hook_system: HookSystem,
        event_bus: EventBus,
        tool_registry: ToolRegistry,
        config: dict[str, Any],
    ):
        self.extension_id = extension_id
        self._hook_system = hook_system
        self._event_bus = event_bus
        self._tool_registry = tool_registry
        self.config = config

    def register_hook(
        self,
        point: HookPoint | str,
        handler: Callable[..., Awaitable[Any]],
        priority: int = 0,
    ) -> None:
        self._hook_system.register(point, handler, priority, self.extension_id)

    def subscribe_event(self, event_type: str, handler: Callable[[Event], Awaitable[None]]) -> None:
        self._event_bus.subscribe(event_type, handler)

    def publish_event(self, event: Event) -> None:
        asyncio = __import__("asyncio")
        asyncio.create_task(self._event_bus.publish(event))

    def register_tool(self, tool_def: ToolDefinition, handler: Callable[..., Awaitable[Any]]) -> None:
        self._tool_registry.register(tool_def, handler, self.extension_id)


class Extension:
    """
    扩展的抽象接口。
    """

    def __init__(
        self,
        id: str,
        name: str,
        version: str = "0.1.0",
        type: str = "builtin",
        dependencies: Optional[list[str]] = None,
        config: Optional[dict[str, Any]] = None,
    ):
        self.id = id
        self.name = name
        self.version = version
        self.type = type
        self.dependencies = dependencies or []
        self.config = config or {}
        self._activate_fn: Optional[ActivateFn] = None
        self._deactivate_fn: Optional[DeactivateFn] = None
        self._is_active = False

    def on_activate(self, fn: ActivateFn) -> "Extension":
        self._activate_fn = fn
        return self

    def on_deactivate(self, fn: DeactivateFn) -> "Extension":
        self._deactivate_fn = fn
        return self

    async def activate(self, context: ExtensionContext) -> None:
        if self._activate_fn:
            await self._activate_fn(context)
        self._is_active = True
        logger.info(f"[Extension] Activated '{self.id}' v{self.version}")

    async def deactivate(self) -> None:
        if self._deactivate_fn:
            await self._deactivate_fn()
        self._is_active = False
        logger.info(f"[Extension] Deactivated '{self.id}'")


class ExtensionManager:
    """
    扩展管理器：
    - 维护扩展列表
    - 处理依赖解析与拓扑排序
    - 控制扩展的激活/停用生命周期
    """

    def __init__(
        self,
        hook_system: HookSystem,
        event_bus: EventBus,
        tool_registry: ToolRegistry,
    ):
        self._hook_system = hook_system
        self._event_bus = event_bus
        self._tool_registry = tool_registry
        self._extensions: dict[str, Extension] = {}

    def register(self, extension: Extension) -> None:
        """注册一个扩展（尚未激活）。"""
        self._extensions[extension.id] = extension
        logger.info(f"[ExtensionManager] Registered extension '{extension.id}'")

    def unregister(self, extension_id: str) -> None:
        """注销并停用扩展。"""
        ext = self._extensions.pop(extension_id, None)
        if ext and ext._is_active:
            asyncio = __import__("asyncio")
            asyncio.create_task(ext.deactivate())

    async def activate_all(self) -> None:
        """
        按依赖拓扑排序后，逐个激活所有扩展。
        """
        order = self._resolve_dependencies()
        for ext_id in order:
            ext = self._extensions[ext_id]
            ctx = ExtensionContext(
                extension_id=ext.id,
                hook_system=self._hook_system,
                event_bus=self._event_bus,
                tool_registry=self._tool_registry,
                config=ext.config,
            )
            await ext.activate(ctx)
            await self._event_bus.publish(
                Event(
                    type="extension.activated",
                    payload={"extension_id": ext.id, "version": ext.version},
                    source="extension_manager",
                )
            )

    async def deactivate_all(self) -> None:
        """停用所有扩展（逆序）。"""
        for ext in reversed(list(self._extensions.values())):
            if ext._is_active:
                await ext.deactivate()
                await self._event_bus.publish(
                    Event(
                        type="extension.deactivated",
                        payload={"extension_id": ext.id},
                        source="extension_manager",
                    )
                )

    def _resolve_dependencies(self) -> list[str]:
        """简单的拓扑排序，决定加载顺序。"""
        visited: set[str] = set()
        result: list[str] = []

        def visit(ext_id: str, stack: set[str]) -> None:
            if ext_id in visited:
                return
            if ext_id in stack:
                raise ValueError(f"Circular dependency detected involving '{ext_id}'")
            stack.add(ext_id)
            ext = self._extensions.get(ext_id)
            if ext:
                for dep in ext.dependencies:
                    if dep not in self._extensions:
                        raise ValueError(f"Extension '{ext_id}' depends on missing '{dep}'")
                    visit(dep, stack)
            stack.remove(ext_id)
            visited.add(ext_id)
            result.append(ext_id)

        for ext_id in self._extensions:
            visit(ext_id, set())

        return result

    def list_active(self) -> list[Extension]:
        return [e for e in self._extensions.values() if e._is_active]
