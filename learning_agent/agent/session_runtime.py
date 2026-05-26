"""
Session Runtime — 边界编排器。

职责：
- 执行完整的 ReACT turn（外层循环）
- 状态机管理
- 并发锁持有
- Hook 插桩（before_agent_run / on_stream_chunk / after_response / before_tool_execute / after_tool_execute）
- EventBus 广播
- Observability trace/span 生命周期
- 降级策略（chat-only mode、failure tracker）
- 错误恢复（流中断兜底、孤儿 tool call 补偿）

边界：
- 不负责 session 生命周期决策
- 不负责文件持久化
- 不新增 Memory 私有状态字段
- LLM 上下文组装、工具执行流水线 分别委托给 ReActEngine / ToolExecutor
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from enum import Enum
from typing import Any, AsyncIterable, Optional

from learning_agent.agent.event_bus import EventBus
from learning_agent.agent.hook_system import HookSystem
from learning_agent.agent.observability import ObservabilityCollector
from learning_agent.agent.runtime_ports import SessionStore, ToolExecutionService
from learning_agent.agent.react_engine import ReActEngine
from learning_agent.agent.tool_executor import ToolExecutor
from learning_agent.agent.tool_failure_tracker import ToolFailureTracker
from learning_agent.agent.tool_validator import ToolInputValidator
from learning_agent.ai import (
    AgentEvent,
    AgentEventType,
    AgentStateSnapshot,
    AfterResponseInput,
    AfterToolExecuteInput,
    BeforeAgentRunInput,
    BeforeToolExecuteInput,
    ChatChunk,
    ChatMessage,
    ChatParams,
    Event,
    HookContext,
    HookDecision,
    LearningSession,
    MessageRole,
    OnStreamChunkInput,
    ProviderUsage,
    ResilienceConfig,
    ToolCall,
    TurnCompactionUsage,
    TurnUsage,
    TraceSpan,
)
from learning_agent.learning_agent.compaction import CompactionPlan
from learning_agent.learning_agent.compaction.full_compact import estimate_text_tokens
from learning_agent.learning_agent.mode_service import TurnExecutionKind, TurnExecutionProfile

logger = logging.getLogger(__name__)


class AgentState(str, Enum):
    """Agent 循环中的状态机。"""
    IDLE = "idle"
    ALIGNING = "aligning"
    BUILDING_CONTEXT = "building_context"
    CALLING_LLM = "calling_llm"
    STREAMING = "streaming"
    EXECUTING_TOOL = "executing_tool"
    COMPLETED = "completed"
    ERROR = "error"


class AgentLoopSession:
    """
    单个 session 的 Agent Runtime 上下文。

    职责：
    1. 执行完整的 ReACT turn
    2. 持有 per-session 运行时可变状态
    3. 保证同 session 的并发安全
    4. 管理状态机、工具执行、重试、降级与观测

    边界：
    - 不负责 session 生命周期决策
    - 不负责文件持久化
    - 不新增 Memory 私有状态字段
    """

    def __init__(
        self,
        session_id: str,
        agent_loop: Any,  # AgentLoop — 延迟类型避免循环导入
        resilience_config: ResilienceConfig,
    ):
        self.session_id = session_id
        self.agent_loop = agent_loop

        # ── 运行时弹性策略状态 ──
        self._failure_tracker = ToolFailureTracker(
            window_turns=resilience_config.tool_failure_window_turns,
            threshold=resilience_config.tool_failure_threshold,
        )
        self._chat_only_mode = False
        self._chat_only_success_turns = 0

        # ── 状态机 ──
        self.state = AgentState.IDLE

        # ── 并发控制 ──
        self.lock = asyncio.Lock()

        # ── 当前 trace ID（用于 span 隔离）──
        self._current_trace_id: Optional[str] = None
        self._hook_runtime_metadata: dict[str, Any] = {}
        self._last_turn_count = 0

        # ── 委托引擎 ──
        self._engine = ReActEngine(
            provider=self.provider,
            session_store=self.sessions,
            max_react_turns=self.max_react_turns,
            resilience_config=self._resilience_config,
            events=self.events,
        )
        self._tool_executor = ToolExecutor(
            hooks=self.hooks,
            validator=self._validator,
            failure_tracker=self._failure_tracker,
            events=self.events,
            tool_service=self.tools,
            session_store=self.sessions,
            resilience_config=self._resilience_config,
            observability=self.obs,
            is_retryable_fn=self.agent_loop._is_retryable_tool_error,
            event_writer=getattr(self.agent_loop, "event_writer", None),
            unresolved_failure_logger=getattr(self.agent_loop, "unresolved_failure_logger", None),
        )

    # ── 共享依赖快捷访问 ──

    @property
    def provider(self):
        return self.agent_loop.provider

    @property
    def memory_service(self):
        """显式 Memory 服务依赖；仅代表产品层子域服务，不代表 runtime 自有状态。"""
        return self.agent_loop.memory_service

    @property
    def memory(self):
        """兼容旧命名，优先使用 `memory_service` 表达跨层服务依赖。"""
        return self.memory_service

    @property
    def sessions(self):
        return self.agent_loop.sessions

    @property
    def hooks(self):
        return self.agent_loop.hooks

    def get_runtime_summary(self, *, last_accessed: Optional[float] = None) -> dict[str, Any]:
        """返回稳定的只读 runtime 摘要，供 AgentLoop 对外暴露。"""
        trace = self.obs.get_trace_by_session(self.session_id) if self.obs else None
        spans = self.obs.get_spans_by_session(self.session_id) if trace else []

        return {
            "session_id": self.session_id,
            "state": self.state.value,
            "chat_only_mode": self._chat_only_mode,
            "chat_only_success_turns": self._chat_only_success_turns,
            "turn_count": self._last_turn_count,
            "failure_tracker": self._failure_tracker.get_runtime_summary(self._last_turn_count),
            "lock_acquired": self.lock.locked(),
            "last_accessed": last_accessed,
            "trace": {
                "trace_id": trace.trace_id,
                "span_count": len(trace.spans),
                "active_spans": [span.name for span in spans],
                "duration_ms": trace.duration_ms,
            } if trace else None,
        }

    @property
    def events(self):
        return self.agent_loop.events

    @property
    def tools(self):
        return self.agent_loop.tool_execution_service

    @property
    def obs(self):
        return self.agent_loop.obs

    @property
    def max_react_turns(self):
        return self.agent_loop.max_react_turns

    @property
    def _resilience_config(self):
        return self.agent_loop._resilience_config

    @property
    def _validator(self):
        return self.agent_loop._validator

    # ── 状态管理 ──

    def _set_state(self, new_state: AgentState) -> None:
        """状态转换，每次转换都触发可观测事件。"""
        old_state = self.state
        self.state = new_state
        logger.info(f"[AgentLoop] State: {old_state.value} -> {new_state.value} (session={self.session_id})")

        if self.obs:
            current_span = self.obs.current_span(trace_id=self._current_trace_id)
            if current_span:
                current_span.tags["agent.state"] = new_state.value

        try:
            asyncio.get_event_loop().create_task(
                self.events.publish(
                    Event(
                        type="agent.stateChanged",
                        payload={
                            "old_state": old_state.value,
                            "new_state": new_state.value,
                            "session_id": self.session_id,
                        },
                        source="agent_loop",
                        session_id=self.session_id,
                    )
                )
            )
        except RuntimeError:
            pass

    async def _emit_agent_event(
        self,
        event_type: AgentEventType,
        payload: dict[str, Any],
        session_id: Optional[str] = None,
    ) -> None:
        """发射 AgentEvent（丰富事件系统），同时通过 EventBus 广播。"""
        sid = session_id or self.session_id
        agent_event = AgentEvent(
            type=event_type,
            payload=payload,
            session_id=sid,
        )
        await self.events.publish(
            Event(
                type=f"agent.{event_type.value}",
                payload=agent_event.model_dump(),
                source="agent_loop",
                session_id=sid,
            )
        )

    async def _save_state_snapshot(
        self,
        session: LearningSession,
        turn_count: int,
        pending_tool_calls: list[str],
        profile: TurnExecutionProfile,
        compaction_plan: CompactionPlan | None = None,
        error_message: Optional[str] = None,
    ) -> None:
        """保存全局状态快照，并通过事件发射。"""
        llm_view = self.sessions.build_llm_input_view(
            session.id,
            profile,
            compaction_plan=compaction_plan,
        )
        snapshot = AgentStateSnapshot(
            session_id=session.id,
            system_prompt=self.agent_loop._get_system_prompt(),
            messages=[
                {
                    "role": message.role.value,
                    "content": message.content,
                    "tool_calls": message.tool_calls or [],
                    "tool_call_id": message.tool_call_id,
                }
                for message in llm_view.messages
            ],
            tools=[
                {
                    "id": t.id,
                    "name": t.name,
                    "description": t.description,
                }
                for t in self.tools.list_tools()
            ],
            turn_count=turn_count,
            pending_tool_calls=pending_tool_calls,
            error_message=error_message,
        )
        await self.events.publish(
            Event(
                type="agent.stateSnapshot",
                payload=snapshot.model_dump(),
                source="agent_loop",
                session_id=session.id,
            )
        )

    def _build_hook_context(
        self,
        session: LearningSession,
        turn_id: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> HookContext:
        merged_metadata = dict(self._hook_runtime_metadata)
        if metadata:
            merged_metadata.update(metadata)
        return HookContext(
            session_id=session.id,
            trace_id=self._current_trace_id,
            turn_id=turn_id,
            agent_state=self.state.value,
            metadata=merged_metadata,
        )

    def _consume_hook_side_effects(
        self,
        hook_name: str,
        hook_result: Any,
        trace_span: Optional[TraceSpan] = None,
    ) -> None:
        warnings = getattr(hook_result, "warnings", [])
        audit_records = getattr(hook_result, "audit_records", [])

        if warnings:
            logger.warning(
                "[AgentLoop] Hook '%s' emitted %s warnings (session=%s)",
                hook_name,
                len(warnings),
                self.session_id,
            )
            if trace_span:
                trace_span.tags[f"hook.{hook_name}.warning_count"] = len(warnings)
        if audit_records:
            logger.info(
                "[AgentLoop] Hook '%s' emitted %s audit records (session=%s)",
                hook_name,
                len(audit_records),
                self.session_id,
            )
            if trace_span:
                trace_span.tags[f"hook.{hook_name}.audit_count"] = len(audit_records)

    def _build_estimated_turn_usage(
        self,
        context_messages: list[ChatMessage],
        compaction_plan: CompactionPlan | None = None,
    ) -> TurnUsage:
        estimated_prompt_tokens = sum(
            estimate_text_tokens(message.content or "")
            for message in context_messages
        )
        context_limit = max(self.provider.get_max_context_length(), 1)
        return TurnUsage(
            estimated_prompt_tokens=estimated_prompt_tokens,
            context_limit=context_limit,
            utilization_ratio=round(estimated_prompt_tokens / context_limit, 4),
            is_estimated=True,
            compaction=TurnCompactionUsage(
                micro_compact_applied=bool(compaction_plan and compaction_plan.use_micro_compact),
                full_compact_applied=bool(compaction_plan and compaction_plan.use_full_compact),
                summary_block_present=bool(compaction_plan and compaction_plan.summary_block),
                full_compact_scope=(
                    compaction_plan.full_compact_scope
                    if compaction_plan is not None
                    else None
                ),
                recent_token_budget=(
                    compaction_plan.recent_token_budget
                    if compaction_plan is not None
                    else 0
                ),
            ),
        )

    @staticmethod
    def _extract_provider_usage(chunk: ChatChunk) -> ProviderUsage | None:
        raw_usage = chunk.metadata.get("provider_usage")
        if raw_usage is None:
            return None
        if isinstance(raw_usage, ProviderUsage):
            return raw_usage
        return ProviderUsage.model_validate(raw_usage)

    @staticmethod
    def _attach_turn_usage(chunk: ChatChunk, turn_usage: TurnUsage | None) -> ChatChunk:
        if turn_usage is None:
            return chunk
        metadata = dict(chunk.metadata)
        metadata["turn_usage"] = turn_usage.model_dump()
        return chunk.model_copy(update={"metadata": metadata})

    async def _publish_turn_usage_event(
        self,
        session_id: str,
        turn_usage: TurnUsage | None,
        *,
        turn: int | str | None = None,
        phase: str = "response",
    ) -> None:
        if turn_usage is None:
            return
        payload: dict[str, Any] = {
            "usage": turn_usage.model_dump(),
            "phase": phase,
        }
        if turn is not None:
            payload["turn"] = turn
        await self.events.publish(
            Event(
                type="agent.turnUsage",
                payload=payload,
                source="agent_loop",
                session_id=session_id,
            )
        )

    # ── 核心 turn ──

    async def run_turn(
        self,
        session: LearningSession,
        user_input: str,
        profile: TurnExecutionProfile,
        compaction_plan: CompactionPlan | None = None,
    ) -> AsyncIterable[ChatChunk]:
        """执行一轮完整的 ReACT 循环。调用方已持有 session 级锁。"""
        trace = None
        root_span = None
        if self.obs:
            trace = self.obs.start_trace(
                session_id=session.id,
                objective_id=session.objective_id,
                mode=profile.mode.value,
            )
            self._current_trace_id = trace.trace_id if trace else None
            root_span = self.obs.start_span("agent.loop", trace_id=self._current_trace_id)
            if root_span:
                root_span.tags["mode"] = profile.mode.value

        self._set_state(AgentState.IDLE)
        turn_count = 0
        self._last_turn_count = 0
        pending_tool_calls: list[str] = []
        error_message: Optional[str] = None
        self._hook_runtime_metadata = {}

        try:
            await self._emit_agent_event(
                AgentEventType.AGENT_START,
                {"session_id": session.id, "user_input": user_input, "mode": profile.mode.value},
                session.id,
            )

            if profile.turn_kind == TurnExecutionKind.SINGLE_PASS:
                async for chunk in self._run_single_pass_turn(
                    session,
                    user_input,
                    profile,
                    root_span,
                    compaction_plan=compaction_plan,
                ):
                    yield chunk
                await self._emit_agent_event(
                    AgentEventType.AGENT_END,
                    {"session_id": session.id, "reason": "single_pass_complete", "mode": profile.mode.value},
                    session.id,
                )
                self._set_state(AgentState.COMPLETED)
                if self.obs:
                    self.obs.end_span(root_span, trace_id=self._current_trace_id)
                    self.obs.end_trace()
                    self._current_trace_id = None
                return

            # before_agent_run Hook
            before_agent_result = await self.hooks.run_before_agent_run(
                BeforeAgentRunInput(
                    user_input=user_input,
                    config_snapshot={
                        "max_react_turns": self.max_react_turns,
                        "resilience": self._resilience_config.model_dump(),
                    },
                    provider_summary={
                        "default_model": getattr(self.provider, "default_model", None),
                        "supports_tool_calling": self.provider.supports_tool_calling(),
                        "supports_vision": self.provider.supports_vision(),
                        "max_context_length": self.provider.get_max_context_length(),
                    },
                    tools_summary=[
                        {"id": tool.id, "name": tool.name, "description": tool.description}
                        for tool in self.tools.list_tools()
                    ],
                    context=self._build_hook_context(session, metadata={"mode": profile.mode.value}),
                ),
                trace_span=root_span,
            )
            self._consume_hook_side_effects("before_agent_run", before_agent_result, trace_span=root_span)

            if before_agent_result.runtime_patch:
                self._hook_runtime_metadata.update(before_agent_result.runtime_patch)

            if before_agent_result.decision in {HookDecision.ASK, HookDecision.DENY}:
                response_text = (
                    before_agent_result.ask_message
                    if before_agent_result.decision == HookDecision.ASK
                    else before_agent_result.deny_reason
                ) or "Request blocked before agent run."
                self.sessions.append_message(
                    session.id,
                    MessageRole.ASSISTANT,
                    response_text,
                    metadata=dict(profile.assistant_message_metadata),
                )
                await self._emit_agent_event(
                    AgentEventType.MESSAGE_END,
                    {"role": "assistant", "content": response_text, "turn": 0, "mode": profile.mode.value},
                    session.id,
                )
                yield ChatChunk(content=response_text)
                await self._emit_agent_event(
                    AgentEventType.AGENT_END,
                    {"session_id": session.id, "reason": before_agent_result.decision.value, "mode": profile.mode.value},
                    session.id,
                )
                self._set_state(AgentState.COMPLETED)
                if self.obs:
                    self.obs.end_span(root_span, trace_id=self._current_trace_id)
                    self.obs.end_trace()
                    self._current_trace_id = None
                return

            # === 正常对话：ReACT 循环 ===
            self.sessions.append_message(
                session.id,
                MessageRole.USER,
                user_input,
                metadata=dict(profile.user_message_metadata),
            )
            await self._emit_agent_event(
                AgentEventType.MESSAGE_END,
                {"role": "user", "content": user_input, "mode": profile.mode.value},
                session.id,
            )

            has_tool_calls = True
            consecutive_tool_failure_turns = 0
            while has_tool_calls and turn_count < self.max_react_turns:
                turn_count += 1
                self._last_turn_count = turn_count
                has_tool_calls = False

                await self._emit_agent_event(
                    AgentEventType.TURN_START,
                    {"turn": turn_count, "session_id": session.id},
                    session.id,
                )

                # 上下文组装
                self._set_state(AgentState.BUILDING_CONTEXT)
                ctx_span = self.obs.start_span("context.build", parent=root_span, trace_id=self._current_trace_id) if self.obs else None
                try:
                    context_messages = self._engine.build_context(
                        session.id,
                        profile,
                        compaction_plan=compaction_plan,
                    )
                    current_turn_usage = self._build_estimated_turn_usage(
                        context_messages,
                        compaction_plan=compaction_plan,
                    )
                    if ctx_span:
                        ctx_span.tags["usage.estimated_prompt_tokens"] = current_turn_usage.estimated_prompt_tokens
                        ctx_span.tags["usage.context_limit"] = current_turn_usage.context_limit
                        ctx_span.tags["usage.utilization_ratio"] = current_turn_usage.utilization_ratio
                finally:
                    if self.obs:
                        self.obs.end_span(ctx_span, trace_id=self._current_trace_id)

                self._set_state(AgentState.CALLING_LLM)

                # Provider 流式调用
                llm_span = self.obs.start_span("llm.stream", parent=root_span, trace_id=self._current_trace_id) if self.obs else None
                if self._chat_only_mode:
                    tools_for_llm = None
                else:
                    all_tools = self.tools.list_tools()
                    tools_for_llm = [tool for tool in all_tools if tool.name in profile.visible_tools]
                    if not tools_for_llm:
                        tools_for_llm = None
                params = ChatParams(
                    model=self.provider.default_model or "gpt-4o",
                    messages=context_messages,
                    tools=tools_for_llm,
                )

                await self.events.publish(
                    Event(
                        type="agent.llmCalled",
                        payload={"model": params.model, "message_count": len(params.messages)},
                        source="agent_loop",
                        session_id=session.id,
                    )
                )

                full_content = ""
                reasoning_content = ""
                tool_call_buffers: dict[int, dict[str, Any]] = {}
                stream_chunk_index = 0

                self._set_state(AgentState.STREAMING)

                await self._emit_agent_event(
                    AgentEventType.MESSAGE_START,
                    {"role": "assistant", "turn": turn_count},
                    session.id,
                )

                llm_error_occurred = False
                try:
                    async for chunk in self._engine.stream_chat_with_retry(session.id, params):
                        stream_result = await self.hooks.run_on_stream_chunk(
                            OnStreamChunkInput(
                                chunk_index=stream_chunk_index,
                                content=chunk.content or "",
                                reasoning_content=chunk.reasoning_content or "",
                                finish_reason=chunk.finish_reason,
                                context=self._build_hook_context(session, turn_id=turn_count),
                            ),
                            trace_span=llm_span,
                        )
                        self._consume_hook_side_effects("on_stream_chunk", stream_result, trace_span=llm_span)
                        if stream_result.content_override is not None:
                            chunk.content = stream_result.content_override
                        if stream_result.reasoning_content_override is not None:
                            chunk.reasoning_content = stream_result.reasoning_content_override

                        if chunk.content:
                            full_content += chunk.content
                        if chunk.reasoning_content:
                            reasoning_content += chunk.reasoning_content
                        stream_chunk_index += 1

                        provider_usage = self._extract_provider_usage(chunk)
                        if provider_usage is not None:
                            current_turn_usage = current_turn_usage.with_provider_usage(provider_usage)

                        # 累积 tool call
                        if chunk.tool_call:
                            tc = chunk.tool_call
                            idx = chunk.tool_call_index or 0
                            if idx not in tool_call_buffers:
                                tool_call_buffers[idx] = {
                                    "id": None,
                                    "type": "function",
                                    "function": {"name": None, "arguments": ""},
                                }
                            if tc.get("id"):
                                tool_call_buffers[idx]["id"] = tc["id"]
                            if tc.get("type"):
                                tool_call_buffers[idx]["type"] = tc["type"]
                            fn = tc.get("function", {})
                            if fn.get("name"):
                                tool_call_buffers[idx]["function"]["name"] = fn["name"]
                            if fn.get("arguments"):
                                tool_call_buffers[idx]["function"]["arguments"] += fn["arguments"]

                        await self._emit_agent_event(
                            AgentEventType.MESSAGE_UPDATE,
                            {"role": "assistant", "delta": chunk.content or "", "turn": turn_count},
                            session.id,
                        )
                        await self.events.publish(
                            Event(
                                type="agent.responseChunk",
                                payload={"content": chunk.content},
                                source="agent_loop",
                                session_id=session.id,
                            )
                        )
                        yield self._attach_turn_usage(chunk, current_turn_usage)

                except Exception as e:
                    logger.exception(f"[AgentLoop] LLM stream error: {e}")
                    yield ChatChunk(content="\n[System] LLM response stream interrupted. Initiating recovery...\n")
                    if llm_span:
                        llm_span.error = str(e)
                    error_message = str(e)
                    llm_error_occurred = True
                finally:
                    if self.obs:
                        self.obs.end_span(llm_span, trace_id=self._current_trace_id)

                # 流中断兜底
                if llm_error_occurred:
                    self.sessions.record_message_stream_failed(
                        session.id,
                        error_message or "llm_stream_interrupted",
                        metadata={"turn": turn_count, "mode": profile.mode.value},
                    )
                    if tool_call_buffers:
                        await self._ensure_tool_results_for_orphans(session, tool_call_buffers, reason="llm_stream_interrupted")

                    logger.warning(f"[AgentLoop] LLM stream interrupted, finalizing with chat-only mode")
                    yield ChatChunk(
                        content="\n[Notice] Tool execution was interrupted. "
                                "Generating a response based on available information...\n"
                    )

                    await self._emit_agent_event(
                        AgentEventType.TURN_END,
                        {"turn": turn_count, "has_tool_calls": len(tool_call_buffers) > 0, "session_id": session.id},
                        session.id,
                    )
                    await self._save_state_snapshot(
                        session,
                        turn_count,
                        [],
                        profile,
                        compaction_plan=compaction_plan,
                        error_message=error_message,
                    )

                    finalized = True
                    try:
                        async for chunk in self._finalize_with_llm(
                            session,
                            error_reason=error_message,
                            parent_span=root_span,
                            profile=profile,
                            compaction_plan=compaction_plan,
                        ):
                            yield chunk
                    except Exception as final_e:
                        logger.exception(f"[AgentLoop] Finalize LLM failed: {final_e}")
                        yield ChatChunk(
                            content="\n[Error] Unable to continue. Please try again later.\n",
                            metadata={"stream_error": True, "stream_error_reason": "finalize_failed"},
                        )
                        finalized = False
                        self._set_state(AgentState.ERROR)

                    await self.events.publish(Event(
                        type="agent.streamInterrupted",
                        payload={"reason": error_message, "finalized": finalized},
                        source="agent_loop",
                        session_id=session.id,
                    ))
                    break

                parsed_tool_calls = (
                    self._engine.parse_tool_calls(tool_call_buffers)
                    if tool_call_buffers
                    else []
                )

                response_metadata = {
                    "turn": turn_count,
                    "reasoning_content": reasoning_content,
                    "user_message_count": sum(
                        1 for message in context_messages if message.role == MessageRole.USER
                    ),
                    "turn_usage": current_turn_usage.model_dump(),
                }
                after_response_result = await self.hooks.run_after_response(
                    AfterResponseInput(
                        response_text=full_content,
                        tool_calls_present=bool(tool_call_buffers),
                        response_metadata=response_metadata,
                        context=self._build_hook_context(session, turn_id=turn_count),
                    ),
                    trace_span=root_span,
                )
                self._consume_hook_side_effects("after_response", after_response_result, trace_span=root_span)
                display_response_text = (
                    after_response_result.response_override
                    if after_response_result.response_override is not None
                    else full_content
                )
                assistant_metadata = {
                    **dict(profile.assistant_message_metadata),
                    "turn": turn_count,
                    "has_tool_calls": bool(parsed_tool_calls),
                    "reasoning_content": reasoning_content,
                    "turn_usage": current_turn_usage.model_dump(),
                    **after_response_result.extra_metadata,
                }
                assistant_entry = None
                if display_response_text.strip() or parsed_tool_calls or reasoning_content.strip():
                    assistant_entry = self.sessions.append_message(
                        session.id,
                        MessageRole.ASSISTANT,
                        display_response_text,
                        metadata=assistant_metadata,
                        tool_calls=parsed_tool_calls,
                    )

                await self._emit_agent_event(
                    AgentEventType.MESSAGE_END,
                    {
                        "role": "assistant",
                        "content": display_response_text,
                        "turn": turn_count,
                        "has_tool_calls": bool(parsed_tool_calls),
                    },
                    session.id,
                )

                await self.events.publish(
                    Event(
                        type="agent.responseDone",
                        payload={
                            "response_text": display_response_text,
                            "tool_calls_present": bool(tool_call_buffers),
                            "turn": turn_count,
                            "metadata": after_response_result.extra_metadata,
                            "turn_usage": current_turn_usage.model_dump(),
                        },
                        source="agent_loop",
                        session_id=session.id,
                    )
                )
                await self._publish_turn_usage_event(
                    session.id,
                    current_turn_usage,
                    turn=turn_count,
                    phase="response",
                )

                # 处理 tool calls
                turn_has_valid_tool_result = False
                if parsed_tool_calls:
                    has_tool_calls = True

                    self._set_state(AgentState.EXECUTING_TOOL)
                    tool_results = await self._tool_executor.execute_all(
                        session, parsed_tool_calls, turn_count, root_span, self._current_trace_id, self._build_hook_context
                    )

                    for outcome in tool_results:
                        tc = outcome["tool_call"]
                        result = outcome["result"]
                        is_error = outcome["is_error"]
                        result_metadata = dict(outcome.get("extra_metadata", {}))
                        display_result = outcome.get("display_result_override")
                        result_to_persist = display_result if display_result is not None else result
                        result_content = (
                            result_to_persist
                            if isinstance(result_to_persist, str)
                            else json.dumps(result_to_persist, ensure_ascii=False, default=str)
                        )
                        persisted_tool_result = {
                            "tool_id": tc.tool_id,
                            "tool_call_id": tc.call_id,
                            "result": result_content,
                            "is_error": is_error,
                        }
                        entry = self.sessions.append_message(
                            session.id,
                            MessageRole.TOOL,
                            result_content,
                            metadata={
                                "mode": profile.mode.value,
                                "tool_id": tc.tool_id,
                                "tool_call_id": tc.call_id or str(uuid.uuid4())[:8],
                                "is_error": is_error,
                                **result_metadata,
                            },
                            tool_results=[persisted_tool_result],
                        )
                        if not is_error:
                            turn_has_valid_tool_result = True

                    # 降级到纯对话判断
                    if turn_has_valid_tool_result:
                        consecutive_tool_failure_turns = 0
                    else:
                        consecutive_tool_failure_turns += 1
                        fallback_limit = self._resilience_config.react_turns_before_chat_fallback
                        if fallback_limit > 0 and consecutive_tool_failure_turns >= fallback_limit:
                            logger.warning(
                                f"[AgentLoop] Tool failures reached {consecutive_tool_failure_turns} "
                                f"consecutive turns. Downgrading to chat-only mode."
                            )
                            self._chat_only_mode = True
                            self._chat_only_success_turns = 0

                # Chat-Only 自动恢复逻辑
                if self._chat_only_mode:
                    recovery_limit = self._resilience_config.chat_only_recovery_turns
                    if recovery_limit > 0:
                        if not has_tool_calls:
                            self._chat_only_success_turns += 1
                            if self._chat_only_success_turns >= recovery_limit:
                                recovery_turns = self._chat_only_success_turns
                                logger.info(
                                    f"[AgentLoop] Auto-recovered from chat-only mode after "
                                    f"{recovery_turns} successful turns."
                                )
                                self._chat_only_mode = False
                                self._chat_only_success_turns = 0
                                consecutive_tool_failure_turns = 0
                                await self.events.publish(
                                    Event(
                                        type="agent.chatOnlyRecovered",
                                        payload={"recovery_turns": recovery_turns},
                                        source="agent_loop",
                                        session_id=session.id,
                                    )
                                )
                        else:
                            pass
                else:
                    self._chat_only_success_turns = 0

                await self._emit_agent_event(
                    AgentEventType.TURN_END,
                    {"turn": turn_count, "has_tool_calls": has_tool_calls, "session_id": session.id},
                    session.id,
                )
                await self._save_state_snapshot(
                    session,
                    turn_count,
                    pending_tool_calls,
                    profile,
                    compaction_plan=compaction_plan,
                    error_message=error_message,
                )

            await self._emit_agent_event(
                AgentEventType.AGENT_END,
                {"session_id": session.id, "turns": turn_count, "error": error_message},
                session.id,
            )

            self._set_state(AgentState.COMPLETED)
            if self.obs:
                self.obs.end_span(root_span, trace_id=self._current_trace_id)
                self.obs.end_trace()
                self._current_trace_id = None
                await self.events.publish(
                    Event(
                        type="agent.traceCompleted",
                        payload={"trace_id": trace.trace_id if trace else None},
                        source="agent_loop",
                        session_id=session.id,
                    )
                )

        except Exception as e:
            local_vars = locals()
            self.sessions.record_message_stream_failed(
                session.id,
                str(e),
                metadata={"mode": profile.mode.value, "phase": "agent_loop"},
            )
            if "tool_call_buffers" in local_vars and tool_call_buffers:
                await self._ensure_tool_results_for_orphans(session, tool_call_buffers, reason="agent_loop_error")

            yield ChatChunk(
                content="\n[Notice] An unexpected error occurred. "
                        "Attempting to generate a final response...\n"
            )

            try:
                final_messages = self._engine.build_context(
                    session.id,
                    profile,
                    compaction_plan=compaction_plan,
                )
                final_messages.append(ChatMessage(
                    role=MessageRole.SYSTEM,
                    content=f"Note: An unexpected error occurred. "
                            f"Please provide the best response possible based on available context.",
                ))
                final_params = ChatParams(
                    model=self.provider.default_model or "gpt-4o",
                    messages=final_messages,
                    tools=None,
                )
                async for chunk in self.provider.stream_chat(final_params):
                    yield chunk
            except Exception:
                yield ChatChunk(
                    content="\n[Error] Unable to continue. Please try again later.\n",
                    metadata={"stream_error": True, "stream_error_reason": "unhandled_error"},
                )

            self._set_state(AgentState.ERROR)
            logger.exception(f"[AgentLoop] Unhandled error: {e}")

            await self.events.publish(Event(
                type="agent.unhandledError",
                payload={"error": str(e), "session_id": session.id},
                source="agent_loop",
                session_id=session.id,
            ))

            if self.obs:
                self.obs.end_span(root_span, trace_id=self._current_trace_id)
                self.obs.end_trace()
                self._current_trace_id = None

    async def _run_single_pass_turn(
        self,
        session: LearningSession,
        user_input: str,
        profile: TurnExecutionProfile,
        parent_span: Optional[Any],
        compaction_plan: CompactionPlan | None = None,
    ) -> AsyncIterable[ChatChunk]:
        """执行单轮直答型 turn，不进入 ReACT 循环。"""
        self._set_state(AgentState.CALLING_LLM)
        self.sessions.append_message(
            session.id,
            MessageRole.USER,
            user_input,
            metadata=dict(profile.user_message_metadata),
        )
        await self._emit_agent_event(
            AgentEventType.MESSAGE_END,
            {"role": "user", "content": user_input, "mode": profile.mode.value},
            session.id,
        )

        single_pass_messages = self._engine.build_single_pass_messages(
            session.id,
            profile,
            compaction_plan=compaction_plan,
        )
        llm_span = self.obs.start_span(
            "turn.single_pass",
            parent=parent_span,
            trace_id=self._current_trace_id,
        ) if self.obs else None
        if llm_span:
            llm_span.tags["mode"] = profile.mode.value
            llm_span.tags["turn.kind"] = profile.turn_kind.value
        params = ChatParams(
            model=self.provider.default_model or "gpt-4o",
            messages=single_pass_messages,
        )
        current_turn_usage = self._build_estimated_turn_usage(
            single_pass_messages,
            compaction_plan=compaction_plan,
        )

        full_content = ""
        self._set_state(AgentState.STREAMING)
        try:
            async for chunk in self.provider.stream_chat(params):
                provider_usage = self._extract_provider_usage(chunk)
                if provider_usage is not None:
                    current_turn_usage = current_turn_usage.with_provider_usage(provider_usage)
                if chunk.content:
                    full_content += chunk.content
                yield self._attach_turn_usage(chunk, current_turn_usage)
        except Exception as e:
            logger.exception(f"[AgentLoop] Single-pass stream error: {e}")
            self.sessions.record_message_stream_failed(
                session.id,
                str(e),
                metadata={"mode": profile.mode.value, "phase": "single_pass"},
            )
            yield ChatChunk(
                content=f"\n[Error] Single-pass turn failed: {e}",
                metadata={"stream_error": True, "stream_error_reason": "single_pass_failed"},
            )
            if llm_span:
                llm_span.error = str(e)
        finally:
            if self.obs:
                self.obs.end_span(llm_span, trace_id=self._current_trace_id)

        if full_content.strip():
            self.sessions.append_message(
                session.id,
                MessageRole.ASSISTANT,
                full_content,
                metadata={
                    **dict(profile.assistant_message_metadata),
                    "turn_usage": current_turn_usage.model_dump(),
                },
            )
            await self._emit_agent_event(
                AgentEventType.MESSAGE_END,
                {"role": "assistant", "content": full_content, "mode": profile.mode.value},
                session.id,
            )
            await self._publish_turn_usage_event(
                session.id,
                current_turn_usage,
                phase="single_pass",
            )
        self._set_state(AgentState.COMPLETED)

    async def _finalize_with_llm(
        self,
        session: LearningSession,
        error_reason: str,
        parent_span: Optional[Any],
        profile: TurnExecutionProfile,
        compaction_plan: CompactionPlan | None = None,
    ) -> AsyncIterable[ChatChunk]:
        """流中断后的兜底 LLM 调用。"""
        final_messages = self._engine.build_context(
            session.id,
            profile,
            compaction_plan=compaction_plan,
        )
        final_messages.append(ChatMessage(
            role=MessageRole.SYSTEM,
            content=(
                f"Note: The previous tool execution was interrupted due to: {error_reason}. "
                "Please provide the best possible response based on the information already available. "
                "If you cannot answer fully, explain the limitation clearly to the user."
            ),
        ))
        final_params = ChatParams(
            model=self.provider.default_model or "gpt-4o",
            messages=final_messages,
            tools=None,
        )

        full_content = ""
        current_turn_usage = self._build_estimated_turn_usage(
            final_messages,
            compaction_plan=compaction_plan,
        )
        llm_span = self.obs.start_span("llm.finalize", parent=parent_span, trace_id=self._current_trace_id) if self.obs else None
        try:
            async for chunk in self.provider.stream_chat(final_params):
                provider_usage = self._extract_provider_usage(chunk)
                if provider_usage is not None:
                    current_turn_usage = current_turn_usage.with_provider_usage(provider_usage)
                if chunk.content:
                    full_content += chunk.content
                yield self._attach_turn_usage(chunk, current_turn_usage)
        finally:
            if self.obs:
                self.obs.end_span(llm_span, trace_id=self._current_trace_id)

        if full_content.strip():
            self.sessions.append_message(
                session.id,
                MessageRole.ASSISTANT,
                full_content,
                metadata={
                    "mode": profile.mode.value,
                    "turn": "finalize",
                    "reason": "stream_interrupted_recovery",
                    "turn_usage": current_turn_usage.model_dump(),
                },
            )
            await self._emit_agent_event(
                AgentEventType.MESSAGE_END,
                {"role": "assistant", "content": full_content, "turn": "finalize", "mode": profile.mode.value},
                session.id,
            )
            await self._publish_turn_usage_event(
                session.id,
                current_turn_usage,
                turn="finalize",
                phase="finalize",
            )

    async def _ensure_tool_results_for_orphans(
        self,
        session: LearningSession,
        tool_call_buffers: dict[int, dict[str, Any]],
        reason: str,
    ) -> None:
        """为孤儿 tool calls 生成 synthetic tool results 并写入 session。"""
        if not tool_call_buffers:
            return

        synthetic_messages = {
            "llm_stream_interrupted": (
                "[Tool Execution Interrupted] The tool call to '{tool_name}' was not executed "
                "because the language model stream was interrupted. No changes were made. "
                "Please retry this tool call or provide the answer directly."
            ),
            "agent_loop_error": (
                "[Tool Execution Skipped] The tool call to '{tool_name}' could not be completed "
                "due to an unexpected agent error. Please retry or answer directly."
            ),
        }

        call_ids = []
        for idx in sorted(tool_call_buffers.keys()):
            tc_buf = tool_call_buffers[idx]
            fn = tc_buf.get("function", {})
            tool_name = fn.get("name", "unknown")
            call_id = tc_buf.get("id") or f"call-{uuid.uuid4().hex[:8]}"
            call_ids.append(call_id)

            message_template = synthetic_messages.get(reason, synthetic_messages["agent_loop_error"])
            content = message_template.format(tool_name=tool_name)
            synthetic_tool_result = {
                "tool_id": tool_name,
                "tool_call_id": call_id,
                "result": content,
                "is_error": True,
            }

            entry = self.sessions.append_message(
                session.id,
                MessageRole.TOOL,
                content,
                metadata={
                    "tool_id": tool_name,
                    "tool_call_id": call_id,
                    "is_error": True,
                    "synthetic": True,
                    "reason": reason,
                },
                tool_results=[synthetic_tool_result],
            )

        await self.events.publish(Event(
            type="agent.orphanToolCallsCompensated",
            payload={"count": len(call_ids), "reason": reason, "call_ids": call_ids},
            source="agent_loop",
            session_id=session.id,
        ))

    # ── 向后兼容包装 ──

    async def _execute_tool_calls(
        self,
        session: LearningSession,
        tool_calls: list[ToolCall],
        turn_count: int,
        parent_span: Optional[Any],
    ) -> list[dict[str, Any]]:
        """向后兼容：委托给 ToolExecutor。"""
        return await self._tool_executor.execute_all(
            session, tool_calls, turn_count, parent_span, self._current_trace_id, self._build_hook_context
        )

    async def _stream_chat_with_retry(
        self,
        session: LearningSession,
        params: ChatParams,
    ) -> AsyncIterable[ChatChunk]:
        """向后兼容：委托给 ReActEngine。"""
        async for chunk in self._engine.stream_chat_with_retry(session.id, params):
            yield chunk

    def clear(self) -> None:
        """清理运行时状态（用于 session 被删除时）。"""
        self._failure_tracker.reset()
        self._chat_only_mode = False
        self._chat_only_success_turns = 0
        self.state = AgentState.IDLE
        self._last_turn_count = 0
