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
import time
from typing import Any, Optional

from learning_agent.agent.hook_system import HookSystem
from learning_agent.agent.observability import ObservabilityCollector
from learning_agent.agent.runtime_ports import SessionEventWriter, SessionStore, ToolExecutionService
from learning_agent.agent.tool_failure_tracker import ToolFailureTracker
from learning_agent.agent.tool_validator import ToolInputValidator
from learning_agent.agent.unresolved_failure_logger import UnresolvedFailureLogger
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
from learning_agent.learning_agent.session_events import EventVisibility, SessionEventType

logger = logging.getLogger(__name__)


class _StructuredToolFailure(Exception):
    """工具返回的结构化失败结果，被收口成与异常等价的失败语义。"""

    def __init__(self, payload: dict[str, Any], retryable: bool, reason_code: str, message: str):
        super().__init__(message)
        self.payload = payload
        self.retryable = retryable
        self.reason_code = reason_code
        self.message = message


def _detect_structured_failure(result: Any) -> Optional[_StructuredToolFailure]:
    """识别工具返回值中的结构化失败。兼容两种现有 schema:

    1. 嵌套：``{"error": {"code": ..., "message": ..., "retryable": ...}}``
    2. 扁平：``{"error": "..."}``

    若 result 不是失败，返回 None。
    """
    if not isinstance(result, dict):
        return None
    err = result.get("error")
    if err is None:
        return None
    if isinstance(err, dict):
        reason_code = str(err.get("code") or "TOOL_ERROR")
        message = str(err.get("message") or err.get("error") or "Tool reported a structured error")
        retryable = bool(err.get("retryable", False))
        return _StructuredToolFailure(result, retryable, reason_code, message)
    message = str(err)
    return _StructuredToolFailure(result, False, "TOOL_ERROR", message)


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
        event_writer: Optional[SessionEventWriter] = None,
        unresolved_failure_logger: Optional[UnresolvedFailureLogger] = None,
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
        # L1 trace writer（可选）。若注入则在工具执行流水线发射 tool.exec_* 诊断事件，
        # 这些事件 visibility=observability，不参与 replay/projection，仅做因果链追踪。
        self._event_writer = event_writer
        # 不可恢复失败的"内部账本"。所有最终对用户隐藏的失败都会落一条。
        self._unresolved_logger = unresolved_failure_logger

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

        并发语义:
        - 默认按 `ResilienceConfig.tool_parallel_execution` 决定是否并行。
        - 并行时使用 `asyncio.gather`,outcome 顺序与输入 `tool_calls` 一致。
        - 单工具或开关关闭时走串行路径,保留 escape hatch。

        契约：本方法对调用方不抛业务异常 —— 任何工具执行链路内部的意外
        异常都会被转成 is_error=True 的 outcome，保证 ReAct 主循环不被
        单个工具的崩溃打断。仅当解释器层异常（KeyboardInterrupt 等）才向上传播。
        """
        if not tool_calls:
            return []

        parallel = (
            self._resilience_config.tool_parallel_execution
            and len(tool_calls) > 1
        )

        if parallel:
            outcomes = await asyncio.gather(*[
                self._run_one_with_pipeline_guard(
                    session, tc, turn_count, parent_span, trace_id, build_hook_context
                )
                for tc in tool_calls
            ])
            return list(outcomes)

        # 串行回退路径(开关关闭 或 只有一个工具时也走这里以省去 gather 开销)
        results: list[dict[str, Any]] = []
        for tc in tool_calls:
            results.append(
                await self._run_one_with_pipeline_guard(
                    session, tc, turn_count, parent_span, trace_id, build_hook_context
                )
            )
        return results

    async def _run_one_with_pipeline_guard(
        self,
        session: LearningSession,
        tc: ToolCall,
        turn_count: int,
        parent_span: Optional[Any],
        trace_id: Optional[str],
        build_hook_context: callable,
    ) -> dict[str, Any]:
        """单个 tool_call 的完整执行 + pipeline_failure 兜底 + obs span 包裹。

        语义与原 execute_all 的 for-body 完全一致:任何业务异常都被吃成
        is_error=True 的 outcome,不向调用方传播。
        """
        tool_span = self.obs.start_span("tool.execute", parent=parent_span, trace_id=trace_id) if self.obs else None
        try:
            try:
                return await self._execute_one(
                    session, tc, turn_count, tool_span, trace_id, build_hook_context
                )
            except Exception as exc:
                # 工具流水线层意外异常（hook dispatch、event 投递、Pydantic 校验等）
                # 不允许打断整轮。统一转成 is_error=True 的 outcome，并落入失败账本。
                logger.exception(
                    "[ToolExecutor] Unexpected pipeline failure for tool '%s'",
                    tc.tool_id,
                )
                if self._unresolved_logger is not None:
                    try:
                        await self._unresolved_logger.record(
                            session_id=session.id,
                            layer="tool_executor_pipeline",
                            reason_code="pipeline_exception",
                            message=f"{type(exc).__name__}: {exc}",
                            tool_id=tc.tool_id,
                            arguments=dict(tc.arguments) if tc.arguments else None,
                        )
                    except Exception:
                        logger.exception(
                            "[ToolExecutor] unresolved_logger.record failed (non-fatal)"
                        )
                tc.error = str(exc)
                return {
                    "tool_call": tc,
                    "result": f"[Error] {exc}",
                    "is_error": True,
                    "display_result_override": None,
                    "extra_metadata": {"pipeline_failure": True},
                }
        finally:
            if self.obs:
                try:
                    self.obs.end_span(tool_span, trace_id=trace_id)
                except Exception:
                    # 可观测性 bug 绝不允许吃掉工具结果或打断后续工具执行。
                    logger.exception(
                        "[ToolExecutor] obs.end_span raised (suppressed)"
                    )

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
                started_event_id = self._emit_exec_started(
                    session_id=session.id,
                    tc=tc,
                    tool_call_id=tool_call_id,
                    attempt=tool_retry_count,
                    timeout=timeout,
                )
                exec_start_ts = time.monotonic()
                result = await self.tool_service.execute_tool_call(tc, timeout=timeout)
                # 收口：工具用 dict 形式返回的结构化失败，与异常等价处理。
                # 之前这里会被当成 success=True 写入 session，导致 failure_tracker
                # 失真、chat-only fallback 永远不触发、错误对象被原样塞给 LLM。
                structured_failure = _detect_structured_failure(result)
                if structured_failure is not None:
                    raise structured_failure
                latency_ms = (time.monotonic() - exec_start_ts) * 1000.0
                self._emit_exec_completed(
                    session_id=session.id,
                    tc=tc,
                    tool_call_id=tool_call_id,
                    attempt=tool_retry_count,
                    latency_ms=latency_ms,
                    result=result,
                    parent_event_id=started_event_id,
                )
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
                latency_ms = (time.monotonic() - exec_start_ts) * 1000.0
                self._emit_exec_failed(
                    session_id=session.id,
                    tc=tc,
                    tool_call_id=tool_call_id,
                    attempt=tool_retry_count,
                    latency_ms=latency_ms,
                    error=e,
                    parent_event_id=started_event_id,
                )
                last_error = e
                # 结构化失败：retryable 取自工具自己的 hint；普通异常走默认判定。
                if isinstance(e, _StructuredToolFailure):
                    is_retryable = e.retryable
                    reason_code = e.reason_code
                else:
                    is_retryable = self._is_retryable(e)
                    reason_code = "execution_error"

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
                    self.failure_tracker.record_failure(tc.tool_id, turn_count=0, reason=reason_code)
                    tc.error = str(last_error)
                    # 结构化失败保留原 payload（给 LLM 一个可读的失败摘要），
                    # 普通异常仍走 "[Error] xxx" 的可读字符串。
                    if isinstance(last_error, _StructuredToolFailure):
                        final_result = last_error.payload
                    else:
                        final_result = f"[Error] {last_error}"
                    final_error = str(last_error)
                    success = False
                    if self._unresolved_logger is not None:
                        try:
                            await self._unresolved_logger.record(
                                session_id=session.id,
                                layer="tool_executor",
                                reason_code=reason_code,
                                message=str(last_error),
                                tool_id=tc.tool_id,
                                arguments=dict(tc.arguments) if tc.arguments else None,
                                extra={"retry_count": tool_retry_count},
                            )
                        except Exception:
                            logger.exception(
                                "[ToolExecutor] unresolved_logger.record failed (non-fatal)"
                            )
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
                            "reason": reason_code,
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

    # ── L1 trace 事件发射（visibility=observability，不入 state） ──

    def _emit_exec_started(
        self,
        *,
        session_id: str,
        tc: ToolCall,
        tool_call_id: str,
        attempt: int,
        timeout: Optional[float],
    ) -> Optional[str]:
        """发射 tool.exec_started 诊断事件到 L1，返回 event_id 供 completed/failed 做 parent。

        失败容错：写 L1 不应该影响业务执行，任何异常都吞掉并打 warning。
        """
        if self._event_writer is None:
            return None
        try:
            event = self._event_writer.append_event(
                session_id=session_id,
                type=SessionEventType.TOOL_EXEC_STARTED,
                payload={
                    "tool_name": tc.tool_id,
                    "call_id": tool_call_id,
                    "args": dict(tc.arguments),
                    "attempt": attempt,
                    "timeout": timeout,
                },
                visibility=EventVisibility.OBSERVABILITY,
                parent_event_id=None,
            )
            return getattr(event, "event_id", None)
        except Exception:
            logger.warning("[ToolExecutor] Failed to emit tool.exec_started event", exc_info=True)
            return None

    def _emit_exec_completed(
        self,
        *,
        session_id: str,
        tc: ToolCall,
        tool_call_id: str,
        attempt: int,
        latency_ms: float,
        result: Any,
        parent_event_id: Optional[str],
    ) -> None:
        if self._event_writer is None:
            return
        try:
            result_repr = self._safe_result_repr(result)
            self._event_writer.append_event(
                session_id=session_id,
                type=SessionEventType.TOOL_EXEC_COMPLETED,
                payload={
                    "tool_name": tc.tool_id,
                    "call_id": tool_call_id,
                    "attempt": attempt,
                    "latency_ms": round(latency_ms, 3),
                    "result": result_repr,
                    "result_size": len(result_repr) if isinstance(result_repr, str) else None,
                },
                visibility=EventVisibility.OBSERVABILITY,
                parent_event_id=parent_event_id,
            )
        except Exception:
            logger.warning("[ToolExecutor] Failed to emit tool.exec_completed event", exc_info=True)

    def _emit_exec_failed(
        self,
        *,
        session_id: str,
        tc: ToolCall,
        tool_call_id: str,
        attempt: int,
        latency_ms: float,
        error: BaseException,
        parent_event_id: Optional[str],
    ) -> None:
        if self._event_writer is None:
            return
        try:
            self._event_writer.append_event(
                session_id=session_id,
                type=SessionEventType.TOOL_EXEC_FAILED,
                payload={
                    "tool_name": tc.tool_id,
                    "call_id": tool_call_id,
                    "attempt": attempt,
                    "latency_ms": round(latency_ms, 3),
                    "error_type": type(error).__name__,
                    "error_message": str(error),
                },
                visibility=EventVisibility.OBSERVABILITY,
                parent_event_id=parent_event_id,
            )
        except Exception:
            logger.warning("[ToolExecutor] Failed to emit tool.exec_failed event", exc_info=True)

    @staticmethod
    def _safe_result_repr(result: Any) -> str:
        """把任意 tool 返回值转为字符串表示，限制最大长度避免 L1 文件膨胀。"""
        try:
            if isinstance(result, str):
                text = result
            else:
                import json as _json
                try:
                    text = _json.dumps(result, ensure_ascii=False, default=str)
                except Exception:
                    text = repr(result)
        except Exception:
            text = "<unrepresentable result>"
        max_len = 4096
        if len(text) > max_len:
            return text[:max_len] + f"...<truncated {len(text) - max_len} chars>"
        return text


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
