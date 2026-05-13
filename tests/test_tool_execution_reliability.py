"""
工具执行可靠性修复 —— 测试套件

覆盖场景：
A. 模拟工具 handler 抛出 ConnectionError，确认重试 1 次后成功
B. 模拟工具连续 validation 失败，确认不触发 ban
C. 模拟工具连续 execution_error，确认触发 ban
D. 降级到 chat-only 后，连续 3 个无工具 turn，确认自动恢复
E. 模拟工具 handler 死锁，确认超时后抛出 TimeoutError
F. 模拟重试耗尽后 execution_error，确认错误回流给 LLM
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, AsyncIterable, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.agent.agent_loop import AgentLoop
from learning_agent.agent.event_bus import EventBus
from learning_agent.agent.hook_system import HookSystem
from learning_agent.agent.tool_failure_tracker import ToolFailureTracker
from learning_agent.agent.tool_registry import ToolRegistry
from learning_agent.memory.memory_manager import MemoryManager
from learning_agent.ai import (
    AfterToolExecuteResult,
    BeforeAgentRunResult,
    BeforeToolExecuteResult,
    ChatChunk,
    ChatMessage,
    ChatParams,
    HookDecision,
    HookName,
    LearningSession,
    MessageRole,
    ResilienceConfig,
    ToolCall,
    ToolDefinition,
)
from learning_agent.ai.base_provider import BaseProvider
from learning_agent.agent.agent_loop import AgentLoopSession
from learning_agent.learning_agent.session_manager import SessionManager


def outcome_result(outcome: dict[str, Any]) -> Any:
    return outcome["result"]


def outcome_is_error(outcome: dict[str, Any]) -> bool:
    return outcome["is_error"]


# ───────────────────────────────────────────────────────────────
# Fixtures
# ───────────────────────────────────────────────────────────────

@pytest.fixture
def resilience_config() -> ResilienceConfig:
    return ResilienceConfig(
        max_tool_retries=1,
        tool_retry_base_delay=0.01,
        tool_retry_max_delay=0.05,
        tool_default_timeout=1,
        chat_only_recovery_turns=3,
        react_turns_before_chat_fallback=2,
        tool_failure_window_turns=5,
        tool_failure_threshold=3,
    )


@pytest.fixture
def event_bus() -> EventBus:
    return EventBus()


@pytest.fixture
def session_manager() -> SessionManager:
    return SessionManager()


@pytest.fixture
def memory_manager() -> MemoryManager:
    return MemoryManager()


@pytest.fixture
def hook_system() -> HookSystem:
    return HookSystem()


@pytest.fixture
def tool_registry() -> ToolRegistry:
    return ToolRegistry()


class FakeProvider(BaseProvider):
    """模拟 Provider，支持控制是否返回 tool_calls。"""

    _default_model = "gpt-4o"

    def __init__(self, chunks: Optional[list[ChatChunk]] = None):
        self._chunks = chunks or []

    @property
    def default_model(self) -> str:
        return self._default_model

    async def stream_chat(self, params: ChatParams) -> AsyncIterable[ChatChunk]:
        for chunk in self._chunks:
            yield chunk

    async def chat(self, params: ChatParams) -> ChatChunk:
        return ChatChunk()

    def supports_tool_calling(self) -> bool:
        return True

    def supports_vision(self) -> bool:
        return False

    def get_max_context_length(self) -> int:
        return 128000


def make_agent_loop(
    provider: BaseProvider,
    resilience_config: ResilienceConfig,
    event_bus: EventBus,
    session_manager: SessionManager,
    memory_manager: MemoryManager,
    hook_system: HookSystem,
    tool_registry: ToolRegistry,
) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        memory_manager=memory_manager,
        session_manager=session_manager,
        hook_system=hook_system,
        event_bus=event_bus,
        tool_registry=tool_registry,
        observability=None,
        max_react_turns=5,
        resilience_config=resilience_config,
    )


# ───────────────────────────────────────────────────────────────
# ToolFailureTracker Tests
# ───────────────────────────────────────────────────────────────

class TestToolFailureTracker:
    def test_record_failure_with_reason(self):
        tracker = ToolFailureTracker(window_turns=5, threshold=3)
        tracker.record_failure("tool_a", turn_count=1, reason="execution_error")
        tracker.record_failure("tool_a", turn_count=2, reason="validation_failed")
        tracker.record_failure("tool_a", turn_count=3, reason="hook_abort")

        entries = tracker._counts["tool_a"]
        assert len(entries) == 3
        assert entries[0] == (1, "execution_error")
        assert entries[1] == (2, "validation_failed")
        assert entries[2] == (3, "hook_abort")

    def test_is_banned_only_counts_execution_error(self):
        tracker = ToolFailureTracker(window_turns=5, threshold=3)
        # 2 execution_error + 1 validation_failed = 应该不 ban（threshold=3）
        tracker.record_failure("tool_a", turn_count=1, reason="execution_error")
        tracker.record_failure("tool_a", turn_count=2, reason="execution_error")
        tracker.record_failure("tool_a", turn_count=3, reason="validation_failed")
        assert not tracker.is_banned("tool_a", current_turn=5)

        # 再加 1 execution_error = 3，应该 ban
        tracker.record_failure("tool_a", turn_count=4, reason="execution_error")
        assert tracker.is_banned("tool_a", current_turn=5)

    def test_is_banned_respects_window(self):
        tracker = ToolFailureTracker(window_turns=3, threshold=2)
        tracker.record_failure("tool_a", turn_count=1, reason="execution_error")
        tracker.record_failure("tool_a", turn_count=2, reason="execution_error")
        # current_turn=4: 4-1=3 <= window, 4-2=2 <= window → 2 failures → banned
        assert tracker.is_banned("tool_a", current_turn=4)
        # current_turn=5: 5-1=4 > window, 5-2=3 <= window → 1 failure → not banned
        assert not tracker.is_banned("tool_a", current_turn=5)

    def test_record_success_clears_all(self):
        tracker = ToolFailureTracker(window_turns=5, threshold=3)
        tracker.record_failure("tool_a", turn_count=1, reason="execution_error")
        tracker.record_failure("tool_a", turn_count=2, reason="validation_failed")
        tracker.record_success("tool_a")
        assert "tool_a" not in tracker._counts
        assert not tracker.is_banned("tool_a", current_turn=5)

    def test_get_all_failures_in_window(self):
        tracker = ToolFailureTracker(window_turns=5, threshold=3)
        tracker.record_failure("tool_a", turn_count=1, reason="execution_error")
        tracker.record_failure("tool_a", turn_count=2, reason="validation_failed")
        tracker.record_failure("tool_a", turn_count=3, reason="execution_error")
        # turn_count=8 with current_turn=6: 6-8=-2 <= 5, so it IS inside window.
        # To test outside-window, use current_turn=15 where 15-8=7 > 5.
        tracker.record_failure("tool_a", turn_count=8, reason="hook_abort")

        counts = tracker.get_all_failures_in_window("tool_a", current_turn=6)
        assert counts == {"execution_error": 2, "validation_failed": 1, "hook_abort": 1}

        counts = tracker.get_all_failures_in_window("tool_a", current_turn=15)
        assert counts == {}

    def test_get_failures_in_window_only_execution_error(self):
        tracker = ToolFailureTracker(window_turns=5, threshold=3)
        tracker.record_failure("tool_a", turn_count=1, reason="execution_error")
        tracker.record_failure("tool_a", turn_count=2, reason="validation_failed")
        tracker.record_failure("tool_a", turn_count=3, reason="execution_error")

        assert tracker.get_failures_in_window("tool_a", current_turn=5) == 2


# ───────────────────────────────────────────────────────────────
# ToolRegistry Tests
# ───────────────────────────────────────────────────────────────

class TestToolRegistry:
    @pytest.mark.asyncio
    async def test_execute_async_handler_success(self):
        registry = ToolRegistry()
        async def handler(x: int) -> int:
            return x * 2
        registry.register(
            ToolDefinition(id="double", name="double", description=""),
            handler,
        )
        tc = ToolCall(tool_id="double", arguments={"x": 5})
        result = await registry.execute(tc, timeout=1.0)
        assert result == 10
        assert tc.duration_ms is not None
        assert tc.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_execute_sync_handler_wrapped(self):
        registry = ToolRegistry()
        def handler(x: int) -> int:
            return x * 3
        registry.register(
            ToolDefinition(id="triple", name="triple", description=""),
            handler,
        )
        tc = ToolCall(tool_id="triple", arguments={"x": 4})
        result = await registry.execute(tc, timeout=1.0)
        assert result == 12

    @pytest.mark.asyncio
    async def test_execute_timeout_raises_timeout_error(self):
        registry = ToolRegistry()
        async def slow_handler() -> str:
            await asyncio.sleep(10)
            return "done"
        registry.register(
            ToolDefinition(id="slow", name="slow", description=""),
            slow_handler,
        )
        tc = ToolCall(tool_id="slow", arguments={})
        with pytest.raises(TimeoutError) as exc_info:
            await registry.execute(tc, timeout=0.1)
        assert "slow" in str(exc_info.value)
        assert "0.1s" in str(exc_info.value)
        assert tc.duration_ms is not None

    @pytest.mark.asyncio
    async def test_execute_sync_handler_timeout(self):
        registry = ToolRegistry()
        def slow_handler() -> str:
            import time
            time.sleep(10)
            return "done"
        registry.register(
            ToolDefinition(id="slow_sync", name="slow_sync", description=""),
            slow_handler,
        )
        tc = ToolCall(tool_id="slow_sync", arguments={})
        with pytest.raises(TimeoutError):
            await registry.execute(tc, timeout=0.1)


# ───────────────────────────────────────────────────────────────
# AgentLoop._is_retryable_tool_error Tests
# ───────────────────────────────────────────────────────────────

class TestAgentLoopRetryableError:
    def test_retryable_exceptions(self):
        loop = make_agent_loop(
            FakeProvider(),
            ResilienceConfig(),
            EventBus(),
            SessionManager(),
            MemoryManager(),
            HookSystem(),
            ToolRegistry(),
        )
        assert loop._is_retryable_tool_error(TimeoutError("timeout"))
        assert loop._is_retryable_tool_error(asyncio.TimeoutError())
        assert loop._is_retryable_tool_error(ConnectionError("conn failed"))
        assert loop._is_retryable_tool_error(ConnectionRefusedError())
        assert loop._is_retryable_tool_error(ConnectionResetError())
        assert loop._is_retryable_tool_error(BrokenPipeError())

    def test_retryable_keywords(self):
        loop = make_agent_loop(
            FakeProvider(),
            ResilienceConfig(),
            EventBus(),
            SessionManager(),
            MemoryManager(),
            HookSystem(),
            ToolRegistry(),
        )
        assert loop._is_retryable_tool_error(RuntimeError("HTTP 503 Service Unavailable"))
        assert loop._is_retryable_tool_error(RuntimeError("Rate limit exceeded"))
        assert loop._is_retryable_tool_error(RuntimeError("Connection reset by peer"))
        assert loop._is_retryable_tool_error(ValueError("too many requests"))

    def test_non_retryable_errors(self):
        loop = make_agent_loop(
            FakeProvider(),
            ResilienceConfig(),
            EventBus(),
            SessionManager(),
            MemoryManager(),
            HookSystem(),
            ToolRegistry(),
        )
        assert not loop._is_retryable_tool_error(ValueError("bad argument"))
        assert not loop._is_retryable_tool_error(TypeError("wrong type"))
        assert not loop._is_retryable_tool_error(RuntimeError("logic error"))


# ───────────────────────────────────────────────────────────────
# AgentLoop._execute_tool_calls Tests
# ───────────────────────────────────────────────────────────────

class TestAgentLoopExecuteToolCalls:
    @pytest.fixture
    def base_setup(self, resilience_config, event_bus, session_manager, memory_manager, hook_system, tool_registry):
        session = session_manager.create_session()
        provider = FakeProvider()
        loop = make_agent_loop(
            provider, resilience_config, event_bus, session_manager,
            memory_manager, hook_system, tool_registry,
        )
        runtime = loop._get_or_create_runtime(session.id)
        return loop, runtime, session, tool_registry, event_bus

    @pytest.mark.asyncio
    async def test_retry_success_on_transient_error(self, base_setup):
        """场景 A：transient 错误后重试成功。"""
        loop, runtime, session, registry, bus = base_setup
        call_count = 0
        async def flaky_handler():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("transient")
            return "ok"
        registry.register(
            ToolDefinition(id="flaky", name="flaky", description=""),
            flaky_handler,
        )
        tc = ToolCall(tool_id="flaky", arguments={})
        results = await runtime._execute_tool_calls(session, [tc], turn_count=1, parent_span=None)
        assert len(results) == 1
        assert outcome_result(results[0]) == "ok"
        assert not outcome_is_error(results[0])
        assert call_count == 2  # 第一次失败 + 重试成功
        # 验证 retry 事件被发布
        history = bus.get_history("agent.toolRetry")
        assert len(history) == 1
        assert history[0].payload["tool_id"] == "flaky"

    @pytest.mark.asyncio
    async def test_retry_exhausted_records_failure(self, base_setup):
        """场景 F：重试耗尽后记录 execution_error。"""
        loop, runtime, session, registry, bus = base_setup
        async def always_fail():
            raise ConnectionError("persistent")
        registry.register(
            ToolDefinition(id="bad", name="bad", description=""),
            always_fail,
        )
        tc = ToolCall(tool_id="bad", arguments={})
        results = await runtime._execute_tool_calls(session, [tc], turn_count=1, parent_span=None)
        assert len(results) == 1
        assert outcome_is_error(results[0])
        assert "persistent" in outcome_result(results[0])
        # 验证被 ban 计数
        assert runtime._failure_tracker.get_failures_in_window("bad", current_turn=1) == 1
        # 验证 retry 事件被发布 1 次
        history = bus.get_history("agent.toolRetry")
        assert len(history) == 1

    @pytest.mark.asyncio
    async def test_no_retry_for_non_retryable_error(self, base_setup):
        """非 transient 错误不重试。"""
        loop, runtime, session, registry, bus = base_setup
        call_count = 0
        async def logic_error():
            nonlocal call_count
            call_count += 1
            raise ValueError("bad logic")
        registry.register(
            ToolDefinition(id="logic", name="logic", description=""),
            logic_error,
        )
        tc = ToolCall(tool_id="logic", arguments={})
        results = await runtime._execute_tool_calls(session, [tc], turn_count=1, parent_span=None)
        assert len(results) == 1
        assert outcome_is_error(results[0])
        assert call_count == 1  # 没有重试
        # 验证没有 retry 事件
        history = bus.get_history("agent.toolRetry")
        assert len(history) == 0

    @pytest.mark.asyncio
    async def test_validation_failed_no_ban(self, base_setup):
        """场景 B：validation 失败不计入 ban。"""
        loop, runtime, session, registry, bus = base_setup
        # 注册一个工具，带有 Pydantic input_model 会触发 validation
        from pydantic import BaseModel
        class Input(BaseModel):
            required_field: str
        registry.register(
            ToolDefinition(id="validated", name="validated", description="", input_model=Input),
            lambda **kwargs: "ok",
        )
        tc = ToolCall(tool_id="validated", arguments={"wrong_field": "x"})
        results = await runtime._execute_tool_calls(session, [tc], turn_count=1, parent_span=None)
        assert len(results) == 1
        assert outcome_is_error(results[0])
        # 验证不计入 ban
        assert runtime._failure_tracker.get_failures_in_window("validated", current_turn=1) == 0
        # 但记录了 failure with reason="validation_failed"
        all_fails = runtime._failure_tracker.get_all_failures_in_window("validated", current_turn=1)
        assert all_fails.get("validation_failed", 0) == 1

    @pytest.mark.asyncio
    async def test_hook_deny_no_ban(self, base_setup):
        """场景 C'：hook deny 不计入 ban。"""
        loop, runtime, session, registry, bus = base_setup
        registry.register(
            ToolDefinition(id="guarded", name="guarded", description=""),
            lambda **kwargs: "ok",
        )
        async def deny_hook(_hook_input):
            return BeforeToolExecuteResult(
                decision=HookDecision.DENY,
                deny_reason="policy denied",
            )
        loop.hooks.register(HookName.BEFORE_TOOL_EXECUTE, deny_hook)
        tc = ToolCall(tool_id="guarded", arguments={})
        results = await runtime._execute_tool_calls(session, [tc], turn_count=1, parent_span=None)
        assert len(results) == 1
        assert outcome_is_error(results[0])
        # 验证不计入 ban
        assert runtime._failure_tracker.get_failures_in_window("guarded", current_turn=1) == 0
        all_fails = runtime._failure_tracker.get_all_failures_in_window("guarded", current_turn=1)
        assert all_fails.get("hook_abort", 0) == 1

    @pytest.mark.asyncio
    async def test_execution_error_triggers_ban(self, base_setup):
        """场景 C：连续 execution_error 触发 ban。"""
        loop, runtime, session, registry, bus = base_setup
        async def always_fail():
            raise ConnectionError("fail")
        registry.register(
            ToolDefinition(id="unstable", name="unstable", description=""),
            always_fail,
        )
        # 连续 3 次 execution_error（threshold=3）
        for turn in range(1, 4):
            tc = ToolCall(tool_id="unstable", arguments={})
            results = await runtime._execute_tool_calls(session, [tc], turn_count=turn, parent_span=None)
            assert outcome_is_error(results[0])
        # 第 4 次应该被 banned
        tc = ToolCall(tool_id="unstable", arguments={})
        results = await runtime._execute_tool_calls(session, [tc], turn_count=4, parent_span=None)
        assert outcome_is_error(results[0])
        assert "temporarily disabled" in outcome_result(results[0])
        # 验证 toolBanned 事件
        history = bus.get_history("agent.toolBanned")
        assert len(history) >= 1
        assert "failure_types" in history[-1].payload

    @pytest.mark.asyncio
    async def test_before_tool_execute_patches_arguments(self, base_setup):
        loop, runtime, session, registry, bus = base_setup
        captured: list[dict[str, Any]] = []

        async def handler(message: str) -> str:
            captured.append({"message": message})
            return message

        registry.register(
            ToolDefinition(
                id="echo",
                name="echo",
                description="",
                parameters={"type": "object", "properties": {"message": {"type": "string"}}},
            ),
            handler,
        )

        async def patch_hook(_hook_input):
            return BeforeToolExecuteResult(patched_arguments={"message": "patched"})

        loop.hooks.register(HookName.BEFORE_TOOL_EXECUTE, patch_hook)
        results = await runtime._execute_tool_calls(
            session,
            [ToolCall(tool_id="echo", arguments={"message": "raw"})],
            turn_count=1,
            parent_span=None,
        )

        assert captured == [{"message": "patched"}]
        assert outcome_result(results[0]) == "patched"

    @pytest.mark.asyncio
    async def test_after_tool_execute_can_override_display_result(self, base_setup):
        loop, runtime, session, registry, bus = base_setup

        async def handler() -> dict[str, Any]:
            return {"secret": "value"}

        registry.register(
            ToolDefinition(id="masked", name="masked", description=""),
            handler,
        )

        async def override_hook(_hook_input):
            return AfterToolExecuteResult(
                display_result_override="[Masked]",
                extra_metadata={"masked": True},
            )

        loop.hooks.register(HookName.AFTER_TOOL_EXECUTE, override_hook)
        results = await runtime._execute_tool_calls(
            session,
            [ToolCall(tool_id="masked", arguments={})],
            turn_count=1,
            parent_span=None,
        )

        assert outcome_result(results[0]) == {"secret": "value"}
        assert results[0]["display_result_override"] == "[Masked]"
        assert results[0]["extra_metadata"]["masked"] is True


# ───────────────────────────────────────────────────────────────
# AgentLoop.run() Chat-Only Recovery Test
# ───────────────────────────────────────────────────────────────

class TestAgentLoopChatOnlyRecovery:
    @pytest.fixture
    def recovery_setup(self, resilience_config, event_bus, session_manager, memory_manager, hook_system, tool_registry):
        session = session_manager.create_session()
        provider = FakeProvider()
        loop = make_agent_loop(
            provider, resilience_config, event_bus, session_manager,
            memory_manager, hook_system, tool_registry,
        )
        return loop, session, provider, event_bus, session_manager

    @pytest.mark.asyncio
    async def test_chat_only_auto_recovery(self, recovery_setup):
        """场景 D：降级到 chat-only 后，连续 3 个无工具 turn 自动恢复。"""
        loop, session, provider, bus, sm = recovery_setup

        # 直接模拟降级状态（避免复杂的 ReACT 多 turn 交互）
        runtime = loop._get_or_create_runtime(session.id)
        runtime._chat_only_mode = True
        runtime._chat_only_success_turns = 0

        # 运行 3 个纯文本 turn（无 tool calls）——使用同一个 session 以累积 recovery 计数
        for i in range(3):
            provider._chunks = [ChatChunk(content=f"reply {i}")]
            async for c in loop.run(session, f"chat {i}"):
                pass

        # 经过 3 个无工具 turn，应该自动恢复
        assert runtime._chat_only_mode is False
        assert runtime._chat_only_success_turns == 0
        # 验证恢复事件
        history = bus.get_history("agent.chatOnlyRecovered")
        assert len(history) >= 1
        assert history[-1].payload["recovery_turns"] == 3

    @pytest.mark.asyncio
    async def test_chat_only_no_recovery_when_tool_calls_present(self, recovery_setup):
        """Chat-only 模式下如果 LLM 仍然请求工具，不算恢复 turn。"""
        loop, session, provider, bus, sm = recovery_setup

        runtime = loop._get_or_create_runtime(session.id)
        runtime._chat_only_mode = True
        runtime._chat_only_success_turns = 0

        # 注册一个工具（虽然 chat-only 模式下不会真正执行，但 LLM 请求了 tool call）
        async def dummy_tool():
            return "ok"
        loop.tools.register(
            ToolDefinition(id="dummy", name="dummy", description=""),
            dummy_tool,
        )

        # LLM 返回 tool call（在 chat-only 模式下工具不会被传给 LLM，但如果 Provider 仍然返回...）
        # 实际上 chat-only 模式下 params.tools=None，FakeProvider 不理会 params，所以仍然会返回 tool_call
        provider._chunks = [
            ChatChunk(
                tool_call={"id": "call-1", "type": "function", "function": {"name": "dummy", "arguments": "{}"}},
                tool_call_index=0,
            ),
        ]
        async for c in loop.run(session, "chat with tool"):
            pass

        # chat-only 模式下，如果 has_tool_calls=True，不应该增加恢复计数
        assert runtime._chat_only_mode is True
        assert runtime._chat_only_success_turns == 0


class TestAgentLoopHookIntegration:
    @pytest.fixture
    def run_setup(self, resilience_config, event_bus, session_manager, memory_manager, hook_system, tool_registry):
        session = session_manager.create_session()
        provider = FakeProvider()
        loop = make_agent_loop(
            provider, resilience_config, event_bus, session_manager,
            memory_manager, hook_system, tool_registry,
        )
        return loop, session, provider, event_bus, tool_registry

    @pytest.mark.asyncio
    async def test_before_agent_run_deny(self, run_setup):
        loop, session, provider, bus, registry = run_setup

        async def deny_hook(_hook_input):
            return BeforeAgentRunResult(
                decision=HookDecision.DENY,
                deny_reason="blocked before run",
            )

        loop.hooks.register(HookName.BEFORE_AGENT_RUN, deny_hook)
        chunks = [chunk async for chunk in loop.run(session, "hello")]
        assert len(chunks) == 1
        assert chunks[0].content == "blocked before run"
        history = session.entries
        assert history[-1].content == "blocked before run"

    @pytest.mark.asyncio
    async def test_before_agent_run_ask(self, run_setup):
        loop, session, provider, bus, registry = run_setup

        async def ask_hook(_hook_input):
            return BeforeAgentRunResult(
                decision=HookDecision.ASK,
                ask_message="need clarification",
            )

        loop.hooks.register(HookName.BEFORE_AGENT_RUN, ask_hook)
        chunks = [chunk async for chunk in loop.run(session, "hello")]
        assert len(chunks) == 1
        assert chunks[0].content == "need clarification"

    @pytest.mark.asyncio
    async def test_stream_chunk_and_after_response_hooks(self, run_setup):
        loop, session, provider, bus, registry = run_setup
        provider._chunks = [ChatChunk(content="hello"), ChatChunk(content=" world", finish_reason="stop")]

        async def stream_hook(hook_input):
            return OnStreamChunkResult(content_override=hook_input.content.upper())

        async def response_hook(_hook_input):
            return AfterResponseResult(extra_metadata={"tag": "done"})

        from learning_agent.ai import OnStreamChunkResult, AfterResponseResult

        loop.hooks.register(HookName.ON_STREAM_CHUNK, stream_hook)
        loop.hooks.register(HookName.AFTER_RESPONSE, response_hook)

        chunks = [chunk async for chunk in loop.run(session, "hi")]
        assert "".join(chunk.content for chunk in chunks) == "HELLO WORLD"
        assistant_entries = [e for e in session.entries if e.role == MessageRole.ASSISTANT]
        assert assistant_entries[-1].metadata["tag"] == "done"


# ───────────────────────────────────────────────────────────────
# AgentLoopSession 状态隔离与生命周期测试
# ───────────────────────────────────────────────────────────────

class TestAgentLoopSessionIsolation:
    @pytest.fixture
    def isolation_setup(self, resilience_config, event_bus, session_manager, memory_manager, hook_system, tool_registry):
        provider = FakeProvider()
        loop = make_agent_loop(
            provider, resilience_config, event_bus, session_manager,
            memory_manager, hook_system, tool_registry,
        )
        return loop, session_manager

    @pytest.mark.asyncio
    async def test_session_state_isolation(self, isolation_setup):
        """验证两个 session 的运行时状态互相隔离。"""
        loop, sm = isolation_setup
        session_a = sm.create_session()
        session_b = sm.create_session()

        # 让 session A 进入 chat-only mode
        runtime_a = loop._get_or_create_runtime(session_a.id)
        runtime_a._chat_only_mode = True

        # session B 的运行时不受影响
        runtime_b = loop._get_or_create_runtime(session_b.id)
        assert runtime_b._chat_only_mode is False
        assert runtime_b._chat_only_success_turns == 0

        # session A 的 failure tracker 独立
        runtime_a._failure_tracker.record_failure("tool_x", turn_count=1, reason="execution_error")
        assert runtime_a._failure_tracker.get_failures_in_window("tool_x", current_turn=1) == 1
        assert runtime_b._failure_tracker.get_failures_in_window("tool_x", current_turn=1) == 0

    @pytest.mark.asyncio
    async def test_concurrent_same_session_serializes(self, isolation_setup):
        """验证同 session 的并发调用被串行化。"""
        loop, sm = isolation_setup
        session = sm.create_session()

        # 使用一个可以控制执行时长的 provider
        provider = loop.provider
        provider._chunks = [ChatChunk(content="chunk")]

        results = []

        async def run1():
            async for chunk in loop.run(session, "msg1"):
                results.append(("run1", chunk.content))

        async def run2():
            async for chunk in loop.run(session, "msg2"):
                results.append(("run2", chunk.content))

        await asyncio.gather(run1(), run2())

        # 结果应该完全串行：run1 的所有 chunk 在前，run2 的所有 chunk 在后
        # 或反之，但绝不交错
        run1_indices = [i for i, (name, _) in enumerate(results) if name == "run1"]
        run2_indices = [i for i, (name, _) in enumerate(results) if name == "run2"]
        assert max(run1_indices) < min(run2_indices) or max(run2_indices) < min(run1_indices)

    @pytest.mark.asyncio
    async def test_concurrent_different_session_parallel(self, isolation_setup):
        """验证不同 session 的调用可并行。"""
        loop, sm = isolation_setup
        session_a = sm.create_session()
        session_b = sm.create_session()

        # 使用一个慢 provider 来验证并行
        class SlowProvider(BaseProvider):
            _default_model = "gpt-4o"
            def __init__(self, delay: float):
                self._delay = delay
            @property
            def default_model(self) -> str:
                return self._default_model
            async def stream_chat(self, params):
                await asyncio.sleep(self._delay)
                yield ChatChunk(content="done")
            async def chat(self, params):
                return ChatChunk(content="done")
            def supports_tool_calling(self):
                return True
            def supports_vision(self):
                return False
            def get_max_context_length(self):
                return 128000

        slow = SlowProvider(delay=0.3)
        loop.provider = slow

        start = asyncio.get_event_loop().time()
        await asyncio.gather(
            loop.run(session_a, "msgA").__anext__(),
            loop.run(session_b, "msgB").__anext__(),
        )
        elapsed = asyncio.get_event_loop().time() - start

        # 如果是串行的，需要 ~0.6s；如果是并行的，只需要 ~0.3s
        assert elapsed < 0.55, f"Expected parallel execution (<0.55s), got {elapsed}s"

    @pytest.mark.asyncio
    async def test_runtime_cleanup_on_delete(self, isolation_setup):
        """验证 session 删除时运行时被清理。"""
        loop, sm = isolation_setup
        session = sm.create_session()

        # 创建运行时
        runtime = loop._get_or_create_runtime(session.id)
        assert session.id in loop._session_runtimes
        assert session.id in loop._session_last_accessed

        # 删除 session
        sm.delete_session(session.id)

        # 运行时应该被清理
        assert session.id not in loop._session_runtimes
        assert session.id not in loop._session_last_accessed

    def test_clear_session_runtime(self, isolation_setup):
        """验证手动清理运行时。"""
        loop, sm = isolation_setup
        session = sm.create_session()

        loop._get_or_create_runtime(session.id)
        assert session.id in loop._session_runtimes

        loop.clear_session_runtime(session.id)
        assert session.id not in loop._session_runtimes
        assert session.id not in loop._session_last_accessed

    def test_clear_all_runtimes(self, isolation_setup):
        """验证清理所有运行时。"""
        loop, sm = isolation_setup
        session_a = sm.create_session()
        session_b = sm.create_session()

        loop._get_or_create_runtime(session_a.id)
        loop._get_or_create_runtime(session_b.id)

        loop.clear_all_runtimes()
        assert not loop._session_runtimes
        assert not loop._session_last_accessed

    def test_expired_runtime_cleanup(self, isolation_setup):
        """验证 TTL 过期清理。"""
        import time
        loop, sm = isolation_setup
        loop._session_runtime_ttl = 0.01  # 10ms TTL
        session = sm.create_session()

        loop._get_or_create_runtime(session.id)
        assert session.id in loop._session_runtimes

        # 等待过期
        time.sleep(0.02)

        # 获取运行时触发清理
        loop._cleanup_expired_runtimes()
        assert session.id not in loop._session_runtimes
