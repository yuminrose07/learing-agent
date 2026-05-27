"""
ToolExecutor 并发执行 — 测试套件

覆盖场景:
- 并行模式下,多个 tool_call 真正并发执行(总耗时 ≈ 最慢那一个)
- 一个 tool 的 pipeline 崩溃不影响其他 tool 的 outcome
- 并行模式下,outcome 列表顺序与输入 tool_calls 一致
- tool_parallel_execution=False 时回退到串行(总耗时 ≈ 累加)
- 单个 tool_call 时无论开关如何都正常执行
"""
from __future__ import annotations

import asyncio
import sys
import time
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.agent.event_bus import EventBus
from learning_agent.agent.hook_system import HookSystem
from learning_agent.agent.tool_executor import ToolExecutor
from learning_agent.agent.tool_failure_tracker import ToolFailureTracker
from learning_agent.ai import (
    HookContext,
    LearningSession,
    ResilienceConfig,
    ToolCall,
    ToolDefinition,
)


def _build_hook_context(session, *, turn_id, metadata):
    """ToolExecutor 期望的 build_hook_context callable。"""
    return HookContext(
        session_id=session.id,
        turn_id=turn_id,
        metadata=metadata,
    )


class _FakeToolService:
    """模拟 ToolExecutionService:按 tool_id 返回预设的 sleep + payload。"""

    def __init__(self, behaviors: dict[str, dict[str, Any]]):
        # behaviors[tool_id] = {"sleep": float, "result": Any, "raise": Exception | None}
        self._behaviors = behaviors

    async def execute_tool_call(
        self,
        tool_call: ToolCall,
        timeout: Optional[float] = None,
    ) -> Any:
        cfg = self._behaviors.get(tool_call.tool_id, {})
        await asyncio.sleep(cfg.get("sleep", 0))
        exc = cfg.get("raise")
        if exc is not None:
            raise exc
        return cfg.get("result", {"ok": True, "tool": tool_call.tool_id})

    def get_tool_definition(self, tool_id: str) -> Optional[ToolDefinition]:
        return ToolDefinition(
            id=tool_id,
            name=tool_id,
            description=f"fake {tool_id}",
            parameters={"type": "object", "properties": {}},
        )

    def list_tools(self) -> list[ToolDefinition]:
        return []


def _make_executor(
    behaviors: dict[str, dict[str, Any]],
    *,
    parallel: bool = True,
) -> ToolExecutor:
    resilience = ResilienceConfig(
        tool_validation_enabled=False,
        max_tool_retries=0,
        tool_parallel_execution=parallel,
    )
    return ToolExecutor(
        hooks=HookSystem(),
        validator=MagicMock(),
        failure_tracker=ToolFailureTracker(),
        events=EventBus(),
        tool_service=_FakeToolService(behaviors),
        session_store=MagicMock(),
        resilience_config=resilience,
        observability=None,
        event_writer=None,
        unresolved_failure_logger=None,
    )


def _make_tool_calls(*tool_ids: str) -> list[ToolCall]:
    return [ToolCall(tool_id=tid, call_id=f"call_{tid}") for tid in tool_ids]


@pytest.mark.asyncio
async def test_execute_all_runs_tool_calls_in_parallel():
    """两个工具各 sleep 200ms,并行后总耗时应 < 350ms(单串行至少 400ms)。"""
    behaviors = {
        "tool_a": {"sleep": 0.2, "result": {"data": "a"}},
        "tool_b": {"sleep": 0.2, "result": {"data": "b"}},
    }
    executor = _make_executor(behaviors, parallel=True)
    session = LearningSession()
    tool_calls = _make_tool_calls("tool_a", "tool_b")

    start = time.monotonic()
    outcomes = await executor.execute_all(
        session=session,
        tool_calls=tool_calls,
        turn_count=1,
        parent_span=None,
        trace_id=None,
        build_hook_context=_build_hook_context,
    )
    elapsed = time.monotonic() - start

    assert len(outcomes) == 2
    assert not outcomes[0]["is_error"]
    assert not outcomes[1]["is_error"]
    assert outcomes[0]["result"] == {"data": "a"}
    assert outcomes[1]["result"] == {"data": "b"}
    # 并行耗时上限留宽松一点,只要明显小于串行(0.4s)即可
    assert elapsed < 0.35, f"Expected parallel execution, but took {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_execute_all_serial_when_flag_disabled():
    """tool_parallel_execution=False 时,两个 200ms 工具应顺序执行,总耗时 >= 0.4s。"""
    behaviors = {
        "tool_a": {"sleep": 0.2, "result": {"data": "a"}},
        "tool_b": {"sleep": 0.2, "result": {"data": "b"}},
    }
    executor = _make_executor(behaviors, parallel=False)
    session = LearningSession()
    tool_calls = _make_tool_calls("tool_a", "tool_b")

    start = time.monotonic()
    outcomes = await executor.execute_all(
        session=session,
        tool_calls=tool_calls,
        turn_count=1,
        parent_span=None,
        trace_id=None,
        build_hook_context=_build_hook_context,
    )
    elapsed = time.monotonic() - start

    assert len(outcomes) == 2
    assert all(not o["is_error"] for o in outcomes)
    # 串行至少要 0.4s,留 5% 容差
    assert elapsed >= 0.38, f"Expected serial execution, but took {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_execute_all_preserves_outcome_order():
    """并行执行时,outcome 顺序应与输入 tool_calls 完全一致,即使后者更快。"""
    behaviors = {
        "slow_tool": {"sleep": 0.15, "result": {"data": "slow"}},
        "fast_tool": {"sleep": 0.01, "result": {"data": "fast"}},
    }
    executor = _make_executor(behaviors, parallel=True)
    session = LearningSession()
    # 故意把慢的放在前面
    tool_calls = _make_tool_calls("slow_tool", "fast_tool")

    outcomes = await executor.execute_all(
        session=session,
        tool_calls=tool_calls,
        turn_count=1,
        parent_span=None,
        trace_id=None,
        build_hook_context=_build_hook_context,
    )

    assert len(outcomes) == 2
    assert outcomes[0]["result"] == {"data": "slow"}
    assert outcomes[1]["result"] == {"data": "fast"}


@pytest.mark.asyncio
async def test_execute_all_isolates_pipeline_failures():
    """一个工具 pipeline 崩溃,其他工具的 outcome 不受影响。"""
    behaviors = {
        "broken_tool": {"sleep": 0.05, "raise": RuntimeError("boom")},
        "ok_tool": {"sleep": 0.05, "result": {"data": "fine"}},
    }
    executor = _make_executor(behaviors, parallel=True)
    session = LearningSession()
    tool_calls = _make_tool_calls("broken_tool", "ok_tool")

    outcomes = await executor.execute_all(
        session=session,
        tool_calls=tool_calls,
        turn_count=1,
        parent_span=None,
        trace_id=None,
        build_hook_context=_build_hook_context,
    )

    assert len(outcomes) == 2
    # broken_tool 的 outcome 是 error,但被 retry 抓住后转为 _execute_with_retry 失败,
    # 是 is_error=True;ok_tool 是 success
    broken_outcome = outcomes[0]
    ok_outcome = outcomes[1]
    assert broken_outcome["is_error"] is True
    assert ok_outcome["is_error"] is False
    assert ok_outcome["result"] == {"data": "fine"}


@pytest.mark.asyncio
async def test_execute_all_single_tool_call():
    """只有一个 tool_call 时,无论开关如何都应正常执行。"""
    behaviors = {"solo": {"sleep": 0.05, "result": {"data": "only"}}}
    executor = _make_executor(behaviors, parallel=True)
    session = LearningSession()
    tool_calls = _make_tool_calls("solo")

    outcomes = await executor.execute_all(
        session=session,
        tool_calls=tool_calls,
        turn_count=1,
        parent_span=None,
        trace_id=None,
        build_hook_context=_build_hook_context,
    )

    assert len(outcomes) == 1
    assert outcomes[0]["result"] == {"data": "only"}


@pytest.mark.asyncio
async def test_execute_all_empty_tool_calls():
    """空 tool_calls 列表应直接返回空 outcome 列表。"""
    executor = _make_executor({}, parallel=True)
    session = LearningSession()

    outcomes = await executor.execute_all(
        session=session,
        tool_calls=[],
        turn_count=1,
        parent_span=None,
        trace_id=None,
        build_hook_context=_build_hook_context,
    )

    assert outcomes == []
