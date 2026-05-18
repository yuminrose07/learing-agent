from __future__ import annotations

from typing import Any, Optional

from learning_agent.agent.runtime_ports import ToolExecutionService
from learning_agent.ai import ToolCall, ToolDefinition
from learning_agent.learning_agent.tool_registry import ToolRegistry


class ToolExecutionServiceImpl(ToolExecutionService):
    """
    Product 层实现的工具执行服务。
    
    职责：
    - 实现 ToolExecutionService 协议
    - 使用 ToolRegistry 执行具体工具调用
    - 提供超时保护和错误处理
    
    边界：
    - 属于 Product/Application 层
    - 被 Runtime 层依赖（通过 Protocol）
    - 不包含工具注册逻辑（由 ExtensionManager 负责）
    """

    def __init__(self, tool_registry: ToolRegistry):
        """
        初始化工具执行服务。
        
        Args:
            tool_registry: 工具注册表实例，提供工具执行能力
        """
        self._tool_registry = tool_registry

    async def execute_tool_call(
        self,
        tool_call: ToolCall,
        timeout: Optional[float] = None,
    ) -> Any:
        """执行工具调用，具体执行细节统一委托给 Product 层的 ToolRegistry。"""
        return await self._tool_registry.execute(tool_call, timeout=timeout)

    def get_tool_definition(self, tool_id: str) -> Optional[ToolDefinition]:
        """
        获取工具定义。
        
        Args:
            tool_id: 工具ID
            
        Returns:
            工具定义对象，如果工具不存在则返回 None
        """
        return self._tool_registry.get(tool_id)

    def list_tools(self) -> list[ToolDefinition]:
        """
        列出所有可用工具。
        
        Returns:
            工具定义对象列表
        """
        return self._tool_registry.list_tools()
