"""
ReAct Engine — 纯 LLM 双层循环核心。

职责：
- 为单次 turn 组装 LLM 上下文
- 流式调用 LLM（带重试、上下文溢出截断尝试）
- 解析 OpenAI 格式的 tool call buffer
- 提供单轮直答和兜底收尾的裸调用

边界：
- 不持有状态机
- 不调用 Hook / EventBus / Observability
- 不执行工具、不做 ban 检查、不做降级决策
- 所有边界处理由调用方（AgentLoopSession）负责
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterable, Optional

from learning_agent.ai import (
    ChatChunk,
    ChatMessage,
    ChatParams,
    AuthError,
    ContextLengthError,
    Event,
    InvalidRequestError,
    MessageRole,
    ResilienceConfig,
    RetryableError,
    SessionEntry,
    ToolCall,
)
from learning_agent.ai.base_provider import BaseProvider
from learning_agent.agent.runtime_ports import SessionStore
from learning_agent.learning_agent.compaction import CompactionPlan, build_micro_compacted_history
from learning_agent.learning_agent.mode_service import TurnExecutionProfile

logger = logging.getLogger(__name__)


class ReActEngine:
    """
    负责 ReACT 循环中与 LLM 交互的纯逻辑。

    注入依赖：
    - provider: LLM Provider
    - session_store: 读写 session 消息历史
    - max_react_turns: ReACT 最大 turn 数（仅用于校验，循环本身由外层持有）
    - resilience_config: 重试策略参数
    """

    def __init__(
        self,
        provider: BaseProvider,
        session_store: SessionStore,
        max_react_turns: int = 10,
        resilience_config: Optional[ResilienceConfig] = None,
        events: Any = None,
    ):
        self.provider = provider
        self.session_store = session_store
        self.max_react_turns = max_react_turns
        self._resilience_config = resilience_config or ResilienceConfig()
        self._events = events

    # ── 上下文构建 ──

    def build_context(
        self,
        session_id: str,
        profile: TurnExecutionProfile,
        compaction_plan: CompactionPlan | None = None,
    ) -> list[ChatMessage]:
        """为当前 turn 构建 LLM 上下文（符合 OpenAI API 协议）。"""
        view = self.session_store.build_llm_input_view(
            session_id,
            profile,
            compaction_plan=compaction_plan,
        )
        return view.messages

    def _compress_tool_error_history(
        self,
        entries: list[SessionEntry],
    ) -> list[SessionEntry]:
        """
        对连续同一工具的 validation error / banned 进行压缩，最多保留最近 N 组。
        """
        max_groups = self._resilience_config.tool_max_validation_history_groups
        if max_groups <= 0:
            return entries

        tool_group_counts: dict[str, int] = {}
        skip_indices: set[int] = set()

        i = 0
        while i < len(entries):
            entry = entries[i]
            if entry.role == MessageRole.ASSISTANT and entry.tool_calls:
                if i + 1 < len(entries) and entries[i + 1].role == MessageRole.TOOL:
                    next_entry = entries[i + 1]
                    is_error = next_entry.metadata.get("is_error", False)
                    tool_id = next_entry.metadata.get("tool_id", "")
                    result_text = next_entry.content
                    if is_error and result_text and (
                        result_text.startswith("[Tool Input Validation Failed]")
                        or result_text.startswith("[Tool Unavailable]")
                    ):
                        tool_group_counts[tool_id] = tool_group_counts.get(tool_id, 0) + 1
                        if tool_group_counts[tool_id] > max_groups:
                            skip_indices.add(i)
                            skip_indices.add(i + 1)
                        i += 1
            i += 1

        return [e for idx, e in enumerate(entries) if idx not in skip_indices]

    def build_single_pass_messages(
        self,
        session_id: str,
        profile: TurnExecutionProfile,
        compaction_plan: CompactionPlan | None = None,
    ) -> list[ChatMessage]:
        """构建单轮直答型 turn 的上下文消息。"""
        return self.build_context(session_id, profile, compaction_plan=compaction_plan)

    def _build_summary_chat_message(self, summary_block: str) -> ChatMessage:
        return ChatMessage(role=MessageRole.SYSTEM, content=summary_block)

    def _history_to_chat_messages(self, entries: list[SessionEntry]) -> list[ChatMessage]:
        messages: list[ChatMessage] = []
        for entry in entries:
            if entry.role == MessageRole.ASSISTANT and not entry.content.strip() and not entry.tool_calls:
                continue
            if entry.role == MessageRole.TOOL:
                tool_call_id = entry.metadata.get("tool_call_id", "")
                messages.append(
                    ChatMessage(
                        role=MessageRole.TOOL,
                        content=entry.content,
                        tool_call_id=tool_call_id,
                    )
                )
            elif entry.tool_calls:
                messages.append(
                    ChatMessage(
                        role=entry.role,
                        content=entry.content,
                        tool_calls=[
                            {
                                "id": tc.call_id or "",
                                "type": "function",
                                "function": {
                                    "name": tc.tool_id,
                                    "arguments": json.dumps(tc.arguments, ensure_ascii=False)
                                    if tc.arguments
                                    else "{}",
                                },
                            }
                            for tc in entry.tool_calls
                        ],
                        reasoning_content=entry.metadata.get("reasoning_content") or "",
                    )
                )
            else:
                messages.append(ChatMessage(role=entry.role, content=entry.content))
        return messages

    # ── LLM 流式调用（带重试）──

    async def stream_chat_with_retry(
        self,
        session_id: str,
        params: ChatParams,
    ) -> AsyncIterable[ChatChunk]:
        """
        带 Turn 级重试的 LLM 流式调用。
        ContextLengthError 时会尝试 emergency truncation 然后重试。
        AuthError / InvalidRequestError 时 yield 错误 chunk 并 return。
        其他异常直接抛出，由外层处理。
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
                if (
                    self._resilience_config.auto_compress_on_context_overflow
                    and attempt < max_attempts
                ):
                    recovered = await self.attempt_emergency_truncation(session_id)
                    if recovered:
                        continue

                logger.warning(
                    "[ReActEngine] Context length exceeded without recoverable truncation (session=%s): %s",
                    session_id,
                    e,
                )
                yield ChatChunk(
                    content=(
                        "\n[Context limit reached. Switching to chat-only mode for this turn. "
                        "Please narrow the file range or search with grep first.]\n"
                    )
                )
                return
            except RetryableError:
                if attempt < max_attempts:
                    delay = min(backoff_base ** attempt, max_delay)
                    await asyncio.sleep(delay)
                    continue
                raise
            except (AuthError, InvalidRequestError) as e:
                yield ChatChunk(content=f"\n[Error] {e}\n")
                return
            except Exception:
                # 未知异常不归入重试，向上传播
                raise

    # ── 紧急截断 ──

    async def attempt_emergency_truncation(
        self,
        session_id: str,
        max_chars: int = 1000,
    ) -> bool:
        """
        截断历史中最长的 TOOL 消息，作为上下文溢出的最后兜底。
        通过 SessionStore.patch_entry() 修改，确保变更进入事件驱动持久化。
        """
        longest_tool_entry = self.session_store.find_truncatable_tool_entry(
            session_id,
            min_chars=max_chars,
        )
        if longest_tool_entry is None:
            return False

        longest_tool_length = len(longest_tool_entry.content or "")

        suffix = "\n\n[Content truncated due to context limit]"
        keep_length = max(0, max_chars - len(suffix))
        new_content = longest_tool_entry.content[:keep_length].rstrip()
        if new_content:
            new_content = f"{new_content}{suffix}"
        else:
            new_content = suffix.strip()

        entry_id = longest_tool_entry.id

        # 通过 SessionStore 统一修改，确保事件发射和持久化
        self.session_store.patch_entry(session_id, entry_id, "content", new_content)
        self.session_store.patch_entry(session_id, entry_id, "metadata.emergency_truncated", True)
        self.session_store.patch_entry(session_id, entry_id, "metadata.original_length", longest_tool_length)
        self.session_store.patch_entry(session_id, entry_id, "metadata.truncated_length", len(new_content))

        for idx, tool_result in enumerate(longest_tool_entry.tool_results):
            if isinstance(tool_result.get("result"), str):
                self.session_store.patch_entry(
                    session_id, entry_id, f"tool_results.{idx}.result", new_content
                )

        logger.warning(
            "[ReActEngine] Emergency truncated TOOL message from %s to %s chars (session=%s)",
            longest_tool_length,
            len(new_content),
            session_id,
        )
        if self._events:
            await self._events.publish(
                Event(
                    type="agent.contextEmergencyTruncation",
                    payload={
                        "session_id": session_id,
                        "original_length": longest_tool_length,
                        "truncated_length": len(new_content),
                    },
                    source="agent_loop",
                    session_id=session_id,
                )
            )
        return True

    # ── Tool Call 解析 ──

    @staticmethod
    def parse_tool_calls(tool_call_buffers: dict[int, dict[str, Any]]) -> list[ToolCall]:
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
            import uuid
            result.append(ToolCall(
                tool_id=fn.get("name", "unknown"),
                call_id=tc.get("id") or f"call-{uuid.uuid4().hex[:8]}",
                arguments=args,
            ))
        return result

    @staticmethod
    def is_retryable_tool_error(error: Exception) -> bool:
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
