"""Self-tests for `tests/observability_asserts.py`.

锁住的不变量：
- assert_event_present：精确匹配；payload_contains 是子集；找不到 → AssertionError + 候选清单
- assert_causal_chain_resolvable：缺失/跨 session/seq 倒挂的 parent 全报错
- assert_tool_exec_paired：started/completed/failed 配对、孤立 close、重复 close 全报错
- assert_visibility_isolation：强校验（event_id 字段）+ 弱校验（content 子串）
"""

from __future__ import annotations

from typing import Any

import pytest

from learning_agent.learning_agent.session_events import (
    EventVisibility,
    SessionEvent,
    SessionEventType,
)
from tests.observability_asserts import (
    assert_causal_chain_resolvable,
    assert_event_present,
    assert_tool_exec_paired,
    assert_visibility_isolation,
)


def _evt(
    seq: int,
    type: str,
    *,
    session_id: str = "sess-1",
    event_id: str | None = None,
    payload: dict[str, Any] | None = None,
    visibility: str = EventVisibility.AGENT,
    parent_event_id: str | None = None,
) -> SessionEvent:
    return SessionEvent(
        seq=seq,
        event_id=event_id or f"evt-{seq:04d}",
        session_id=session_id,
        type=type,
        payload=payload or {},
        visibility=visibility,
        parent_event_id=parent_event_id,
    )


# ─── assert_event_present ────────────────────────────────────────────────────


def test_assert_event_present_returns_first_match():
    events = [
        _evt(1, "message.user_appended", payload={"text": "hi"}),
        _evt(2, "tool.exec_started", payload={"call_id": "c1"}, visibility=EventVisibility.OBSERVABILITY),
        _evt(3, "tool.exec_started", payload={"call_id": "c2"}, visibility=EventVisibility.OBSERVABILITY),
    ]
    match = assert_event_present(events, type="tool.exec_started")
    assert match.seq == 2
    assert match.payload["call_id"] == "c1"


def test_assert_event_present_filters_by_payload_subset():
    events = [
        _evt(1, "tool.exec_started", payload={"call_id": "c1", "tool_name": "read_file"}),
        _evt(2, "tool.exec_started", payload={"call_id": "c2", "tool_name": "bash"}),
    ]
    match = assert_event_present(
        events, type="tool.exec_started", payload_contains={"tool_name": "bash"}
    )
    assert match.seq == 2


def test_assert_event_present_filters_by_visibility():
    events = [
        _evt(1, "x.y", visibility=EventVisibility.AGENT),
        _evt(2, "x.y", visibility=EventVisibility.OBSERVABILITY),
    ]
    match = assert_event_present(events, type="x.y", visibility=EventVisibility.OBSERVABILITY)
    assert match.seq == 2


def test_assert_event_present_filters_by_parent_event_id():
    events = [
        _evt(1, "a", event_id="evt-A"),
        _evt(2, "b", parent_event_id="evt-A"),
        _evt(3, "b", parent_event_id=None),
    ]
    match = assert_event_present(events, type="b", parent_event_id="evt-A")
    assert match.seq == 2

    no_parent = assert_event_present(events, type="b", parent_event_id="")
    assert no_parent.seq == 3


def test_assert_event_present_raises_with_candidates_when_no_match():
    events = [
        _evt(1, "tool.exec_started", payload={"call_id": "c1"}),
        _evt(2, "tool.exec_started", payload={"call_id": "c2"}),
    ]
    with pytest.raises(AssertionError) as exc_info:
        assert_event_present(events, type="tool.exec_started", payload_contains={"call_id": "missing"})
    msg = str(exc_info.value)
    # 候选清单必须包含同 type 的事件
    assert "candidates" in msg
    assert "call_id='c1'" in msg or "'call_id': 'c1'" in msg or "call_id=" in msg


def test_assert_event_present_raises_when_type_absent_lists_available():
    events = [_evt(1, "a"), _evt(2, "b")]
    with pytest.raises(AssertionError) as exc_info:
        assert_event_present(events, type="nope")
    msg = str(exc_info.value)
    assert "type='nope'" in msg
    assert "no events with type='nope'" in msg


# ─── assert_causal_chain_resolvable ──────────────────────────────────────────


def test_assert_causal_chain_resolvable_passes_for_valid_chain():
    events = [
        _evt(1, "a", event_id="evt-A"),
        _evt(2, "b", event_id="evt-B", parent_event_id="evt-A"),
        _evt(3, "c", parent_event_id=None),  # 无 parent 不参与校验
    ]
    assert_causal_chain_resolvable(events)


def test_assert_causal_chain_resolvable_detects_missing_parent():
    events = [
        _evt(1, "a", event_id="evt-A"),
        _evt(2, "b", parent_event_id="evt-MISSING"),
    ]
    with pytest.raises(AssertionError) as exc_info:
        assert_causal_chain_resolvable(events)
    assert "evt-MISSING" in str(exc_info.value)
    assert "not found" in str(exc_info.value)


def test_assert_causal_chain_resolvable_detects_cross_session_parent():
    events = [
        _evt(1, "a", event_id="evt-A", session_id="sess-X"),
        _evt(2, "b", parent_event_id="evt-A", session_id="sess-Y"),
    ]
    with pytest.raises(AssertionError) as exc_info:
        assert_causal_chain_resolvable(events)
    assert "session_id" in str(exc_info.value)


def test_assert_causal_chain_resolvable_detects_seq_inversion():
    events = [
        _evt(5, "child", event_id="evt-C", parent_event_id="evt-P"),
        _evt(10, "parent", event_id="evt-P"),  # parent 在 child 之后 — 违例
    ]
    with pytest.raises(AssertionError) as exc_info:
        assert_causal_chain_resolvable(events)
    assert "must precede" in str(exc_info.value)


# ─── assert_tool_exec_paired ─────────────────────────────────────────────────


def test_assert_tool_exec_paired_passes_for_started_completed():
    events = [
        _evt(1, SessionEventType.TOOL_EXEC_STARTED, payload={"call_id": "c1"}),
        _evt(2, SessionEventType.TOOL_EXEC_COMPLETED, payload={"call_id": "c1"}),
    ]
    assert_tool_exec_paired(events)


def test_assert_tool_exec_paired_passes_for_started_failed():
    events = [
        _evt(1, SessionEventType.TOOL_EXEC_STARTED, payload={"call_id": "c1"}),
        _evt(2, SessionEventType.TOOL_EXEC_FAILED, payload={"call_id": "c1"}),
    ]
    assert_tool_exec_paired(events)


def test_assert_tool_exec_paired_detects_unmatched_started():
    events = [
        _evt(1, SessionEventType.TOOL_EXEC_STARTED, payload={"call_id": "c1"}),
        _evt(2, SessionEventType.TOOL_EXEC_COMPLETED, payload={"call_id": "c1"}),
        _evt(3, SessionEventType.TOOL_EXEC_STARTED, payload={"call_id": "c2"}),
        # c2 没 close
    ]
    with pytest.raises(AssertionError) as exc_info:
        assert_tool_exec_paired(events)
    msg = str(exc_info.value)
    assert "unmatched tool.exec_started" in msg
    assert "c2" in msg


def test_assert_tool_exec_paired_detects_orphan_close():
    events = [
        _evt(1, SessionEventType.TOOL_EXEC_COMPLETED, payload={"call_id": "no_started"}),
    ]
    with pytest.raises(AssertionError) as exc_info:
        assert_tool_exec_paired(events)
    assert "orphan close" in str(exc_info.value)


def test_assert_tool_exec_paired_detects_duplicate_close():
    events = [
        _evt(1, SessionEventType.TOOL_EXEC_STARTED, payload={"call_id": "c1"}),
        _evt(2, SessionEventType.TOOL_EXEC_COMPLETED, payload={"call_id": "c1"}),
        _evt(3, SessionEventType.TOOL_EXEC_FAILED, payload={"call_id": "c1"}),
    ]
    with pytest.raises(AssertionError) as exc_info:
        assert_tool_exec_paired(events)
    assert "duplicate close" in str(exc_info.value)


def test_assert_tool_exec_paired_detects_duplicate_started():
    events = [
        _evt(1, SessionEventType.TOOL_EXEC_STARTED, payload={"call_id": "c1"}),
        _evt(2, SessionEventType.TOOL_EXEC_STARTED, payload={"call_id": "c1"}),
    ]
    with pytest.raises(AssertionError) as exc_info:
        assert_tool_exec_paired(events)
    assert "duplicate tool.exec_started" in str(exc_info.value)


def test_assert_tool_exec_paired_detects_missing_call_id_on_started():
    events = [
        _evt(1, SessionEventType.TOOL_EXEC_STARTED, payload={}),
    ]
    with pytest.raises(AssertionError) as exc_info:
        assert_tool_exec_paired(events)
    assert "missing payload.call_id" in str(exc_info.value)


def test_assert_tool_exec_paired_no_op_when_no_tool_events():
    events = [_evt(1, "message.user_appended")]
    # 不应抛错
    assert_tool_exec_paired(events)


# ─── assert_visibility_isolation ─────────────────────────────────────────────


class _FakeMessage:
    """模拟 SessionEntry 的最小接口（content + 可选 metadata）。"""

    def __init__(self, content: str = "", metadata: dict[str, Any] | None = None,
                 event_id: str | None = None) -> None:
        self.content = content
        self.metadata = metadata or {}
        if event_id is not None:
            self.event_id = event_id


def test_assert_visibility_isolation_passes_when_no_obs_events():
    events = [_evt(1, "message.user_appended")]
    assert_visibility_isolation(events, [_FakeMessage(content="hi")])


def test_assert_visibility_isolation_passes_for_clean_messages():
    events = [
        _evt(1, "message.user_appended"),
        _evt(2, SessionEventType.TOOL_EXEC_STARTED, payload={"call_id": "c1"},
             visibility=EventVisibility.OBSERVABILITY, event_id="evt-obs-1"),
    ]
    messages = [_FakeMessage(content="hello")]
    assert_visibility_isolation(events, messages)


def test_assert_visibility_isolation_detects_event_id_leak_via_attr():
    events = [
        _evt(1, SessionEventType.TOOL_EXEC_STARTED, event_id="evt-obs-leaked",
             visibility=EventVisibility.OBSERVABILITY, payload={"call_id": "c1"}),
    ]
    leaky = _FakeMessage(content="ok", event_id="evt-obs-leaked")
    with pytest.raises(AssertionError) as exc_info:
        assert_visibility_isolation(events, [leaky])
    msg = str(exc_info.value)
    assert "evt-obs-leaked" in msg
    assert "observability event" in msg


def test_assert_visibility_isolation_detects_event_id_leak_via_metadata():
    events = [
        _evt(1, SessionEventType.TOOL_EXEC_STARTED, event_id="evt-obs-meta",
             visibility=EventVisibility.OBSERVABILITY, payload={"call_id": "c1"}),
    ]
    leaky = _FakeMessage(content="ok", metadata={"source_event_id": "evt-obs-meta"})
    with pytest.raises(AssertionError) as exc_info:
        assert_visibility_isolation(events, [leaky])
    assert "metadata.source_event_id" in str(exc_info.value)


def test_assert_visibility_isolation_detects_content_substring_leak():
    events = [
        _evt(1, SessionEventType.TOOL_EXEC_STARTED, payload={"call_id": "c1"},
             visibility=EventVisibility.OBSERVABILITY),
    ]
    leaky = _FakeMessage(content="something tool.exec_started leaked through here")
    with pytest.raises(AssertionError) as exc_info:
        assert_visibility_isolation(events, [leaky])
    assert "tool.exec_started" in str(exc_info.value)


# ─── 综合：用前面的 helpers 互验一组真实形态事件 ──────────────────────────────


def test_assertions_compose_on_realistic_event_log():
    events = [
        _evt(1, "session.created", event_id="evt-001"),
        _evt(2, "message.user_appended", event_id="evt-002", payload={"text": "read it"}),
        _evt(3, SessionEventType.TOOL_EXEC_STARTED, event_id="evt-003",
             payload={"call_id": "call-A", "tool_name": "read_file"},
             visibility=EventVisibility.OBSERVABILITY),
        _evt(4, SessionEventType.TOOL_EXEC_COMPLETED, event_id="evt-004",
             payload={"call_id": "call-A", "latency_ms": 5.0},
             visibility=EventVisibility.OBSERVABILITY,
             parent_event_id="evt-003"),
        _evt(5, "message_end", event_id="evt-005"),
    ]
    assert_causal_chain_resolvable(events)
    assert_tool_exec_paired(events)
    started = assert_event_present(
        events, type=SessionEventType.TOOL_EXEC_STARTED, payload_contains={"call_id": "call-A"}
    )
    completed = assert_event_present(
        events, type=SessionEventType.TOOL_EXEC_COMPLETED, parent_event_id=started.event_id
    )
    assert completed.event_id == "evt-004"
    assert_visibility_isolation(events, [_FakeMessage(content="hi"), _FakeMessage(content="done")])
