from __future__ import annotations

from typing import Any, Optional, Protocol

from learning_agent.ai import (
    ChatMessage,
    ContextComponent,
    LearningSession,
    MessageRole,
    SessionEntry,
    ToolCall,
    ToolDefinition,
)
from learning_agent.learning_agent.compaction import CompactionPlan
from learning_agent.learning_agent.mode_service import TurnExecutionProfile
from learning_agent.learning_agent.views import LLMInputView


class SessionStore(Protocol):
    """Runtime 执行 turn 所需的最小 session 存取能力。"""

    def append_message(
        self,
        session_id: str,
        role: MessageRole,
        content: str,
        parent_id: Optional[str] = None,
        metadata: Optional[dict] = None,
        tool_calls: Optional[list[ToolCall]] = None,
        tool_results: Optional[list[dict[str, Any]]] = None,
    ) -> Optional[SessionEntry]: ...

    def get_message_history(
        self,
        session_id: str,
        leaf_id: Optional[str] = None,
    ) -> list[SessionEntry]: ...

    def build_llm_input_view(
        self,
        session_id: str,
        profile: TurnExecutionProfile,
        compaction_plan: CompactionPlan | None = None,
    ) -> LLMInputView: ...

    def find_truncatable_tool_entry(
        self,
        session_id: str,
        min_chars: int,
    ) -> Optional[SessionEntry]: ...

    def record_message_stream_failed(
        self,
        session_id: str,
        reason: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None: ...

    def record_message_interrupted(
        self,
        session_id: str,
        reason: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None: ...

    def patch_entry(
        self,
        session_id: str,
        entry_id: str,
        path: str,
        value: Any,
    ) -> bool: ...


class MemoryService(Protocol):
    """Runtime 可选依赖的最小 Memory 能力端口。"""

    def relevant_recall(
        self,
        query: str,
        session: Optional[LearningSession] = None,
        limit: int = 5,
    ) -> list[ContextComponent]: ...


class ToolExecutionService(Protocol):
    """Runtime 依赖的工具执行服务协议，由 Product 层实现。"""

    async def execute_tool_call(
        self,
        tool_call: ToolCall,
        timeout: Optional[float] = None,
    ) -> Any: ...

    def get_tool_definition(self, tool_id: str) -> Optional[ToolDefinition]: ...

    def list_tools(self) -> list[ToolDefinition]: ...


class SessionEventWriter(Protocol):
    """Runtime 写入 L1 事件流的最小能力端口。

    依赖方向：Runtime 定义协议，Product 层 ``SessionEventStore`` 实现。
    Runtime 通过这个 Protocol 写诊断/观测事件（visibility=observability），
    不直接依赖 Product 层具体类型。

    与 ``SessionStore`` 的区别：
    - ``SessionStore`` 是高层 session 状态变更（append_message 等）
    - ``SessionEventWriter`` 是底层事件流写入，用于 trace / observability
    """

    def append_event(
        self,
        session_id: str,
        type: str,
        payload: Optional[dict[str, Any]] = None,
        visibility: str = "agent",
        parent_event_id: Optional[str] = None,
    ) -> Any: ...
