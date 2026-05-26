from __future__ import annotations

import sys


from learning_agent.ai.file_store import FileStore
from learning_agent.learning_agent.session_event_store import SessionEventStore
from learning_agent.learning_agent.session_events import SessionEventType


def test_append_event_assigns_monotonic_seq_and_uses_events_jsonl(tmp_path):
    file_store = FileStore(str(tmp_path / "data"))
    event_store = SessionEventStore(file_store)

    first = event_store.append_event(
        "sess-001",
        SessionEventType.SESSION_CREATED,
        {"title": "Event session"},
    )
    second = event_store.append_event(
        "sess-001",
        SessionEventType.MESSAGE_USER_APPENDED,
        {"role": "user", "content": "hello"},
    )

    assert first.seq == 1
    assert second.seq == 2
    assert first.event_id != second.event_id
    assert (tmp_path / "data" / "sessions" / "sess-001.events.jsonl").exists()
    assert not (tmp_path / "data" / "sessions" / "sess-001.json").exists()
    assert not (tmp_path / "data" / "sessions" / "sess-001.jsonl").exists()
    assert [event.seq for event in event_store.read_events("sess-001", after_seq=1)] == [2]


def test_delete_session_events_removes_event_log(tmp_path):
    file_store = FileStore(str(tmp_path / "data"))
    event_store = SessionEventStore(file_store)

    event_store.append_event("sess-delete", SessionEventType.SESSION_CREATED, {})

    assert event_store.delete_events("sess-delete") is True
    assert event_store.read_events("sess-delete") == []
