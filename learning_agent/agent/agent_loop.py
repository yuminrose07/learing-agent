"""
Agent Runtime 层主循环。

本模块只负责单次对话 turn 的运行时执行与 per-session runtime 状态管理：
- `AgentLoop` 负责 runtime 实例路由、生命周期与只读观测接口
- `AgentLoopSession` 负责具体 ReACT 执行、状态机、工具、事件与自愈

Memory 在本文件中仅以显式服务依赖的形式注入，不下沉为新的 session 私有状态。
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
from learning_agent.agent.tool_failure_tracker import ToolFailureTracker
from learning_agent.agent.tool_registry import ToolRegistry
from learning_agent.agent.tool_validator import ToolInputValidator
from learning_agent.memory.memory_manager import MemoryManager
from learning_agent.ai import (
    AgentEvent,
    AgentEventType,
    AgentStateSnapshot,
    AfterResponseInput,
    AfterToolExecuteInput,
    BeforeAgentRunInput,
    BeforeToolExecuteInput,
    AuthError,
    ChatChunk,
    ChatMessage,
    ChatParams,
    ContextComponent,
    ContextLengthError,
    EntryType,
    Event,
    HookContext,
    HookDecision,
    InvalidRequestError,
    LearningSession,
    MessageRole,
    OnStreamChunkInput,
    ResilienceConfig,
    RetryableError,
    SessionEntry,
    ToolCall,
    TraceSpan,
)
from learning_agent.ai.base_provider import BaseProvider
from learning_agent.learning_agent.session_manager import SessionManager

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
        agent_loop: AgentLoop,
        resilience_config: ResilienceConfig,
    ):
        self.session_id = session_id
        self.agent_loop = agent_loop

        # ── 运行时弹性策略状态（从 AgentLoop 迁移）──
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
        return self.agent_loop.tools

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
        error_message: Optional[str] = None,
    ) -> None:
        """保存全局状态快照，并通过事件发射。"""
        history = self.sessions.get_message_history(session.id)
        snapshot = AgentStateSnapshot(
            session_id=session.id,
            system_prompt=self.agent_loop._get_system_prompt(),
            messages=[
                {
                    "role": e.role.value if e.role else None,
                    "content": e.content,
                    "tool_calls": [tc.model_dump() for tc in e.tool_calls],
                    "tool_results": e.tool_results,
                    "timestamp": e.timestamp.isoformat(),
                }
                for e in history
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

    async def run_turn(
        self,
        session: LearningSession,
        user_input: str,
        ask_mode: bool = False,
    ) -> AsyncIterable[ChatChunk]:
        """执行一轮完整的 ReACT 循环。调用方已持有 session 级锁。"""
        trace = None
        root_span = None
        if self.obs:
            trace = self.obs.start_trace(session_id=session.id, objective_id=session.objective_id)
            self._current_trace_id = trace.trace_id if trace else None
            root_span = self.obs.start_span("agent.loop", trace_id=self._current_trace_id)

        self._set_state(AgentState.IDLE)
        turn_count = 0
        self._last_turn_count = 0
        pending_tool_calls: list[str] = []
        error_message: Optional[str] = None
        self._hook_runtime_metadata = {}

        try:
            await self._emit_agent_event(
                AgentEventType.AGENT_START,
                {"session_id": session.id, "user_input": user_input},
                session.id,
            )

            # === Ask 模式：多轮对齐，确认后才进入 ReACT ===
            is_aligning = session.ask_state.status == "aligning"
            if ask_mode or is_aligning:
                if self.agent_loop._is_confirmation(user_input):
                    # 用户确认，退出对齐，用最后一次对齐输出作为提示词进入 ReACT
                    confirmed = session.ask_state.confirmed_input
                    session.ask_state.status = "idle"
                    session.ask_state.confirmed_input = ""
                    user_input = confirmed or user_input
                    # 继续往下执行 ReACT，不 return
                else:
                    # 继续对齐轮（单轮 LLM，非 ReACT）
                    self._set_state(AgentState.ALIGNING)
                    session.ask_state.status = "aligning"
                    async for chunk in self._run_alignment_turn(session, user_input, root_span):
                        yield chunk
                    await self._emit_agent_event(
                        AgentEventType.AGENT_END,
                        {"session_id": session.id, "reason": "alignment_complete"},
                        session.id,
                    )
                    self._set_state(AgentState.COMPLETED)
                    if self.obs:
                        self.obs.end_span(root_span, trace_id=self._current_trace_id)
                        self.obs.end_trace()
                        self._current_trace_id = None
                    return

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
                        {
                            "id": tool.id,
                            "name": tool.name,
                            "description": tool.description,
                        }
                        for tool in self.tools.list_tools()
                    ],
                    context=self._build_hook_context(
                        session,
                        metadata={"ask_mode": ask_mode},
                    ),
                ),
                trace_span=root_span,
            )
            self._consume_hook_side_effects(
                "before_agent_run",
                before_agent_result,
                trace_span=root_span,
            )

            if before_agent_result.runtime_patch:
                self._hook_runtime_metadata.update(before_agent_result.runtime_patch)

            if before_agent_result.decision in {HookDecision.ASK, HookDecision.DENY}:
                response_text = (
                    before_agent_result.ask_message
                    if before_agent_result.decision == HookDecision.ASK
                    else before_agent_result.deny_reason
                ) or "Request blocked before agent run."
                self.sessions.append_message(session.id, MessageRole.ASSISTANT, response_text)
                await self._emit_agent_event(
                    AgentEventType.MESSAGE_END,
                    {"role": "assistant", "content": response_text, "turn": 0},
                    session.id,
                )
                yield ChatChunk(content=response_text)
                await self._emit_agent_event(
                    AgentEventType.AGENT_END,
                    {
                        "session_id": session.id,
                        "reason": before_agent_result.decision.value,
                    },
                    session.id,
                )
                self._set_state(AgentState.COMPLETED)
                if self.obs:
                    self.obs.end_span(root_span, trace_id=self._current_trace_id)
                    self.obs.end_trace()
                    self._current_trace_id = None
                return

            # === 正常对话：ReACT 循环 ===
            self.sessions.append_message(session.id, MessageRole.USER, user_input)
            await self._emit_agent_event(
                AgentEventType.MESSAGE_END,
                {"role": "user", "content": user_input},
                session.id,
            )

            # ReACT 内层循环
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

                # 5. 上下文组装
                self._set_state(AgentState.BUILDING_CONTEXT)
                ctx_span = self.obs.start_span("context.build", parent=root_span, trace_id=self._current_trace_id) if self.obs else None
                try:
                    context_messages = await self._build_context_for_turn(session, user_input)
                finally:
                    if self.obs:
                        self.obs.end_span(ctx_span, trace_id=self._current_trace_id)

                self._set_state(AgentState.CALLING_LLM)

                # 7. Provider 流式调用（带 Turn 级重试）
                llm_span = self.obs.start_span("llm.stream", parent=root_span, trace_id=self._current_trace_id) if self.obs else None
                tools_for_llm = None if self._chat_only_mode else (
                    self.tools.list_tools() if self.tools.list_tools() else None
                )
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

                # emit message_start for assistant
                await self._emit_agent_event(
                    AgentEventType.MESSAGE_START,
                    {"role": "assistant", "turn": turn_count},
                    session.id,
                )

                llm_error_occurred = False
                try:
                    async for chunk in self._stream_chat_with_retry(session, params):
                        stream_result = await self.hooks.run_on_stream_chunk(
                            OnStreamChunkInput(
                                chunk_index=stream_chunk_index,
                                content=chunk.content or "",
                                reasoning_content=chunk.reasoning_content or "",
                                finish_reason=chunk.finish_reason,
                                context=self._build_hook_context(
                                    session,
                                    turn_id=turn_count,
                                ),
                            ),
                            trace_span=llm_span,
                        )
                        self._consume_hook_side_effects(
                            "on_stream_chunk",
                            stream_result,
                            trace_span=llm_span,
                        )
                        if stream_result.content_override is not None:
                            chunk.content = stream_result.content_override
                        if stream_result.reasoning_content_override is not None:
                            chunk.reasoning_content = stream_result.reasoning_content_override

                        if chunk.content:
                            full_content += chunk.content
                        if chunk.reasoning_content:
                            reasoning_content += chunk.reasoning_content
                        stream_chunk_index += 1

                        # 累积 tool call（支持并行多个 tool calls）
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

                        # emit message_update
                        await self._emit_agent_event(
                            AgentEventType.MESSAGE_UPDATE,
                            {
                                "role": "assistant",
                                "delta": chunk.content or "",
                                "turn": turn_count,
                            },
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
                        yield chunk

                except Exception as e:
                    logger.exception(f"[AgentLoop] LLM stream error: {e}")
                    yield ChatChunk(
                        content="\n[System] LLM response stream interrupted. Initiating recovery...\n"
                    )
                    if llm_span:
                        llm_span.error = str(e)
                    error_message = str(e)
                    llm_error_occurred = True
                finally:
                    if self.obs:
                        self.obs.end_span(llm_span, trace_id=self._current_trace_id)

                # 【核心改动】流中断兜底：无重试循环，直接收尾
                if llm_error_occurred:
                    # 1. 补偿孤儿 tool calls
                    if tool_call_buffers:
                        await self._ensure_tool_results_for_orphans(
                            session, tool_call_buffers, reason="llm_stream_interrupted"
                        )

                    # 2. 直接调用收尾 LLM（无重试循环）
                    logger.warning(f"[AgentLoop] LLM stream interrupted, finalizing with chat-only mode")
                    yield ChatChunk(
                        content="\n[Notice] Tool execution was interrupted. "
                                "Generating a response based on available information...\n"
                    )

                    # 保存当前 turn 状态
                    await self._emit_agent_event(
                        AgentEventType.TURN_END,
                        {
                            "turn": turn_count,
                            "has_tool_calls": len(tool_call_buffers) > 0,
                            "session_id": session.id,
                        },
                        session.id,
                    )
                    await self._save_state_snapshot(session, turn_count, [], error_message)

                    # 调用收尾 LLM
                    finalized = True
                    try:
                        async for chunk in self._finalize_with_llm(session, user_input, error_message, root_span):
                            yield chunk
                    except Exception as final_e:
                        logger.exception(f"[AgentLoop] Finalize LLM failed: {final_e}")
                        yield ChatChunk(content="\n[Error] Unable to continue. Please try again later.\n")
                        finalized = False
                        self._set_state(AgentState.ERROR)

                    # 观测事件
                    await self.events.publish(Event(
                        type="agent.streamInterrupted",
                        payload={"reason": error_message, "finalized": finalized},
                        source="agent_loop",
                        session_id=session.id,
                    ))
                    break

                # emit message_end for assistant
                await self._emit_agent_event(
                    AgentEventType.MESSAGE_END,
                    {
                        "role": "assistant",
                        "content": full_content,
                        "turn": turn_count,
                        "has_tool_calls": len(tool_call_buffers) > 0,
                    },
                    session.id,
                )

                assistant_entry = None
                # 保存 assistant 消息（含 tool_calls + reasoning_content）
                if full_content.strip() or tool_call_buffers or reasoning_content.strip():
                    assistant_entry = self.sessions.append_message(
                        session.id,
                        MessageRole.ASSISTANT,
                        full_content,
                        metadata={
                            "turn": turn_count,
                            "has_tool_calls": len(tool_call_buffers) > 0,
                            "reasoning_content": reasoning_content,
                        },
                    )
                    if assistant_entry and tool_call_buffers:
                        for idx in sorted(tool_call_buffers.keys()):
                            tc_buf = tool_call_buffers[idx]
                            fn = tc_buf.get("function", {})
                            args_str = fn.get("arguments", "{}")
                            try:
                                args = json.loads(args_str)
                            except json.JSONDecodeError:
                                args = {}
                            
                            call_id = tc_buf.get("id") or f"call-{uuid.uuid4().hex[:8]}"
                            tc_buf["id"] = call_id  # ← 回写，确保后续 synthetic result 使用相同 id
                            
                            assistant_entry.tool_calls.append(
                                ToolCall(
                                    tool_id=fn.get("name", "unknown"),
                                    call_id=call_id,
                                    arguments=args,
                                )
                            )

                response_metadata = {
                    "turn": turn_count,
                    "reasoning_content": reasoning_content,
                    "user_message_count": len(
                        [
                            entry
                            for entry in session.entries
                            if entry.role == MessageRole.USER
                        ]
                    ),
                }
                after_response_result = await self.hooks.run_after_response(
                    AfterResponseInput(
                        response_text=full_content,
                        tool_calls_present=bool(tool_call_buffers),
                        response_metadata=response_metadata,
                        context=self._build_hook_context(
                            session,
                            turn_id=turn_count,
                        ),
                    ),
                    trace_span=root_span,
                )
                self._consume_hook_side_effects(
                    "after_response",
                    after_response_result,
                    trace_span=root_span,
                )
                display_response_text = (
                    after_response_result.response_override
                    if after_response_result.response_override is not None
                    else full_content
                )
                if assistant_entry:
                    assistant_entry.content = display_response_text
                    assistant_entry.metadata.update(after_response_result.extra_metadata)

                await self.events.publish(
                    Event(
                        type="agent.responseDone",
                        payload={
                            "response_text": display_response_text,
                            "tool_calls_present": bool(tool_call_buffers),
                            "turn": turn_count,
                            "metadata": after_response_result.extra_metadata,
                        },
                        source="agent_loop",
                        session_id=session.id,
                    )
                )

                # 处理 tool calls
                turn_has_valid_tool_result = False
                if tool_call_buffers:
                    has_tool_calls = True
                    tool_calls = self._parse_tool_calls(tool_call_buffers)

                    self._set_state(AgentState.EXECUTING_TOOL)
                    tool_results = await self._execute_tool_calls(session, tool_calls, turn_count, root_span)

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
                        entry = self.sessions.append_message(
                            session.id,
                            MessageRole.TOOL,
                            result_content,
                            metadata={
                                "tool_id": tc.tool_id,
                                "tool_call_id": tc.call_id or str(uuid.uuid4())[:8],
                                "is_error": is_error,
                                **result_metadata,
                            },
                        )
                        if entry:
                            entry.tool_results.append({
                                "tool_id": tc.tool_id,
                                "tool_call_id": tc.call_id,
                                "result": result_content,
                                "is_error": is_error,
                            })
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
                            self._chat_only_success_turns = 0  # 重置恢复计数

                # 【新增】Chat-Only 自动恢复逻辑
                if self._chat_only_mode:
                    recovery_limit = self._resilience_config.chat_only_recovery_turns
                    if recovery_limit > 0:
                        # 本 turn 没有工具调用需求（LLM 直接回答了），算一个"成功对话 turn"
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
                            # 本 turn 有工具调用但 chat-only 模式下被跳过了，不算恢复
                            pass
                else:
                    self._chat_only_success_turns = 0

                await self._emit_agent_event(
                    AgentEventType.TURN_END,
                    {
                        "turn": turn_count,
                        "has_tool_calls": has_tool_calls,
                        "session_id": session.id,
                    },
                    session.id,
                )

                # 保存状态快照
                await self._save_state_snapshot(session, turn_count, pending_tool_calls, error_message)

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
            # 1. 补偿孤儿 tool calls（保证历史完整）
            local_vars = locals()
            if "tool_call_buffers" in local_vars and tool_call_buffers:
                await self._ensure_tool_results_for_orphans(
                    session, tool_call_buffers, reason="agent_loop_error"
                )

            # 2. 通知用户
            yield ChatChunk(
                content="\n[Notice] An unexpected error occurred. "
                        "Attempting to generate a final response...\n"
            )

            # 3. 尝试收尾 LLM
            try:
                final_messages = await self._build_context_for_turn(session, user_input)
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
            except Exception as final_e:
                yield ChatChunk(content="\n[Error] Unable to continue. Please try again later.\n")

            # 4. 错误状态 + 观测
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

    async def _run_alignment_turn(
        self,
        session: LearningSession,
        user_input: str,
        parent_span: Optional[Any],
    ) -> AsyncIterable[ChatChunk]:
        """Ask 对齐轮（单轮，不进入 ReACT）。用户确认前可持续多轮对齐。"""
        self._set_state(AgentState.ALIGNING)
        self.sessions.append_message(
            session.id, MessageRole.USER, user_input,
            metadata={"ask_mode": True},
        )

        alignment_messages = self._build_alignment_messages(session, user_input)
        llm_span = self.obs.start_span("ask.alignment", parent=parent_span, trace_id=self._current_trace_id) if self.obs else None
        params = ChatParams(
            model=self.provider.default_model or "gpt-4o",
            messages=alignment_messages,
        )

        full_content = ""
        self._set_state(AgentState.STREAMING)
        try:
            async for chunk in self.provider.stream_chat(params):
                if chunk.content:
                    full_content += chunk.content
                yield chunk
        except Exception as e:
            logger.exception(f"[AgentLoop] Ask alignment stream error: {e}")
            yield ChatChunk(content=f"\n[Error] Alignment failed: {e}")
            if llm_span:
                llm_span.error = str(e)
        finally:
            if self.obs:
                self.obs.end_span(llm_span, trace_id=self._current_trace_id)

        # 保存对齐摘要，供用户确认后作为提示词使用
        session.ask_state.confirmed_input = full_content

        if full_content.strip():
            self.sessions.append_message(session.id, MessageRole.ASSISTANT, full_content)
        self._set_state(AgentState.COMPLETED)

    async def _build_context_for_turn(
        self,
        session: LearningSession,
        user_input: str,
    ) -> list[ChatMessage]:
        """为当前 turn 构建 LLM 上下文（包含完整消息历史，符合 OpenAI API 协议）。"""
        messages: list[ChatMessage] = []

        # 系统提示
        system_prompt = self.agent_loop._get_system_prompt()
        messages.append(ChatMessage(role=MessageRole.SYSTEM, content=system_prompt))

        # 会话历史（含 tool results），按 OpenAI API 协议格式组装
        history = self.sessions.get_message_history(session.id)

        # 新增：压缩同一工具的冗余 validation error 历史
        history = self._compress_tool_error_history(history)

        for entry in history:
            if entry.role == MessageRole.ASSISTANT and not entry.content.strip() and not entry.tool_calls:
                continue
            if entry.role == MessageRole.TOOL:
                tool_call_id = entry.metadata.get("tool_call_id", "")
                messages.append(ChatMessage(
                    role=MessageRole.TOOL,
                    content=entry.content,
                    tool_call_id=tool_call_id,
                ))
            elif entry.tool_calls:
                messages.append(ChatMessage(
                    role=entry.role,
                    content=entry.content,
                    tool_calls=[{
                        "id": tc.call_id or "",
                        "type": "function",
                        "function": {
                            "name": tc.tool_id,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False) if tc.arguments else "{}",
                        },
                    } for tc in entry.tool_calls],
                    reasoning_content=entry.metadata.get("reasoning_content") or "",
                ))
            else:
                messages.append(ChatMessage(role=entry.role, content=entry.content))

        return messages

    def _compress_tool_error_history(
        self,
        entries: list[SessionEntry],
    ) -> list[SessionEntry]:
        """
        对连续同一工具的 validation error / banned 进行压缩，最多保留最近 N 组。
        一组 = assistant entry(含 tool_calls) + 紧随其后的 tool error entry。
        成功的 tool result、权限 ask/abort、系统消息永不压缩。
        """
        max_groups = self._resilience_config.tool_max_validation_history_groups
        if max_groups <= 0:
            return entries

        # 扫描所有 (assistant_idx, tool_id) 组合，标记哪些 error group 需要保留
        # 从旧到新扫描
        tool_group_counts: dict[str, int] = {}
        skip_indices: set[int] = set()

        i = 0
        while i < len(entries):
            entry = entries[i]
            if entry.role == MessageRole.ASSISTANT and entry.tool_calls:
                # 检查紧随其后的 tool result 是否为 validation/banned/error
                if i + 1 < len(entries) and entries[i + 1].role == MessageRole.TOOL:
                    next_entry = entries[i + 1]
                    is_error = next_entry.metadata.get("is_error", False)
                    tool_id = next_entry.metadata.get("tool_id", "")
                    result_text = next_entry.content
                    # 只压缩 validation / banned / unavailable 类错误
                    if is_error and result_text and (
                        result_text.startswith("[Tool Input Validation Failed]")
                        or result_text.startswith("[Tool Unavailable]")
                    ):
                        tool_group_counts[tool_id] = tool_group_counts.get(tool_id, 0) + 1
                        if tool_group_counts[tool_id] > max_groups:
                            # 超出限制，标记这组跳过
                            skip_indices.add(i)
                            skip_indices.add(i + 1)
                        i += 1  # 跳过紧随的 tool result
            i += 1

        return [e for idx, e in enumerate(entries) if idx not in skip_indices]

    async def _stream_chat_with_retry(
        self,
        session: LearningSession,
        params: ChatParams,
    ) -> AsyncIterable[ChatChunk]:
        """
        带 Turn 级重试的 LLM 流式调用。
        成功时逐 chunk yield；最终失败时 yield 错误信息 chunk。
        """
        max_attempts = self._resilience_config.turn_retry_max_attempts
        backoff_base = self._resilience_config.provider_retry_backoff_base
        max_delay = self._resilience_config.provider_retry_max_delay

        for attempt in range(max_attempts + 1):
            try:
                async for chunk in self.provider.stream_chat(params):
                    yield chunk
                return
            except ContextLengthError as e:
                # 上下文压缩已从核心层移除，交由 BEFORE_CONTEXT_BUILD hook 或扩展层实现
                yield ChatChunk(content=f"\n[Error] Context length exceeded: {e}\n")
                return
            except RetryableError as e:
                if attempt < max_attempts:
                    delay = min(backoff_base ** attempt, max_delay)
                    await self.events.publish(
                        Event(
                            type="agent.turnRetry",
                            payload={"attempt": attempt + 1, "reason": str(e)},
                            source="agent_loop",
                            session_id=session.id,
                        )
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
            except (AuthError, InvalidRequestError) as e:
                yield ChatChunk(content=f"\n[Error] {e}\n")
                return
            except Exception as e:
                # 未知异常，不归入重试，向上传播以触发兜底流程
                raise

    def _parse_tool_calls(self, tool_call_buffers: dict[int, dict[str, Any]]) -> list[ToolCall]:
        """解析 OpenAI 格式的 tool call buffer 为 ToolCall 列表（支持并行 tool calls）。"""
        result: list[ToolCall] = []
        for idx in sorted(tool_call_buffers.keys()):
            tc = tool_call_buffers[idx]
            fn = tc.get("function", {})
            args_str = fn.get("arguments", "{}")
            try:
                args = json.loads(args_str)
            except json.JSONDecodeError:
                args = {}
            result.append(ToolCall(
                tool_id=fn.get("name", "unknown"),
                call_id=tc.get("id") or f"call-{uuid.uuid4().hex[:8]}",
                arguments=args,
            ))
        return result

    async def _execute_tool_calls(
        self,
        session: LearningSession,
        tool_calls: list[ToolCall],
        turn_count: int,
        parent_span: Optional[Any],
    ) -> list[dict[str, Any]]:
        """
        执行工具调用流水线，返回结构化结果列表。

        流水线：
        1. emit TOOL_EXECUTION_START
        2. ToolFailureTracker.is_banned()?
        3. ToolInputValidator.validate()
        4. before_tool_execute
        5. ToolRegistry.execute()
        6. after_tool_execute
        7. emit TOOL_EXECUTION_END
        8. 结果保存到 SessionEntry（由调用方处理）
        """
        results: list[dict[str, Any]] = []
        for tc in tool_calls:
            tool_span = self.obs.start_span("tool.execute", parent=parent_span, trace_id=self._current_trace_id) if self.obs else None
            try:
                tool_call_id = getattr(tc, "call_id", "") or str(uuid.uuid4())[:8]
                tool_def = self.tools.get(tc.tool_id)
                base_context = self._build_hook_context(
                    session,
                    turn_id=turn_count,
                    metadata={"tool_call_id": tool_call_id},
                )

                # Step 1: emit TOOL_EXECUTION_START
                await self._emit_agent_event(
                    AgentEventType.TOOL_EXECUTION_START,
                    {
                        "tool_call_id": tool_call_id,
                        "tool_id": tc.tool_id,
                        "arguments": tc.arguments,
                    },
                    session.id,
                )

                # Step 2: 检查工具是否被临时禁用
                if self._failure_tracker.is_banned(tc.tool_id, turn_count):
                    ban_msg = self._failure_tracker.get_ban_message(tc.tool_id)
                    tc.error = ban_msg
                    logger.warning(f"[AgentLoop] Tool '{tc.tool_id}' is temporarily banned.")
                    failures_in_window = self._failure_tracker.get_failures_in_window(tc.tool_id, turn_count)
                    failure_types = self._failure_tracker.get_all_failures_in_window(tc.tool_id, turn_count)
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
                    results.append(
                        {
                            "tool_call": tc,
                            "result": ban_msg,
                            "is_error": True,
                            "display_result_override": None,
                            "extra_metadata": {"reason": "banned"},
                        }
                    )
                    await self._emit_agent_event(
                        AgentEventType.TOOL_EXECUTION_END,
                        {
                            "tool_call_id": tool_call_id,
                            "tool_id": tc.tool_id,
                            "result": ban_msg,
                            "is_error": True,
                            "reason": "banned",
                        },
                        session.id,
                    )
                    continue

                # Step 3: 输入参数校验
                if self._resilience_config.tool_validation_enabled:
                    validation_errors = await self._validator.validate(tc)
                    if validation_errors is not None:
                        self._failure_tracker.record_failure(tc.tool_id, turn_count, reason="validation_failed")
                        error_message = self._validator.format_validation_error(
                            tc.tool_id, tool_call_id, validation_errors
                        )
                        tc.error = error_message
                        logger.warning(f"[AgentLoop] Tool '{tc.tool_id}' validation failed: {validation_errors}")
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
                        results.append(
                            {
                                "tool_call": tc,
                                "result": error_message,
                                "is_error": True,
                                "display_result_override": None,
                                "extra_metadata": {"reason": "validation"},
                            }
                        )
                        await self._emit_agent_event(
                            AgentEventType.TOOL_EXECUTION_END,
                            {
                                "tool_call_id": tool_call_id,
                                "tool_id": tc.tool_id,
                                "result": error_message,
                                "is_error": True,
                                "reason": "validation",
                            },
                            session.id,
                        )
                        continue

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
                self._consume_hook_side_effects(
                    "before_tool_execute",
                    before_tool_result,
                    trace_span=tool_span,
                )

                if before_tool_result.decision == HookDecision.ASK:
                    ask_message = before_tool_result.ask_message or "Tool call requires approval"
                    tc.error = ask_message
                    logger.info(f"[AgentLoop] Tool call '{tc.tool_id}' requires approval: {ask_message}")
                    await self.events.publish(
                        Event(
                            type="agent.toolPermissionAsk",
                            payload={"tool_id": tc.tool_id, "call_id": tool_call_id, "message": ask_message},
                            source="agent_loop",
                            session_id=session.id,
                        )
                    )
                    results.append(
                        {
                            "tool_call": tc,
                            "result": f"[Approval Required] {ask_message}",
                            "is_error": True,
                            "display_result_override": None,
                            "extra_metadata": {
                                "reason": "ask",
                                **before_tool_result.annotations,
                            },
                        }
                    )
                    await self._emit_agent_event(
                        AgentEventType.TOOL_EXECUTION_END,
                        {
                            "tool_call_id": tool_call_id,
                            "tool_id": tc.tool_id,
                            "result": ask_message,
                            "is_error": True,
                            "reason": "ask",
                        },
                        session.id,
                    )
                    continue

                if before_tool_result.decision == HookDecision.DENY:
                    deny_reason = before_tool_result.deny_reason or "Tool call denied by guard policy"
                    tc.error = deny_reason
                    self._failure_tracker.record_failure(tc.tool_id, turn_count, reason="hook_abort")
                    logger.warning(f"[AgentLoop] Tool call '{tc.tool_id}' denied: {deny_reason}")
                    await self.events.publish(
                        Event(
                            type="agent.toolPermissionDenied",
                            payload={"tool_id": tc.tool_id, "call_id": tool_call_id, "reason": deny_reason},
                            source="agent_loop",
                            session_id=session.id,
                        )
                    )
                    results.append(
                        {
                            "tool_call": tc,
                            "result": f"[Blocked] {deny_reason}",
                            "is_error": True,
                            "display_result_override": None,
                            "extra_metadata": {
                                "reason": "deny",
                                **before_tool_result.annotations,
                            },
                        }
                    )
                    await self._emit_agent_event(
                        AgentEventType.TOOL_EXECUTION_END,
                        {
                            "tool_call_id": tool_call_id,
                            "tool_id": tc.tool_id,
                            "result": deny_reason,
                            "is_error": True,
                            "reason": "deny",
                        },
                        session.id,
                    )
                    continue

                if before_tool_result.patched_arguments:
                    tc.arguments = {
                        **tc.arguments,
                        **before_tool_result.patched_arguments,
                    }

                await self.events.publish(
                    Event(
                        type="agent.toolCalled",
                        payload={"tool_id": tc.tool_id, "arguments": tc.arguments},
                        source="agent_loop",
                        session_id=session.id,
                    )
                )

                # Step 5: ToolRegistry.execute() 带重试
                max_tool_retries = self._resilience_config.max_tool_retries
                tool_retry_count = 0
                last_error = None
                success = False
                final_result: Any = None
                final_error: Optional[str] = None

                while True:
                    try:
                        timeout = self._resilience_config.tool_default_timeout
                        result = await self.tools.execute(tc, timeout=timeout)
                        tc.result = result
                        final_result = result
                        final_error = None
                        success = True
                        self._failure_tracker.record_success(tc.tool_id)
                        await self.events.publish(
                            Event(
                                type="agent.toolResult",
                                payload={"tool_id": tc.tool_id, "success": True, "result": result},
                                source="agent_loop",
                                session_id=session.id,
                            )
                        )
                        await self._emit_agent_event(
                            AgentEventType.TOOL_EXECUTION_END,
                            {
                                "tool_call_id": tool_call_id,
                                "tool_id": tc.tool_id,
                                "result": result,
                                "is_error": False,
                            },
                            session.id,
                        )
                        break
                    except Exception as e:
                        last_error = e
                        is_retryable = self.agent_loop._is_retryable_tool_error(e)

                        if tool_retry_count < max_tool_retries and is_retryable:
                            tool_retry_count += 1
                            delay = min(
                                self._resilience_config.tool_retry_base_delay * (2 ** (tool_retry_count - 1)),
                                self._resilience_config.tool_retry_max_delay,
                            )
                            logger.warning(
                                f"[AgentLoop] Tool '{tc.tool_id}' failed transiently "
                                f"(attempt {tool_retry_count}/{max_tool_retries + 1}), "
                                f"retrying in {delay}s..."
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
                            # 重试耗尽或非可重试异常 → 记录失败并返回错误
                            self._failure_tracker.record_failure(tc.tool_id, turn_count, reason="execution_error")
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
                                AgentEventType.TOOL_EXECUTION_END,
                                {
                                    "tool_call_id": tool_call_id,
                                    "tool_id": tc.tool_id,
                                    "result": str(last_error),
                                    "is_error": True,
                                    "reason": "execution_error",
                                },
                                session.id,
                            )
                            break

                after_tool_result = await self.hooks.run_after_tool_execute(
                    AfterToolExecuteInput(
                        tool_call_id=tool_call_id,
                        tool_name=tc.tool_id,
                        arguments=dict(tc.arguments),
                        success=success,
                        result=final_result,
                        error=final_error,
                        duration_ms=tc.duration_ms or 0,
                        retry_count=tool_retry_count,
                        annotations=dict(before_tool_result.annotations),
                        context=base_context,
                    ),
                    trace_span=tool_span,
                )
                self._consume_hook_side_effects(
                    "after_tool_execute",
                    after_tool_result,
                    trace_span=tool_span,
                )
                results.append(
                    {
                        "tool_call": tc,
                        "result": final_result,
                        "is_error": not success,
                        "display_result_override": after_tool_result.display_result_override,
                        "extra_metadata": {
                            **before_tool_result.annotations,
                            **after_tool_result.extra_metadata,
                        },
                    }
                )
            finally:
                if self.obs:
                    self.obs.end_span(tool_span, trace_id=self._current_trace_id)

        return results

    async def _ensure_tool_results_for_orphans(
        self,
        session: LearningSession,
        tool_call_buffers: dict[int, dict[str, Any]],
        reason: str,
    ) -> None:
        """
        为孤儿 tool calls 生成 synthetic tool results 并写入 session。
        """
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
            )
            if entry:
                entry.tool_results.append({
                    "tool_id": tool_name,
                    "tool_call_id": call_id,
                    "result": content,
                    "is_error": True,
                })

        await self.events.publish(Event(
            type="agent.orphanToolCallsCompensated",
            payload={"count": len(call_ids), "reason": reason, "call_ids": call_ids},
            source="agent_loop",
            session_id=session.id,
        ))

    async def _finalize_with_llm(
        self,
        session: LearningSession,
        user_input: str,
        error_reason: str,
        parent_span: Optional[Any],
    ) -> AsyncIterable[ChatChunk]:
        """
        流中断后的收尾 LLM 调用。
        """
        final_messages = await self._build_context_for_turn(session, user_input)
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
        llm_span = self.obs.start_span("llm.finalize", parent=parent_span, trace_id=self._current_trace_id) if self.obs else None
        try:
            async for chunk in self.provider.stream_chat(final_params):
                if chunk.content:
                    full_content += chunk.content
                yield chunk
        finally:
            if self.obs:
                self.obs.end_span(llm_span, trace_id=self._current_trace_id)

        if full_content.strip():
            self.sessions.append_message(
                session.id,
                MessageRole.ASSISTANT,
                full_content,
                metadata={"turn": "finalize", "reason": "stream_interrupted_recovery"},
            )

    def _build_alignment_messages(
        self,
        session: LearningSession,
        user_input: str,
    ) -> list[ChatMessage]:
        """构建 Ask 对齐轮的上下文消息。"""
        messages: list[ChatMessage] = []
        system_prompt = (
            "You are a learning assistant in **Alignment Mode**.\n\n"
            "The user has submitted a question or task. BEFORE you answer, "
            "you must:\n"
            "1. Restate the user's intent in one sentence.\n"
            "2. Briefly outline your planned approach to answer (1-2 sentences).\n"
            "3. Ask the user to confirm or clarify.\n\n"
            "Do NOT provide the detailed answer yet. "
            "Keep your response concise (under 150 words)."
        )
        messages.append(ChatMessage(role=MessageRole.SYSTEM, content=system_prompt))

        history = self.sessions.get_message_history(session.id)
        for entry in history:
            messages.append(ChatMessage(role=entry.role, content=entry.content))

        messages.append(ChatMessage(role=MessageRole.USER, content=user_input))
        return messages

    def clear(self) -> None:
        """清理运行时状态（用于 session 被删除时）。"""
        self._failure_tracker.reset()
        self._chat_only_mode = False
        self._chat_only_success_turns = 0
        self.state = AgentState.IDLE
        self._last_turn_count = 0


class AgentLoop:
    """
    Agent Runtime 层路由器。

    它持有运行时共享依赖，按 session 管理 `AgentLoopSession` 实例，并向上层
    暴露稳定的只读 runtime 摘要。它不是产品层编排器，也不拥有 session 数据真源。
    """

    def __init__(
        self,
        provider: BaseProvider,
        memory_manager: MemoryManager,
        session_manager: SessionManager,
        hook_system: HookSystem,
        event_bus: EventBus,
        tool_registry: ToolRegistry,
        observability: Optional[ObservabilityCollector] = None,
        max_react_turns: int = 10,
        resilience_config: Optional[ResilienceConfig] = None,
        session_runtime_ttl: int = 3600,
    ):
        # Runtime 共享依赖
        self.provider = provider
        self.memory_service = memory_manager
        self.sessions = session_manager
        self.hooks = hook_system
        self.events = event_bus
        self.tools = tool_registry
        self.obs = observability

        # 全局配置
        self.max_react_turns = max_react_turns
        self._resilience_config = resilience_config or ResilienceConfig()
        self._validator = ToolInputValidator(tool_registry)

        # per-session 运行时容器
        self._session_runtimes: dict[str, AgentLoopSession] = {}
        self._session_last_accessed: dict[str, float] = {}
        self._session_runtime_ttl = session_runtime_ttl

        # 注册 session 删除回调（如果 SessionManager 支持）
        if hasattr(self.sessions, "register_delete_callback"):
            self.sessions.register_delete_callback(self.clear_session_runtime)

    # ── 公共接口 ──

    async def run(
        self,
        session: LearningSession,
        user_input: str,
        ask_mode: bool = False,
    ) -> AsyncIterable[ChatChunk]:
        """
        执行一轮 Agent 循环。
        获取或创建 per-session 运行时，委托执行。
        """
        runtime = self._get_or_create_runtime(session.id)
        async with runtime.lock:
            async for chunk in runtime.run_turn(session, user_input, ask_mode):
                yield chunk

    def clear_runtime(self, session_id: str) -> dict[str, Any]:
        """
        清理指定 session 的运行时状态，并返回被清理 runtime 的稳定摘要。

        返回结构仅包含只读摘要，不暴露内部 runtime 对象。
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
        """
        清理指定 session 的运行时状态。
        由 SessionManager 在删除 session 时回调。
        """
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
        """返回当前运行时的稳定摘要，供上层观测层读取。"""
        runtimes = self.list_runtime_summaries()
        return {
            "active_runtime_count": len(runtimes),
            "total_session_count": len(self.sessions.list_sessions()),
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

        # 检查常见 HTTP/服务错误关键词
        error_str = str(error).lower()
        retryable_keywords = [
            "503", "502", "504", "429",
            "timeout", "connection reset", "connection refused",
            "temporarily unavailable", "service unavailable",
            "too many requests", "rate limit",
        ]
        return any(kw in error_str for kw in retryable_keywords)

    @staticmethod
    def _is_confirmation(user_input: str) -> bool:
        """判断用户输入是否为对齐确认。"""
        text = user_input.strip().lower()
        negation_patterns = {
            "不对", "不好", "不行", "不要", "不用", "不可以", "不能",
            "没", "没有", "否", "不是", "错了", "别",
            "no", "not", "don't", "dont", "cannot", "can't", "cant",
            "won't", "wouldn't", "nope", "wrong", "incorrect",
        }
        for neg in negation_patterns:
            if neg in text:
                return False

        confirm_keywords = {
            "确认", "是的", "没错", "ok", "好", "好的",
            "可以", "行", "没问题", "正确", "就这样", "开始吧",
            "yes", "y", "sure", "confirm", "correct", "go ahead",
            "please proceed", "proceed", "do it", "准备好了",
        }
        for kw in confirm_keywords:
            if text == kw or text.startswith(kw + "，") or text.startswith(kw + ",") or text.startswith(kw + " "):
                return True
        return False
