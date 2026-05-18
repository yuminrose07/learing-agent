"""
Product 层内部使用的工具注册表。

负责维护工具定义、处理器以及实际执行能力。
扩展通过 Product 层的 ExtensionManager 将工具注册到这里。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from learning_agent.ai import ToolCall, ToolDefinition

logger = logging.getLogger(__name__)

ToolHandler = Callable[..., Awaitable[Any]]


class ToolRegistry:
    """
    全局工具注册表。
    - 每个工具有唯一的 id
    - 工具可由扩展注册，也可由核心注册
    - 支持动态注册/注销
    """

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self._handlers: dict[str, ToolHandler] = {}

    def register(
        self,
        tool_def: ToolDefinition,
        handler: ToolHandler,
        extension_id: Optional[str] = None,
    ) -> None:
        """注册一个工具及其处理函数。"""
        self._tools[tool_def.id] = tool_def
        self._handlers[tool_def.id] = handler
        logger.info(f"[ToolRegistry] Registered tool '{tool_def.id}' (ext={extension_id})")

    def unregister(self, tool_id: str) -> None:
        """注销一个工具。"""
        self._tools.pop(tool_id, None)
        self._handlers.pop(tool_id, None)
        logger.info(f"[ToolRegistry] Unregistered tool '{tool_id}'")

    def get(self, tool_id: str) -> Optional[ToolDefinition]:
        return self._tools.get(tool_id)

    def get_handler(self, tool_id: str) -> Optional[ToolHandler]:
        return self._handlers.get(tool_id)

    def list_tools(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def list_tool_schemas(self) -> list[dict[str, Any]]:
        """返回可供 LLM 使用的 tool schema 列表（OpenAI 格式）。"""
        schemas = []
        for tool in self._tools.values():
            schema = {
                "type": "function",
                "function": {
                    "name": tool.id,
                    "description": tool.description,
                    "parameters": tool.parameters or {"type": "object", "properties": {}},
                },
            }
            schemas.append(schema)
        return schemas

    async def execute(self, tool_call: ToolCall, timeout: Optional[float] = None) -> Any:
        """执行工具调用，带超时保护。"""
        handler = self._handlers.get(tool_call.tool_id)
        if not handler:
            raise ValueError(f"Tool '{tool_call.tool_id}' not found")

        start = time.time()

        try:
            if inspect.iscoroutinefunction(handler):
                coro = handler(**tool_call.arguments)
            else:
                # 同步函数包装为异步
                coro = asyncio.to_thread(handler, **tool_call.arguments)

            result = await asyncio.wait_for(coro, timeout=timeout or 60.0)

            tool_call.result = result
            tool_call.duration_ms = int((time.time() - start) * 1000)
            return result

        except asyncio.TimeoutError:
            tool_call.duration_ms = int((time.time() - start) * 1000)
            raise TimeoutError(
                f"Tool '{tool_call.tool_id}' execution timed out after "
                f"{timeout or 60.0}s. The tool handler did not complete within the allowed time."
            )
        except Exception:
            tool_call.duration_ms = int((time.time() - start) * 1000)
            raise
