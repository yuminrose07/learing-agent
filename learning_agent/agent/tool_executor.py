"""
Tool Executor — 工具执行流水线。

职责：
- 对单个或多个 tool call 执行完整生命周期
- ban 检查 → 输入校验 → before_tool_execute Hook → 执行 → 重试 → after_tool_execute Hook
- 记录 failure / success 到 FailureTracker
- 发射工具级事件

边界：
- 不持有 session 状态机
- 不决定 ReACT 循环流转
- 仅返回结构化结果，由调用方（AgentLoopSession）写回 session
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from learning_agent.agent.hook_system import HookSystem
from learning_agent.agent.observability import ObservabilityCollector
from learning_agent.agent.runtime_ports import SessionStore, ToolExecutionService
from learning_agent.agent.tool_failure_tracker import ToolFailureTracker
from learning_agent.agent.tool_validator import ToolInputValidator
from learning_agent.ai import (
    AgentEventType,
    AfterToolExecuteInput,
    BeforeToolExecuteInput,
    Event,
    HookContext,
    HookDecision,
    LearningSession,
    ResilienceConfig,
    ToolCall,
)

logger = logging.getLogger(__name__)


class ToolExecutor:
    """
    负责 tool call 的完整执行生命周期。

    注入依赖：
    - hooks: HookSystem
    - validator: ToolInputValidator
    - failure_tracker: ToolFailureTracker
    - events: EventBus
    - obs: ObservabilityCollector（可选）
    - tool_service: ToolExecutionService
    - session_store: SessionStore
    - resilience_config: 重试/超时策略
    - is_retryable_fn: 判断工具错误是否可重试的函数
    """

    def __init__(
        self,
        hooks: HookSystem,
        validator: ToolInputValidator,
        failure_tracker: ToolFailureTracker,
        events: Any,  # EventBus
        tool_service: ToolExecutionService,
        session_store: SessionStore,
        resilience_config: ResilienceConfig,
        observability: Optional[ObservabilityCollector] = None,
        is_retryable_fn: Optional[callable] = None,
    ):
        self.hooks = hooks
        self.validator = validator
        self.failure_tracker = failure_tracker
        self.events = events
        self.tool_service = tool_service
        self.session_store = session_store
        self.obs = observability
        self._resilience_config = resilience_config
        self._is_retryable = is_retryable_fn or _default_is_retryable

    # ── 批量执行 ──

    async def execute_all(
        self,
        session: LearningSession,
        tool_calls: list[ToolCall],
        turn_count: int,
        parent_span: Optional[Any],
        trace_id: Optional[str],
        build_hook_context: callable,
    ) -> list[dict[str, Any]]:
        """
        执行全部 tool calls，返回结构化结果列表。
        每个结果 dict 包含：tool_call, result, is_error, display_result_override, extra_metadata
        """
        results: list[dict[str, Any]] = []
        for tc in tool_calls:
            tool_span = self.obs.start_span("tool.execute", parent=parent_span, trace_id=trace_id) if self.obs else None
            try:
                result = await self._execute_one(
                    session, tc, turn_count, tool_span, trace_id, build_hook_context
                )
                results.append(result)
            finally:
                if self.obs:
                    self.obs.end_span(tool_span, trace_id=trace_id)
        return results

    # ── 单工具执行 ──

    async def _execute_one(
        self,
        session: LearningSession,
        tc: ToolCall,
        turn_count: int,
        tool_span: Optional[Any],
        trace_id: Optional[str],
        build_hook_context: callable,
    ) -> dict[str, Any]:
        tool_call_id = getattr(tc, "call_id", "") or _random_id()
        tool_def = self.tool_service.get_tool_definition(tc.tool_id)
        base_context = build_hook_context(
            session,
            turn_id=turn_count,
            metadata={"tool_id": tc.tool_id, "tool_call_id": tool_call_id},
        )

        # Step 1: emit TOOL_EXECUTION_START
        await self._emit_agent_event(
            session.id, AgentEventType.TOOL_EXECUTION_START,
            {"tool_call_id": tool_call_id, "tool_id": tc.tool_id, "arguments": tc.arguments}
        )

        # Step 2: ban 检查
        if self.failure_tracker.is_banned(tc.tool_id, turn_count):
            return await self._handle_banned(
                session, tc, tool_call_id, turn_count, trace_id
            )

        # Step 3: 输入参数校验
        if self._resilience_config.tool_validation_enabled:
            validation_errors = await self.validator.validate(tc)
            if validation_errors is not None:
                return await self._handle_validation_failed(
                    session, tc, tool_call_id, turn_count, validation_errors, trace_id
                )

        # Step 4: before_tool_execute Hook
        before_tool_result = await self.hooks.run_before_tool_execute(
            BeforeToolExecuteInput(
                tool_call_id=tool_call_id,
                tool_name=tc.tool_id,
                arguments=dict(tc.arguments),
                tool_schema=tool_def.parameters if tool_def else {},
                context=base_context,
            ),
            trace_span=tool_span,
        )
        self._consume_hook_side_effects("before_tool_execute", before_tool_result, tool_span)

        if before_tool_result.decision == HookDecision.ASK:
            return await self._handle_ask(
                session, tc, tool_call_id, before_tool_result, trace_id
            )

        if before_tool_result.decision == HookDecision.DENY:
            return await self._handle_deny(
                session, tc, tool_call_id, turn_count, before_tool_result, trace_id
            )

        if before_tool_result.patched_arguments:
            tc.arguments = {**tc.arguments, **before_tool_result.patched_arguments}

        await self.events.publish(
            Event(
                type="agent.toolCalled",
                payload={"tool_id": tc.tool_id, "arguments": tc.arguments},
                source="agent_loop",
                session_id=session.id,
            )
        )

        # Step 5: 执行（带重试）
        result, success, tool_retry_count, final_error = await self._execute_with_retry(
            session, tc, tool_call_id, trace_id
        )

        # Step 6: after_tool_execute Hook
        after_tool_result = await self.hooks.run_after_tool_execute(
            AfterToolExecuteInput(
                tool_call_id=tool_call_id,
                tool_name=tc.tool_id,
                arguments=dict(tc.arguments),
                success=success,
                result=result,
                error=final_error,
                duration_ms=tc.duration_ms or 0,
                retry_count=tool_retry_count,
                annotations=dict(getattr(before_tool_result, "annotations", {})),
                context=base_context,
            ),
            trace_span=tool_span,
        )
        self._consume_hook_side_effects("after_tool_execute", after_tool_result, tool_span)

        return {
            "tool_call": tc,
            "result": result,
            "is_error": not success,
            "display_result_override": after_tool_result.display_result_override,
            "extra_metadata": {
                **getattr(before_tool_result, "annotations", {}),
                **after_tool_result.extra_metadata,
            },
        }

    # ── 执行带重试 ──

    async def _execute_with_retry(
        self,
        session: LearningSession,
        tc: ToolCall,
        tool_call_id: str,
        trace_id: Optional[str],
    ) -> tuple[Any, bool, int, Optional[str]]:
        max_tool_retries = self._resilience_config.max_tool_retries
        tool_retry_count = 0
        last_error = None
        success = False
        final_result: Any = None
        final_error: Optional[str] = None

        while True:
            try:
                timeout = self._resilience_config.tool_default_timeout
                result = await self.tool_service.execute_tool_call(tc, timeout=timeout)
                tc.result = result
                final_result = result
                final_error = None
                success = True
                self.failure_tracker.record_success(tc.tool_id)
                await self.events.publish(
                    Event(
                        type="agent.toolResult",
                        payload={"tool_id": tc.tool_id, "success": True, "result": result},
                        source="agent_loop",
                        session_id=session.id,
                    )
                )
                await self._emit_agent_event(
                    session.id, AgentEventType.TOOL_EXECUTION_END,
                    {"tool_call_id": tool_call_id, "tool_id": tc.tool_id, "result": result, "is_error": False}
                )
                break
            except Exception as e:
                last_error = e
                is_retryable = self._is_retryable(e)

                if tool_retry_count < max_tool_retries and is_retryable:
                    tool_retry_count += 1
                    delay = min(
                        self._resilience_config.tool_retry_base_delay * (2 ** (tool_retry_count - 1)),
                        self._resilience_config.tool_retry_max_delay,
                    )
                    logger.warning(
                        f"[ToolExecutor] Tool '{tc.tool_id}' failed transiently "
                        f"(attempt {tool_retry_count}/{max_tool_retries + 1}), retrying in {delay}s..."
                    )
                    await self.events.publish(
                        Event(
                            type="agent.toolRetry",
                            payload={
                                "tool_id": tc.tool_id,
                                "attempt": tool_retry_count,
                                "max_attempts": max_tool_retries + 1,
                                "delay": delay,
                                "reason": str(e),
                            },
                            source="agent_loop",
                            session_id=session.id,
                        )
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    self.failure_tracker.record_failure(tc.tool_id, turn_count=0, reason="execution_error")
                    tc.error = str(last_error)
                    final_result = f"[Error] {last_error}"
                    final_error = str(last_error)
                    success = False
                    await self.events.publish(
                        Event(
                            type="agent.toolResult",
                            payload={"tool_id": tc.tool_id, "success": False, "error": str(last_error)},
                            source="agent_loop",
                            session_id=session.id,
                        )
                    )
                    await self._emit_agent_event(
                        session.id, AgentEventType.TOOL_EXECUTION_END,
                        {
                            "tool_call_id": tool_call_id,
                            "tool_id": tc.tool_id,
                            "result": str(last_error),
                            "is_error": True,
                            "reason": "execution_error",
                        }
                    )
                    break

        return final_result, success, tool_retry_count, final_error

    # ── 结果处理分支 ──

    async def _handle_banned(
        self,
        session: LearningSession,
        tc: ToolCall,
        tool_call_id: str,
        turn_count: int,
        trace_id: Optional[str],
    ) -> dict[str, Any]:
        ban_msg = self.failure_tracker.get_ban_message(tc.tool_id)
        tc.error = ban_msg
        failures_in_window = self.failure_tracker.get_failures_in_window(tc.tool_id, turn_count)
        failure_types = self.failure_tracker.get_all_failures_in_window(tc.tool_id, turn_count)
        await self.events.publish(
            Event(
                type="agent.toolBanned",
                payload={
                    "tool_id": tc.tool_id,
                    "call_id": tool_call_id,
                    "turn": turn_count,
                    "failures_in_window": failures_in_window,
                    "failure_types": failure_types,
                },
                source="agent_loop",
                session_id=session.id,
            )
        )
        await self._emit_agent_event(
            session.id, AgentEventType.TOOL_EXECUTION_END,
            {"tool_call_id": tool_call_id, "tool_id": tc.tool_id, "result": ban_msg, "is_error": True, "reason": "banned"}
        )
        return {"tool_call": tc, "result": ban_msg, "is_error": True, "display_result_override": None, "extra_metadata": {"reason": "banned"}}

    async def _handle_validation_failed(
        self,
        session: LearningSession,
        tc: ToolCall,
        tool_call_id: str,
        turn_count: int,
        validation_errors: list[Any],
        trace_id: Optional[str],
    ) -> dict[str, Any]:
        self.failure_tracker.record_failure(tc.tool_id, turn_count, reason="validation_failed")
        error_message = self.validator.format_validation_error(tc.tool_id, tool_call_id, validation_errors)
        tc.error = error_message
        await self.events.publish(
            Event(
                type="agent.toolValidationFailed",
                payload={
                    "tool_id": tc.tool_id,
                    "call_id": tool_call_id,
                    "errors": [e.model_dump() for e in validation_errors],
                    "turn": turn_count,
                },
                source="agent_loop",
                session_id=session.id,
            )
        )
        await self._emit_agent_event(
            session.id, AgentEventType.TOOL_EXECUTION_END,
            {"tool_call_id": tool_call_id, "tool_id": tc.tool_id, "result": error_message, "is_error": True, "reason": "validation"}
        )
        return {"tool_call": tc, "result": error_message, "is_error": True, "display_result_override": None, "extra_metadata": {"reason": "validation"}}

    async def _handle_ask(
        self,
        session: LearningSession,
        tc: ToolCall,
        tool_call_id: str,
        before_tool_result: Any,
        trace_id: Optional[str],
    ) -> dict[str, Any]:
        ask_message = before_tool_result.ask_message or "Tool call requires approval"
        tc.error = ask_message
        await self.events.publish(
            Event(
                type="agent.toolPermissionAsk",
                payload={"tool_id": tc.tool_id, "call_id": tool_call_id, "message": ask_message},
                source="agent_loop",
                session_id=session.id,
            )
        )
        await self._emit_agent_event(
            session.id, AgentEventType.TOOL_EXECUTION_END,
            {"tool_call_id": tool_call_id, "tool_id": tc.tool_id, "result": ask_message, "is_error": True, "reason": "ask"}
        )
        return {
            "tool_call": tc,
            "result": f"[Approval Required] {ask_message}",
            "is_error": True,
            "display_result_override": None,
            "extra_metadata": {"reason": "ask", **before_tool_result.annotations},
        }

    async def _handle_deny(
        self,
        session: LearningSession,
        tc: ToolCall,
        tool_call_id: str,
        turn_count: int,
        before_tool_result: Any,
        trace_id: Optional[str],
    ) -> dict[str, Any]:
        deny_reason = before_tool_result.deny_reason or "Tool call denied by guard policy"
        tc.error = deny_reason
        self.failure_tracker.record_failure(tc.tool_id, turn_count, reason="hook_abort")
        await self.events.publish(
            Event(
                type="agent.toolPermissionDenied",
                payload={"tool_id": tc.tool_id, "call_id": tool_call_id, "reason": deny_reason},
                source="agent_loop",
                session_id=session.id,
            )
        )
        await self._emit_agent_event(
            session.id, AgentEventType.TOOL_EXECUTION_END,
            {"tool_call_id": tool_call_id, "tool_id": tc.tool_id, "result": deny_reason, "is_error": True, "reason": "deny"}
        )
        return {
            "tool_call": tc,
            "result": f"[Blocked] {deny_reason}",
            "is_error": True,
            "display_result_override": None,
            "extra_metadata": {"reason": "deny", **before_tool_result.annotations},
        }

    # ── 辅助 ──

    async def _emit_agent_event(
        self,
        session_id: str,
        event_type: AgentEventType,
        payload: dict[str, Any],
    ) -> None:
        """通过 EventBus 发射 AgentEvent。"""
        from learning_agent.ai import AgentEvent
        agent_event = AgentEvent(type=event_type, payload=payload, session_id=session_id)
        await self.events.publish(
            Event(
                type=f"agent.{event_type.value}",
                payload=agent_event.model_dump(),
                source="agent_loop",
                session_id=session_id,
            )
        )

    def _consume_hook_side_effects(
        self,
        hook_point: str,
        result: Any,
        trace_span: Optional[Any],
    ) -> None:
        """消费 Hook 返回的 side effects（目前主要是 span annotations）。"""
        if trace_span and hasattr(result, "annotations") and result.annotations:
            for key, value in result.annotations.items():
                trace_span.tags[f"hook.{hook_point}.{key}"] = value


def _default_is_retryable(error: Exception) -> bool:
    import asyncio
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


def _random_id() -> str:
    import uuid
    return uuid.uuid4().hex[:8]
