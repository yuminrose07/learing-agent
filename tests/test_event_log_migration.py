from __future__ import annotations

import sys

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai import LearningSession, MessageRole, SessionEntry
from learning_agent.ai.file_store import FileStore
from learning_agent.learning_agent.session_event_store import SessionEventStore
from learning_agent.learning_agent.session_migration import migrate_legacy_session
from learning_agent.learning_agent.session_projection import replay_events


def test_migrate_legacy_session_creates_events_and_deletes_legacy_files(tmp_path):
    file_store = FileStore(str(tmp_path / "data"))
    session = LearningSession(
        id="sess-legacy",
        title="Legacy",
        entries=[
            SessionEntry(role=MessageRole.USER, content="hello"),
        ],
    )
    file_store.legacy_save_session(session.id, session.model_dump(mode="json"))
    file_store.legacy_append_session_delta(
        session.id,
        {
            "op": "append",
            "entry": SessionEntry(role=MessageRole.ASSISTANT, content="hi").model_dump(mode="json"),
        },
    )

    migrated = migrate_legacy_session(session.id, file_store)

    assert migrated is True
    assert (tmp_path / "data" / "sessions" / "sess-legacy.events.jsonl").exists()
    assert not (tmp_path / "data" / "sessions" / "sess-legacy.json").exists()
    assert not (tmp_path / "data" / "sessions" / "sess-legacy.jsonl").exists()

    events = SessionEventStore(file_store).read_events(session.id)
    snapshot = replay_events(events, session_id=session.id)
    assert [entry.role for entry in snapshot.messages] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert [entry.content for entry in snapshot.messages] == ["hello", "hi"]
