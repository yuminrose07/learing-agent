from __future__ import annotations

import sys

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai import AgentMode, EntryType, LearningSession, MessageRole, SessionEntry
from learning_agent.learning_agent.session_events import (
    EventVisibility,
    SessionEvent,
    SessionEventType,
)
from learning_agent.learning_agent.session_projection import project_legacy_session, replay_events


def test_replay_events_is_idempotent_for_duplicate_event_ids():
    created = SessionEvent(
        seq=1,
        event_id="evt-created",
        session_id="sess-proj",
        type=SessionEventType.SESSION_CREATED,
        payload={"title": "Projection", "mode": "chat"},
    )
    user = SessionEvent(
        seq=2,
        event_id="evt-user",
        session_id="sess-proj",
        type=SessionEventType.MESSAGE_USER_APPENDED,
        payload={"role": "user", "content": "hello"},
    )

    snapshot = replay_events([created, user, user], session_id="sess-proj")

    assert snapshot.title == "Projection"
    assert snapshot.mode == AgentMode.CHAT
    assert len(snapshot.messages) == 1
    assert snapshot.messages[0].role == MessageRole.USER
    assert snapshot.corrupt_events == []


def test_replay_events_stops_on_seq_gap():
    created = SessionEvent(
        seq=1,
        event_id="evt-created",
        session_id="sess-gap",
        type=SessionEventType.SESSION_CREATED,
        payload={},
    )
    skipped = SessionEvent(
        seq=3,
        event_id="evt-gap",
        session_id="sess-gap",
        type=SessionEventType.MESSAGE_USER_APPENDED,
        payload={"role": "user", "content": "should not apply"},
    )

    snapshot = replay_events([created, skipped], session_id="sess-gap")

    assert snapshot.messages == []
    assert snapshot.corrupt_events[0]["expected_seq"] == 2


def test_project_legacy_session_linearizes_current_leaf_path():
    root = SessionEntry(
        id="entry-root",
        type=EntryType.MESSAGE,
        role=MessageRole.SYSTEM,
        content="root",
    )
    first = SessionEntry(
        id="entry-user",
        parent_id="entry-root",
        type=EntryType.MESSAGE,
        role=MessageRole.USER,
        content="first",
    )
    fork = SessionEntry(
        id="entry-fork",
        parent_id="entry-user",
        type=EntryType.FORK_POINT,
        content="fork",
    )
    assistant = SessionEntry(
        id="entry-assistant",
        parent_id="entry-fork",
        type=EntryType.MESSAGE,
        role=MessageRole.ASSISTANT,
        content="answer",
    )
    session = LearningSession(
        id="sess-legacy",
        root_entry_id=root.id,
        current_leaf_id=assistant.id,
        entries=[root, first, fork, assistant],
    )

    snapshot = project_legacy_session(session)

    assert [entry.id for entry in snapshot.messages] == ["entry-root", "entry-user", "entry-assistant"]
    assert all(entry.parent_id is None for entry in snapshot.messages)


def test_replay_ignores_observability_events():
    """事实源护栏：visibility=OBSERVABILITY 的事件不参与 replay/projection。

    构造两份事件流：
    - baseline：纯业务事件
    - augmented：在业务事件之间插入 OBSERVABILITY 事件（占用 seq 编号）

    两次 replay 出来的 state 必须等价（messages / mode / title / status / compact_metadata）。
    这是 L1 trace 事件接入前必须先红、加过滤后必须转绿的护栏测试。
    """
    sid = "sess-obs-guard"

    created = SessionEvent(
        seq=1,
        event_id="evt-created",
        session_id=sid,
        type=SessionEventType.SESSION_CREATED,
        payload={"title": "Guard", "mode": "chat"},
        visibility=EventVisibility.SYSTEM,
    )
    user1 = SessionEvent(
        seq=2,
        event_id="evt-user1",
        session_id=sid,
        type=SessionEventType.MESSAGE_USER_APPENDED,
        payload={"role": "user", "content": "hello"},
        visibility=EventVisibility.AGENT,
    )
    end1 = SessionEvent(
        seq=3,
        event_id="evt-end1",
        session_id=sid,
        type=SessionEventType.MESSAGE_END,
        payload={"role": "assistant", "content": "hi there"},
        visibility=EventVisibility.AGENT,
    )

    baseline = replay_events([created, user1, end1], session_id=sid)

    # augmented 版本：在业务事件之间插入 OBSERVABILITY 事件，
    # 这些事件占用 seq=2/4，业务事件 seq 顺移到 3/5/6。
    created_a = created.model_copy(update={"seq": 1})
    obs_pre_user = SessionEvent(
        seq=2,
        event_id="evt-obs-1",
        session_id=sid,
        type=SessionEventType.TOOL_EXEC_STARTED,
        payload={"tool_name": "fake_tool", "args": {"q": "anything"}},
        visibility=EventVisibility.OBSERVABILITY,
        parent_event_id=created.event_id,
    )
    user1_a = user1.model_copy(update={"seq": 3})
    obs_pre_end = SessionEvent(
        seq=4,
        event_id="evt-obs-2",
        session_id=sid,
        type=SessionEventType.TOOL_EXEC_COMPLETED,
        payload={"tool_name": "fake_tool", "latency_ms": 42, "result": "ok"},
        visibility=EventVisibility.OBSERVABILITY,
        parent_event_id=obs_pre_user.event_id,
    )
    end1_a = end1.model_copy(update={"seq": 5})

    augmented = replay_events(
        [created_a, obs_pre_user, user1_a, obs_pre_end, end1_a],
        session_id=sid,
    )

    # 核心不变量：业务 state 完全相同
    assert augmented.title == baseline.title
    assert augmented.mode == baseline.mode
    assert augmented.status == baseline.status
    assert len(augmented.messages) == len(baseline.messages)
    for actual, expected in zip(augmented.messages, baseline.messages):
        assert actual.role == expected.role
        assert actual.content == expected.content
    # 没有事件被标为 corrupt（observability 事件被合法跳过而非破坏）
    assert augmented.corrupt_events == [], (
        f"observability events caused replay to mark corrupt: {augmented.corrupt_events}"
    )


def test_replay_filters_business_event_types_marked_observability():
    """更严格的护栏：即使事件 type 是 business（如 MESSAGE_USER_APPENDED），
    只要 visibility=OBSERVABILITY，就必须被 replay 跳过，不能进入 messages。

    这是真正验证 visibility 过滤器有效的测试。在加过滤前应该 RED
    （因为 _apply_event 只看 type 不看 visibility）。
    """
    sid = "sess-obs-strict"
    created = SessionEvent(
        seq=1,
        event_id="evt-created",
        session_id=sid,
        type=SessionEventType.SESSION_CREATED,
        payload={"title": "Strict", "mode": "chat"},
        visibility=EventVisibility.SYSTEM,
    )
    # 关键：业务类型 + OBSERVABILITY visibility。
    # 这是一个"伪装成业务事件的诊断事件"，必须被过滤掉。
    user_obs = SessionEvent(
        seq=2,
        event_id="evt-user-obs",
        session_id=sid,
        type=SessionEventType.MESSAGE_USER_APPENDED,
        payload={"role": "user", "content": "this should NOT appear in messages"},
        visibility=EventVisibility.OBSERVABILITY,
    )
    user_real = SessionEvent(
        seq=3,
        event_id="evt-user-real",
        session_id=sid,
        type=SessionEventType.MESSAGE_USER_APPENDED,
        payload={"role": "user", "content": "this SHOULD appear"},
        visibility=EventVisibility.AGENT,
    )

    snapshot = replay_events([created, user_obs, user_real], session_id=sid)

    # 关键断言：observability 事件不能进入 messages
    contents = [entry.content for entry in snapshot.messages]
    assert "this should NOT appear in messages" not in contents, (
        f"OBSERVABILITY-marked event leaked into messages: {contents}"
    )
    assert "this SHOULD appear" in contents
    assert len(snapshot.messages) == 1, f"expected 1 message, got {len(snapshot.messages)}: {contents}"
