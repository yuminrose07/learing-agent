from __future__ import annotations

import sys


from learning_agent.ai import MessageRole
from learning_agent.ai.file_store import FileStore
from learning_agent.learning_agent.session_manager import SessionManager


def test_ui_message_view_exposes_user_and_assistant_only(tmp_path):
    session_manager = SessionManager(file_store=FileStore(str(tmp_path / "data")))
    session = session_manager.create_session(title="ui-view")
    session_manager.append_message(session.id, MessageRole.USER, "hello", metadata={"mode": "chat"})
    session_manager.append_message(
        session.id,
        MessageRole.TOOL,
        "sensitive output",
        metadata={"tool_id": "read_file", "tool_call_id": "call-1", "sensitive": True},
    )
    session_manager.append_message(
        session.id,
        MessageRole.ASSISTANT,
        "done",
        metadata={"mode": "chat", "reasoning_content": "hidden"},
    )

    messages = session_manager.build_ui_messages(session.id)

    assert [message.role for message in messages] == ["user", "assistant"]
    assert [message.content for message in messages] == ["hello", "done"]
    assert "reasoning_content" not in messages[1].metadata
