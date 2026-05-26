from __future__ import annotations

import sys


from learning_agent.ai import MessageRole, ToolCall
from learning_agent.ai.file_store import FileStore
from learning_agent.learning_agent.mode_service import build_turn_profile
from learning_agent.learning_agent.session_manager import SessionManager


def test_llm_input_view_uses_linear_event_projection_and_skips_failures(tmp_path):
    session_manager = SessionManager(file_store=FileStore(str(tmp_path / "data")))
    session = session_manager.create_session(title="view-test")
    session_manager.append_message(session.id, MessageRole.USER, "hello")
    session_manager.record_message_stream_failed(session.id, "network")
    session_manager.append_message(session.id, MessageRole.ASSISTANT, "hi")

    view = session_manager.build_llm_input_view(session.id, build_turn_profile(session.mode))

    assert [message.role for message in view.messages] == [
        MessageRole.SYSTEM,
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]
    assert [message.content for message in view.messages[1:]] == ["hello", "hi"]


def test_llm_input_view_preserves_assistant_tool_call_pair(tmp_path):
    session_manager = SessionManager(file_store=FileStore(str(tmp_path / "data")))
    session = session_manager.create_session(title="tool-view")
    session_manager.append_message(session.id, MessageRole.USER, "read file")
    session_manager.append_message(
        session.id,
        MessageRole.ASSISTANT,
        "",
        tool_calls=[ToolCall(tool_id="read_file", call_id="call-1", arguments={"path": "a.txt"})],
    )
    session_manager.append_message(
        session.id,
        MessageRole.TOOL,
        "contents",
        metadata={"tool_id": "read_file", "tool_call_id": "call-1", "is_error": False},
    )

    view = session_manager.build_llm_input_view(session.id, build_turn_profile(session.mode))

    assistant = next(message for message in view.messages if message.role == MessageRole.ASSISTANT)
    tool = next(message for message in view.messages if message.role == MessageRole.TOOL)
    assert assistant.tool_calls[0]["id"] == "call-1"
    assert tool.tool_call_id == "call-1"
