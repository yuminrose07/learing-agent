"""L1 trace 事件落地的护栏测试：tool.exec_started / completed / failed.

验证：
- ToolExecutor 注入 event_writer 后会向 L1 写入 tool.exec_* 诊断事件
- 这些事件 visibility=observability，不参与 replay/projection（已有 session_projection 测试锁住）
- started → completed/failed 形成 parent_event_id 因果链
- event_writer=None 时整条链路退化为 no-op，不破坏业务执行
- L1 写入失败不影响业务执行（容错性）
"""

from __future__ import annotations

import sys
from typing import Any, Optional

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai import ToolCall
from learning_agent.agent.tool_executor import ToolExecutor
from learning_agent.learning_agent.session_events import (
    EventVisibility,
    SessionEvent,
    SessionEventType,
)


class _InMemoryEventWriter:
    """实现 SessionEventWriter Protocol 的内存版，用于断言写入。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._seq = 0

    def append_event(
        self,
        session_id: str,
        type: str,
        payload: Optional[dict[str, Any]] = None,
        visibility: str = "agent",
        parent_event_id: Optional[str] = None,
    ) -> SessionEvent:
        self._seq += 1
        event = SessionEvent(
            seq=self._seq,
            event_id=f"evt-test-{self._seq:04d}",
            session_id=session_id,
            type=type,
            payload=payload or {},
            visibility=visibility,
            parent_event_id=parent_event_id,
        )
        self.events.append(
            {
                "seq": event.seq,
                "event_id": event.event_id,
                "type": event.type,
                "payload": dict(event.payload),
                "visibility": event.visibility,
                "parent_event_id": event.parent_event_id,
            }
        )
        return event


def _make_minimal_executor(event_writer: Optional[_InMemoryEventWriter] = None) -> ToolExecutor:
    """构造一个只插了 event_writer 的 ToolExecutor。

    helpers _emit_exec_* 不依赖其他注入项，所以其余字段直接传 None。
    """
    # 跳过 __init__ 里依赖 ResilienceConfig 等的字段，直接绑定我们需要的属性
    executor = ToolExecutor.__new__(ToolExecutor)
    executor._event_writer = event_writer
    return executor


def test_emit_exec_started_writes_observability_event_with_args():
    writer = _InMemoryEventWriter()
    executor = _make_minimal_executor(writer)
    tc = ToolCall(tool_id="read_file", call_id="call-123", arguments={"path": "a.txt"})

    started_id = executor._emit_exec_started(
        session_id="sess-x",
        tc=tc,
        tool_call_id="call-123",
        attempt=0,
        timeout=5.0,
    )

    assert started_id == "evt-test-0001"
    assert len(writer.events) == 1
    evt = writer.events[0]
    assert evt["type"] == SessionEventType.TOOL_EXEC_STARTED
    assert evt["visibility"] == EventVisibility.OBSERVABILITY
    assert evt["payload"]["tool_name"] == "read_file"
    assert evt["payload"]["call_id"] == "call-123"
    assert evt["payload"]["args"] == {"path": "a.txt"}
    assert evt["payload"]["attempt"] == 0
    assert evt["payload"]["timeout"] == 5.0
    # started 是因果链的起点，自身不挂 parent
    assert evt["parent_event_id"] is None


def test_emit_exec_completed_chains_to_started_event():
    writer = _InMemoryEventWriter()
    executor = _make_minimal_executor(writer)
    tc = ToolCall(tool_id="read_file", call_id="call-7", arguments={"path": "b.txt"})

    started_id = executor._emit_exec_started(
        session_id="sess-y", tc=tc, tool_call_id="call-7", attempt=0, timeout=None,
    )
    executor._emit_exec_completed(
        session_id="sess-y",
        tc=tc,
        tool_call_id="call-7",
        attempt=0,
        latency_ms=12.345,
        result="file body",
        parent_event_id=started_id,
    )

    assert [e["type"] for e in writer.events] == [
        SessionEventType.TOOL_EXEC_STARTED,
        SessionEventType.TOOL_EXEC_COMPLETED,
    ]
    completed = writer.events[1]
    # 因果链：completed.parent = started.event_id
    assert completed["parent_event_id"] == started_id
    assert completed["visibility"] == EventVisibility.OBSERVABILITY
    assert completed["payload"]["latency_ms"] == 12.345
    assert completed["payload"]["result"] == "file body"
    assert completed["payload"]["result_size"] == len("file body")


def test_emit_exec_failed_carries_error_type_and_message():
    writer = _InMemoryEventWriter()
    executor = _make_minimal_executor(writer)
    tc = ToolCall(tool_id="net_call", call_id="call-9", arguments={"url": "http://x"})

    started_id = executor._emit_exec_started(
        session_id="sess-z", tc=tc, tool_call_id="call-9", attempt=1, timeout=2.0,
    )
    error = ConnectionError("ECONNREFUSED 127.0.0.1:80")
    executor._emit_exec_failed(
        session_id="sess-z",
        tc=tc,
        tool_call_id="call-9",
        attempt=1,
        latency_ms=3.5,
        error=error,
        parent_event_id=started_id,
    )

    failed = writer.events[-1]
    assert failed["type"] == SessionEventType.TOOL_EXEC_FAILED
    assert failed["visibility"] == EventVisibility.OBSERVABILITY
    assert failed["parent_event_id"] == started_id
    assert failed["payload"]["error_type"] == "ConnectionError"
    assert "ECONNREFUSED" in failed["payload"]["error_message"]
    assert failed["payload"]["attempt"] == 1


def test_emit_helpers_no_op_when_writer_is_none():
    """事实源护栏：未注入 writer 时不能尝试写入（不能抛异常、不能影响业务）。"""
    executor = _make_minimal_executor(event_writer=None)
    tc = ToolCall(tool_id="t", call_id="c")

    # 这三个调用都应该静默返回，不抛错
    started_id = executor._emit_exec_started(
        session_id="s", tc=tc, tool_call_id="c", attempt=0, timeout=None,
    )
    assert started_id is None
    executor._emit_exec_completed(
        session_id="s", tc=tc, tool_call_id="c", attempt=0,
        latency_ms=1.0, result="ok", parent_event_id=None,
    )
    executor._emit_exec_failed(
        session_id="s", tc=tc, tool_call_id="c", attempt=0,
        latency_ms=1.0, error=RuntimeError("x"), parent_event_id=None,
    )


def test_emit_helpers_swallow_writer_exception():
    """L1 写入失败不能影响业务执行 —— 任何 writer 异常必须被吞掉。"""

    class _BrokenWriter:
        def append_event(self, **kwargs):
            raise RuntimeError("disk full")

    executor = _make_minimal_executor(event_writer=_BrokenWriter())
    tc = ToolCall(tool_id="t", call_id="c")

    # 不应抛异常
    started_id = executor._emit_exec_started(
        session_id="s", tc=tc, tool_call_id="c", attempt=0, timeout=None,
    )
    assert started_id is None
    executor._emit_exec_completed(
        session_id="s", tc=tc, tool_call_id="c", attempt=0,
        latency_ms=1.0, result="ok", parent_event_id=None,
    )
    executor._emit_exec_failed(
        session_id="s", tc=tc, tool_call_id="c", attempt=0,
        latency_ms=1.0, error=RuntimeError("x"), parent_event_id=None,
    )


def test_safe_result_repr_truncates_long_payload():
    long = "x" * 5000
    repr_ = ToolExecutor._safe_result_repr(long)
    assert repr_.startswith("x" * 4096)
    assert "truncated" in repr_


def test_safe_result_repr_serializes_non_string():
    repr_ = ToolExecutor._safe_result_repr({"a": 1, "b": [1, 2]})
    # 必须是合法 JSON / 至少包含可读形式
    assert '"a": 1' in repr_ or "'a': 1" in repr_
