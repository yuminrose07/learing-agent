from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.core.hook_system import HookSystem
from learning_agent.models import (
    AfterResponseInput,
    AfterResponseResult,
    AfterToolExecuteInput,
    AfterToolExecuteResult,
    BeforeAgentRunInput,
    BeforeAgentRunResult,
    BeforeToolExecuteInput,
    BeforeToolExecuteResult,
    HookAuditRecord,
    HookContext,
    HookDecision,
    HookWarning,
    OnStreamChunkInput,
    OnStreamChunkResult,
)


def make_context() -> HookContext:
    return HookContext(session_id="sess-test", trace_id="trace-test", turn_id=1)


class TestHookSystem:
    @pytest.mark.asyncio
    async def test_handlers_run_in_priority_order(self):
        hooks = HookSystem()
        order: list[str] = []

        async def low(_hook_input):
            order.append("low")
            return BeforeAgentRunResult()

        async def high(_hook_input):
            order.append("high")
            return BeforeAgentRunResult()

        hooks.register("before_agent_run", low, priority=10)
        hooks.register("before_agent_run", high, priority=100)

        await hooks.run_before_agent_run(
            BeforeAgentRunInput(
                user_input="hello",
                context=make_context(),
            )
        )

        assert order == ["high", "low"]

    @pytest.mark.asyncio
    async def test_before_tool_execute_merges_decision_and_patches(self):
        hooks = HookSystem()

        async def continue_hook(_hook_input):
            return BeforeToolExecuteResult(
                patched_arguments={"a": 1},
                warnings=[HookWarning(code="w1", message="warn1")],
            )

        async def ask_hook(_hook_input):
            return BeforeToolExecuteResult(
                decision=HookDecision.ASK,
                ask_message="need approval",
                patched_arguments={"b": 2},
            )

        async def deny_hook(_hook_input):
            return BeforeToolExecuteResult(
                decision=HookDecision.DENY,
                deny_reason="policy denied",
                patched_arguments={"c": 3},
                audit_records=[HookAuditRecord(category="policy", action="deny")],
            )

        hooks.register("before_tool_execute", continue_hook, priority=10)
        hooks.register("before_tool_execute", ask_hook, priority=20)
        hooks.register("before_tool_execute", deny_hook, priority=30)

        result = await hooks.run_before_tool_execute(
            BeforeToolExecuteInput(
                tool_call_id="call-1",
                tool_name="demo",
                arguments={},
                context=make_context(),
            )
        )

        assert result.decision == HookDecision.DENY
        assert result.deny_reason == "policy denied"
        assert result.patched_arguments == {"c": 3, "b": 2, "a": 1}
        assert len(result.warnings) == 1
        assert len(result.audit_records) == 1

    @pytest.mark.asyncio
    async def test_after_tool_execute_uses_last_override_and_merges_metadata(self):
        hooks = HookSystem()

        async def first(_hook_input):
            return AfterToolExecuteResult(
                display_result_override="first",
                extra_metadata={"a": 1},
            )

        async def second(_hook_input):
            return AfterToolExecuteResult(
                display_result_override="second",
                extra_metadata={"b": 2, "a": 3},
            )

        hooks.register("after_tool_execute", first, priority=20)
        hooks.register("after_tool_execute", second, priority=10)

        result = await hooks.run_after_tool_execute(
            AfterToolExecuteInput(
                tool_call_id="call-1",
                tool_name="demo",
                arguments={},
                success=True,
                result="ok",
                context=make_context(),
            )
        )

        assert result.display_result_override == "second"
        assert result.extra_metadata == {"a": 3, "b": 2}

    @pytest.mark.asyncio
    async def test_stream_chunk_override_is_chainable(self):
        hooks = HookSystem()

        async def upper(hook_input):
            return OnStreamChunkResult(content_override=hook_input.content.upper())

        async def suffix(hook_input):
            return OnStreamChunkResult(content_override=hook_input.content + "!")

        hooks.register("on_stream_chunk", upper, priority=20)
        hooks.register("on_stream_chunk", suffix, priority=10)

        result = await hooks.run_on_stream_chunk(
            OnStreamChunkInput(
                chunk_index=0,
                content="hello",
                context=make_context(),
            )
        )

        assert result.content_override == "HELLO!"

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_break_following_handlers(self):
        hooks = HookSystem()

        async def broken(_hook_input):
            raise RuntimeError("boom")

        async def healthy(_hook_input):
            return AfterResponseResult(
                response_override="ok",
                followup_signals=["sig-a"],
            )

        hooks.register("after_response", broken, priority=20)
        hooks.register("after_response", healthy, priority=10)

        result = await hooks.run_after_response(
            AfterResponseInput(
                response_text="hello",
                context=make_context(),
            )
        )

        assert result.response_override == "ok"
        assert result.followup_signals == ["sig-a"]

    @pytest.mark.asyncio
    async def test_after_response_deduplicates_followup_signals(self):
        hooks = HookSystem()

        async def first(_hook_input):
            return AfterResponseResult(followup_signals=["a", "b"])

        async def second(_hook_input):
            return AfterResponseResult(followup_signals=["b", "c"])

        hooks.register("after_response", first, priority=20)
        hooks.register("after_response", second, priority=10)

        result = await hooks.run_after_response(
            AfterResponseInput(
                response_text="hello",
                context=make_context(),
            )
        )

        assert result.followup_signals == ["a", "b", "c"]
