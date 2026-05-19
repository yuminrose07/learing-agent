from __future__ import annotations

import sys

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai import MessageRole
from learning_agent.ai.file_store import FileStore
from learning_agent.learning_agent.compaction import CompactionCoordinator
from learning_agent.learning_agent.mode_service import build_turn_profile
from learning_agent.learning_agent.session_manager import SessionManager


def test_full_compact_records_event_source_cursor(tmp_path):
    file_store = FileStore(str(tmp_path / "data"))
    session_manager = SessionManager(file_store=file_store)
    session = session_manager.create_session()

    for index in range(4):
        session_manager.append_message(session.id, MessageRole.USER, f"user {index} " * 40)
        session_manager.append_message(session.id, MessageRole.ASSISTANT, f"assistant {index} " * 40)

    profile = build_turn_profile(session.mode).model_copy(
        update={"full_compact_threshold": 0.0001, "recent_token_budget": 20}
    )
    coordinator = CompactionCoordinator(session_manager, max_context_tokens=1000)

    plan = coordinator.evaluate_turn(session, "continue", profile, allow_full_compact=True)

    assert plan.use_full_compact is True
    metadata = session_manager.get_compact_metadata(session.id)
    assert metadata is not None
    assert metadata.source_event_start_seq is not None
    assert metadata.source_event_end_seq is not None
    assert metadata.next_jsonl_cursor == metadata.source_event_end_seq
    events = file_store.read_session_events(session.id)
    assert any(event["type"] == "compaction.summary_added" for event in events)
