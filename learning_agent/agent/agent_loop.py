"""
Agent 循环：执行 Agent 主循环，协调 LLM 与工具，解析意图，驱动输出。

流式架构：
用户输入 → Agent Loop 启动 → 触发 Hook → Provider.streamChat()
→ 逐 chunk 处理 → 触发 Hook → 发布 Event → 实时推送
→ 流结束 → 触发 Hook → 工具执行 → 结果回流
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterable, Optional

from learning_agent.core.event_bus import EventBus
from learning_agent.core.hook_system import HookAbortError, HookPoint, HookSystem
from learning_agent.core.observability import ObservabilityCollector
from learning_agent.core.tool_registry import ToolRegistry
from learning_agent.memory.memory_manager import MemoryManager
from learning_agent.models import (
    ChatChunk,
    ChatMessage,
    ChatParams,
    ContextComponent,
    Event,
    IntentResult,
    LearningSession,
    MessageRole,
    ToolCall,
    TraceSpan,
)
from learning_agent.provider.base_provider import BaseProvider
from learning_agent.session.session_manager import SessionManager

logger = logging.getLogger(__name__)


class AgentLoop:
    """
    Agent 主循环。
    不持久化任何状态，循环结束即清空运行时状态。
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
    ):
        self.provider = provider
        self.memory = memory_manager
        self.sessions = session_manager
        self.hooks = hook_system
        self.events = event_bus
        self.tools = tool_registry
        self.obs = observability

    async def run(
        self,
        session: LearningSession,
        user_input: str,
    ) -> AsyncIterable[ChatChunk]:
        """
        执行一轮 Agent 循环（流式版）。
        返回 AsyncIterable[ChatChunk]，调用方可实时渲染。
        """
        # ─── Trace 开始 ───
        trace = None
        root_span = None
        if self.obs:
            trace = self.obs.start_trace(session_id=session.id, objective_id=session.objective_id)
            root_span = self.obs.start_span("agent.loop")

        try:
            # 1. 追加用户消息到会话树
            self.sessions.append_message(session.id, MessageRole.USER, user_input)

            # 2. 触发 beforeIntentParse
            try:
                hook_result = await self.hooks.execute(
                    HookPoint.BEFORE_INTENT_PARSE,
                    user_input,
                    {"session": session, "memory": self.memory},
                    trace_span=root_span,
                )
                if hook_result.modified:
                    user_input = hook_result.data
            except HookAbortError as e:
                yield ChatChunk(content=f"[Clarification needed] {e}")
                return

            # 3. 意图解析（简化版，后续由 core-intent 扩展接管）
            intent_span = self.obs.start_span("intent.parse") if self.obs else None
            intent = self._parse_intent(user_input)
            if self.obs:
                self.obs.end_span(intent_span)
            await self.events.publish(
                Event(
                    type="agent.intentParsed",
                    payload=intent.model_dump(),
                    source="agent_loop",
                    session_id=session.id,
                )
            )

            if intent.needs_clarification:
                yield ChatChunk(content=f"[Clarification] {intent.clarification_question}")
                return

            # 4. 触发 afterIntentParse
            await self.hooks.execute(
                HookPoint.AFTER_INTENT_PARSE,
                intent,
                {"session": session, "memory": self.memory},
                trace_span=root_span,
            )

            # 5. 上下文组装
            ctx_span = self.obs.start_span("context.build") if self.obs else None
            context_messages = await self._build_context(session, user_input, intent)
            if self.obs:
                self.obs.end_span(ctx_span)

            # 6. 触发 beforeLLMCall
            hook_result = await self.hooks.execute(
                HookPoint.BEFORE_LLM_CALL,
                context_messages,
                {"session": session, "memory": self.memory, "intent": intent},
                trace_span=root_span,
            )
            if hook_result.modified:
                context_messages = hook_result.data

            # 7. Provider 流式调用
            llm_span = self.obs.start_span("llm.stream") if self.obs else None
            params = ChatParams(
                model="gpt-4o",
                messages=context_messages,
                tools=self.tools.list_tools() if self.tools.list_tools() else None,
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
            tool_call_buffer: Optional[dict[str, Any]] = None

            try:
                async for chunk in self.provider.stream_chat(params):
                    # Hook: onStreamChunk
                    await self.hooks.execute(
                        HookPoint.ON_STREAM_CHUNK,
                        chunk,
                        {"session": session},
                        trace_span=llm_span,
                    )

                    # 累积内容
                    if chunk.content:
                        full_content += chunk.content

                    # 累积 tool call
                    if chunk.tool_call:
                        tc = chunk.tool_call
                        if tc.get("id"):
                            tool_call_buffer = tc
                        elif tool_call_buffer and tc.get("function", {}).get("arguments"):
                            tool_call_buffer["function"]["arguments"] += tc["function"]["arguments"]

                    # 发布事件
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
                yield ChatChunk(content=f"\n[Error] LLM stream failed: {e}")
                if llm_span:
                    llm_span.error = str(e)
                return
            finally:
                if self.obs:
                    self.obs.end_span(llm_span)

            # 8. 追加助手消息到会话树
            self.sessions.append_message(session.id, MessageRole.ASSISTANT, full_content)

            # 9. 触发 afterResponse
            await self.hooks.execute(
                HookPoint.AFTER_RESPONSE,
                full_content,
                {"session": session, "memory": self.memory, "intent": intent},
                trace_span=root_span,
            )

            # 10. 处理工具调用
            if tool_call_buffer:
                await self._handle_tool_call(session, tool_call_buffer, root_span)

            # 11. 结束 Trace
            if self.obs:
                self.obs.end_span(root_span)
                self.obs.end_trace()
                await self.events.publish(
                    Event(
                        type="agent.traceCompleted",
                        payload={"trace_id": trace.trace_id if trace else None},
                        source="agent_loop",
                        session_id=session.id,
                    )
                )

        except Exception as e:
            logger.exception(f"[AgentLoop] Unhandled error: {e}")
            yield ChatChunk(content=f"\n[Error] Agent loop failed: {e}")
            if self.obs:
                self.obs.end_span(root_span)
                self.obs.end_trace()

    def _parse_intent(self, user_input: str) -> IntentResult:
        """
        极度简化的意图解析（占位实现）。
        实际逻辑由内置扩展 core-intent 通过 Hook 接管。
        """
        # 简单规则：如果输入很短且模糊，触发澄清
        if len(user_input.strip()) < 5:
            return IntentResult(
                type="clarify",
                confidence=0.3,
                needs_clarification=True,
                clarification_question="您的输入似乎不太完整，能否再详细说明一下？",
            )
        return IntentResult(type="chat", confidence=0.9)

    async def _build_context(
        self,
        session: LearningSession,
        user_input: str,
        intent: IntentResult,
    ) -> list[ChatMessage]:
        """
        组装 LLM 上下文：
        - 系统提示
        - 当前会话历史
        - Relevant Memories（L2/L3 召回）
        - 间隔重复提醒
        - 用户输入
        """
        messages: list[ChatMessage] = []

        # 系统提示
        system_prompt = (
            "You are a learning assistant. Help the user understand concepts deeply, "
            "ask clarifying questions when needed, and encourage active recall."
        )
        messages.append(ChatMessage(role=MessageRole.SYSTEM, content=system_prompt))

        # 相关记忆召回
        recalled = self.memory.relevant_recall(user_input, limit=3)
        if recalled:
            memory_prompt = "Relevant knowledge from your memory:\n"
            for node in recalled:
                memory_prompt += f"- [{node.type.value}] {node.content}\n"
            messages.append(ChatMessage(role=MessageRole.SYSTEM, content=memory_prompt))

        # 到期复习提醒
        due_reviews = self.memory.get_due_reviews()[:1]
        if due_reviews:
            review_prompt = "Review reminder:\n"
            for node in due_reviews:
                review_prompt += f"- You previously learned: '{node.content}'. Do you still remember?\n"
            messages.append(ChatMessage(role=MessageRole.SYSTEM, content=review_prompt))

        # 会话历史
        history = self.sessions.get_message_history(session.id)
        for entry in history[-20:]:  # 保留最近 20 条
            messages.append(ChatMessage(role=entry.role, content=entry.content))

        # 用户输入
        messages.append(ChatMessage(role=MessageRole.USER, content=user_input))

        return messages

    async def _handle_tool_call(
        self,
        session: LearningSession,
        tool_call: dict[str, Any],
        parent_span: Optional[Any],
    ) -> None:
        """处理工具调用。"""
        tool_span = self.obs.start_span("tool.execute") if self.obs else None
        tool_id = tool_call.get("function", {}).get("name", "unknown")
        arguments_str = tool_call.get("function", {}).get("arguments", "{}")
        try:
            arguments = json.loads(arguments_str)
        except json.JSONDecodeError:
            arguments = {}

        tc = ToolCall(tool_id=tool_id, arguments=arguments)

        await self.hooks.execute(
            HookPoint.ON_TOOL_CALL,
            tc,
            {"session": session},
            trace_span=tool_span,
        )

        await self.events.publish(
            Event(
                type="agent.toolCalled",
                payload={"tool_id": tool_id, "arguments": arguments},
                source="agent_loop",
                session_id=session.id,
            )
        )

        try:
            result = await self.tools.execute(tc)
            tc.result = result
            await self.events.publish(
                Event(
                    type="agent.toolResult",
                    payload={"tool_id": tool_id, "success": True, "result": result},
                    source="agent_loop",
                    session_id=session.id,
                )
            )
        except Exception as e:
            tc.error = str(e)
            await self.events.publish(
                Event(
                    type="agent.toolResult",
                    payload={"tool_id": tool_id, "success": False, "error": str(e)},
                    source="agent_loop",
                    session_id=session.id,
                )
            )

        await self.hooks.execute(
            HookPoint.AFTER_TOOL_RESULT,
            tc,
            {"session": session},
            trace_span=tool_span,
        )

        if self.obs:
            self.obs.end_span(tool_span)
