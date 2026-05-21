from __future__ import annotations

import sys

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai import MessageRole
from learning_agent.ai.file_store import FileStore
from learning_agent.learning_agent.compaction import CompactionCoordinator
from learning_agent.learning_agent.mode_service import build_turn_profile
from learning_agent.learning_agent.session_manager import SessionManager
from learning_agent.learning_agent.session_events import SessionEventType


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


def test_compact_summary_event_is_authoritative_and_filters_provider_history(tmp_path):
    file_store = FileStore(str(tmp_path / "data"))
    session_manager = SessionManager(file_store=file_store)
    session = session_manager.create_session()

    for index in range(5):
        session_manager.append_message(session.id, MessageRole.USER, f"source-user-{index} " * 30)
        session_manager.append_message(session.id, MessageRole.ASSISTANT, f"source-assistant-{index} " * 30)

    profile = build_turn_profile(session.mode).model_copy(
        update={"full_compact_threshold": 0.0001, "recent_token_budget": 20}
    )
    coordinator = CompactionCoordinator(session_manager, max_context_tokens=1000)

    plan = coordinator.evaluate_turn(session, "continue", profile, allow_full_compact=True)

    assert plan.use_full_compact is True
    assert plan.summary_block is not None
    assert plan.source_entry_ids
    assert plan.retained_entry_ids

    events = file_store.read_session_events(session.id)
    compact_event = next(event for event in events if event["type"] == SessionEventType.COMPACTION_SUMMARY_ADDED)
    payload = compact_event["payload"]
    assert payload["summary_text"]
    assert "<analysis>" not in payload["summary_text"]
    assert payload["source_entry_ids"] == plan.source_entry_ids
    assert payload["retained_entry_ids"] == plan.retained_entry_ids
    assert payload["summary_hash"] == session_manager.get_compact_metadata(session.id).last_summary_hash

    view = session_manager.build_llm_input_view(session.id, profile, compaction_plan=plan)
    non_system_contents = [message.content for message in view.messages if message.role != MessageRole.SYSTEM]
    source_entries = [
        entry for entry in session_manager.get_message_history(session.id)
        if entry.id in plan.source_entry_ids
    ]
    retained_entries = [
        entry for entry in session_manager.get_message_history(session.id)
        if entry.id in plan.retained_entry_ids
    ]

    assert source_entries
    assert retained_entries
    assert all(entry.content not in non_system_contents for entry in source_entries)
    assert all(entry.content in non_system_contents for entry in retained_entries)


def test_latest_compact_summary_replayed_without_current_turn_plan(tmp_path):
    file_store = FileStore(str(tmp_path / "data"))
    session_manager = SessionManager(file_store=file_store)
    session = session_manager.create_session()

    for index in range(5):
        session_manager.append_message(session.id, MessageRole.USER, f"old-user-{index} " * 30)
        session_manager.append_message(session.id, MessageRole.ASSISTANT, f"old-assistant-{index} " * 30)

    profile = build_turn_profile(session.mode).model_copy(
        update={"full_compact_threshold": 0.0001, "recent_token_budget": 20}
    )
    plan = CompactionCoordinator(session_manager, max_context_tokens=1000).evaluate_turn(
        session,
        "continue",
        profile,
        allow_full_compact=True,
    )

    session_manager.append_message(session.id, MessageRole.USER, "new request after compact")

    view = session_manager.build_llm_input_view(session.id, profile)
    system_blocks = [message.content for message in view.messages if message.role == MessageRole.SYSTEM]
    non_system_contents = [message.content for message in view.messages if message.role != MessageRole.SYSTEM]
    source_entries = [
        entry for entry in session_manager.get_message_history(session.id)
        if entry.id in plan.source_entry_ids
    ]

    assert any("[Compact Summary]" in block for block in system_blocks)
    assert "new request after compact" in non_system_contents
    assert all(entry.content not in non_system_contents for entry in source_entries)
