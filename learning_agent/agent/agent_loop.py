"""
Agent Runtime 层路由器。

本模块收敛为两层：
- `AgentLoop`: 运行时路由器，管理 per-session AgentLoopSession 实例
- 从 `session_runtime` 重新导出 `AgentLoopSession`、`AgentState` 以保持向后兼容
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterable, Optional

from learning_agent.agent.event_bus import EventBus
from learning_agent.agent.hook_system import HookSystem
from learning_agent.agent.observability import ObservabilityCollector
from learning_agent.agent.runtime_ports import MemoryService, SessionStore, ToolExecutionService
from learning_agent.agent.tool_validator import ToolInputValidator
from learning_agent.ai import (
    ChatChunk,
    LearningSession,
    ResilienceConfig,
)
from learning_agent.ai.base_provider import BaseProvider
from learning_agent.ai.models import AgentMode
from learning_agent.learning_agent.compaction import CompactionPlan
from learning_agent.learning_agent.mode_service import TurnExecutionKind, TurnExecutionProfile

# 向后兼容：从 session_runtime 重新导出
from learning_agent.agent.session_runtime import AgentLoopSession, AgentState

logger = logging.getLogger(__name__)


class AgentLoop:
    """
    Agent Runtime 层路由器。

    它持有运行时共享依赖，按 session 管理 `AgentLoopSession` 实例，并向上层
    暴露稳定的只读 runtime 摘要。它不是产品层编排器，也不拥有 session 数据真源。
    """

    def __init__(
        self,
        provider: BaseProvider,
        memory_service: Optional[MemoryService],
        session_store: SessionStore,
        hook_system: HookSystem,
        event_bus: EventBus,
        tool_execution_service: ToolExecutionService,
        observability: Optional[ObservabilityCollector] = None,
        max_react_turns: int = 10,
        resilience_config: Optional[ResilienceConfig] = None,
        session_runtime_ttl: int = 3600,
    ):
        # Runtime 共享依赖
        self.provider = provider
        self.memory_service = memory_service
        self.sessions = session_store
        self.hooks = hook_system
        self.events = event_bus
        self.tool_execution_service = tool_execution_service
        self.obs = observability

        # 全局配置
        self.max_react_turns = max_react_turns
        self._resilience_config = resilience_config or ResilienceConfig()
        self._validator = ToolInputValidator(tool_execution_service)

        # per-session 运行时容器
        self._session_runtimes: dict[str, AgentLoopSession] = {}
        self._session_last_accessed: dict[str, float] = {}
        self._session_runtime_ttl = session_runtime_ttl

    # ── 公共接口 ──

    async def run(
        self,
        session: LearningSession,
        user_input: str,
        profile: Optional[TurnExecutionProfile] = None,
        *,
        compaction_plan: CompactionPlan | None = None,
    ) -> AsyncIterable[ChatChunk]:
        """
        执行一轮 Agent 循环。
        获取或创建 per-session 运行时，委托执行。
        """
        if profile is None:
            profile = TurnExecutionProfile(
                mode=AgentMode.CHAT,
                turn_kind=TurnExecutionKind.REACT,
                system_prompt=self._get_system_prompt(),
            )
        runtime = self._get_or_create_runtime(session.id)
        async with runtime.lock:
            async for chunk in runtime.run_turn(
                session,
                user_input,
                profile,
                compaction_plan=compaction_plan,
            ):
                yield chunk

    def clear_runtime(self, session_id: str) -> dict[str, Any]:
        """
        清理指定 session 的运行时状态，并返回被清理 runtime 的稳定摘要。
        """
        runtime = self._session_runtimes.pop(session_id, None)
        cleared_summary = None
        if runtime:
            cleared_summary = runtime.get_runtime_summary(
                last_accessed=self._session_last_accessed.get(session_id),
            )
            runtime.clear()
        self._session_last_accessed.pop(session_id, None)
        if self.obs:
            self.obs.clear_session_traces(session_id)
            self.obs.record_runtime_cleared(session_id)
        logger.info(f"[AgentLoop] Session runtime cleared: {session_id}")
        return {
            "session_id": session_id,
            "had_runtime": runtime is not None,
            "cleared_runtime": cleared_summary,
        }

    def clear_session_runtime(self, session_id: str) -> None:
        """清理指定 session 的运行时状态。"""
        self.clear_runtime(session_id)

    def clear_all_runtimes(self) -> None:
        """清理所有运行时（系统启动/关闭时调用）。"""
        for runtime in self._session_runtimes.values():
            runtime.clear()
        self._session_runtimes.clear()
        self._session_last_accessed.clear()

    def get_runtime_summary(self, session_id: str) -> Optional[dict[str, Any]]:
        """返回单个 session runtime 的只读摘要。"""
        self._cleanup_expired_runtimes()
        runtime = self._session_runtimes.get(session_id)
        if runtime is None:
            return None
        return runtime.get_runtime_summary(
            last_accessed=self._session_last_accessed.get(session_id),
        )

    def list_runtime_summaries(self) -> list[dict[str, Any]]:
        """返回所有活跃 runtime 的只读摘要列表。"""
        self._cleanup_expired_runtimes()
        return [
            runtime.get_runtime_summary(
                last_accessed=self._session_last_accessed.get(session_id),
            )
            for session_id, runtime in self._session_runtimes.items()
        ]

    def get_runtime_overview(self) -> dict[str, Any]:
        """返回当前运行时的稳定摘要，仅包含 Runtime 自身信息。"""
        runtimes = self.list_runtime_summaries()
        return {
            "active_runtime_count": len(runtimes),
            "runtimes": runtimes,
        }

    # ── 内部方法 ──

    def _get_or_create_runtime(self, session_id: str) -> AgentLoopSession:
        """获取或创建 session 运行时，同时清理过期实例。"""
        self._cleanup_expired_runtimes()

        if session_id not in self._session_runtimes:
            self._session_runtimes[session_id] = AgentLoopSession(
                session_id=session_id,
                agent_loop=self,
                resilience_config=self._resilience_config,
            )
            if self.obs:
                self.obs.record_runtime_created(session_id)
            logger.info(f"[AgentLoop] Session runtime created: {session_id}")

        self._session_last_accessed[session_id] = time.time()
        return self._session_runtimes[session_id]

    def _cleanup_expired_runtimes(self) -> None:
        """清理超过 TTL 未访问的运行时实例。"""
        if self._session_runtime_ttl <= 0:
            return
        now = time.time()
        expired = [
            sid for sid, last in self._session_last_accessed.items()
            if now - last > self._session_runtime_ttl
        ]
        for sid in expired:
            self.clear_session_runtime(sid)
            logger.debug(f"[AgentLoop] Expired session runtime cleaned: {sid}")

    def _get_system_prompt(self) -> str:
        return (
            "You are a learning assistant. Help the user understand concepts deeply, "
            "ask clarifying questions when needed, and encourage active recall."
        )

    @staticmethod
    def _is_retryable_tool_error(error: Exception) -> bool:
        """判断工具错误是否属于 transient 故障，值得重试。"""
        retryable_exceptions = (
            TimeoutError,
            asyncio.TimeoutError,
            ConnectionError,
            ConnectionRefusedError,
            ConnectionResetError,
            BrokenPipeError,
        )
        if isinstance(error, retryable_exceptions):
            return True

        error_str = str(error).lower()
        retryable_keywords = [
            "503", "502", "504", "429",
            "timeout", "connection reset", "connection refused",
            "temporarily unavailable", "service unavailable",
            "too many requests", "rate limit",
        ]
        return any(kw in error_str for kw in retryable_keywords)
