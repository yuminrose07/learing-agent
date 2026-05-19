from __future__ import annotations

import sys

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai import AgentMode, EntryType, LearningSession, MessageRole, SessionEntry
from learning_agent.learning_agent.session_events import SessionEvent, SessionEventType
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
