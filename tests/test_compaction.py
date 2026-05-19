from __future__ import annotations

import sys

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai import MessageRole, ToolCall
from learning_agent.ai.file_store import FileStore
from learning_agent.learning_agent.compaction import (
    CompactionCoordinator,
    build_micro_compacted_history,
)
from learning_agent.learning_agent.mode_service import build_turn_profile
from learning_agent.learning_agent.session_manager import SessionManager
from learning_agent.ai.models import AgentMode


def _append_tool_round(
    session_manager: SessionManager,
    session_id: str,
    *,
    user_text: str,
    tool_name: str,
    tool_result: str,
    assistant_text: str,
    call_id: str,
) -> None:
    session_manager.append_message(session_id, MessageRole.USER, user_text)
    session_manager.append_message(
        session_id,
        MessageRole.ASSISTANT,
        "",
        tool_calls=[ToolCall(tool_id=tool_name, call_id=call_id, arguments={"path": "demo.txt"})],
    )
    session_manager.append_message(
        session_id,
        MessageRole.TOOL,
        tool_result,
        metadata={"tool_id": tool_name, "tool_call_id": call_id, "is_error": False},
        tool_results=[
            {
                "tool_id": tool_name,
                "tool_call_id": call_id,
                "result": tool_result,
                "is_error": False,
            }
        ],
    )
    session_manager.append_message(session_id, MessageRole.ASSISTANT, assistant_text)


def test_micro_compact_rewrites_old_tool_groups_but_keeps_recent():
    session_manager = SessionManager()
    session = session_manager.create_session()

    _append_tool_round(
        session_manager,
        session.id,
        user_text="先读第一个文件",
        tool_name="read_file",
        tool_result="old result",
        assistant_text="第一个文件读完了",
        call_id="call-old",
    )
    _append_tool_round(
        session_manager,
        session.id,
        user_text="再搜一下关键词",
        tool_name="grep",
        tool_result="recent result",
        assistant_text="我找到关键词了",
        call_id="call-recent",
    )

    history = session_manager.get_message_history(session.id)
    compacted = build_micro_compacted_history(history, keep_recent_groups=1)
    compacted_ids = {entry.id for entry in compacted}

    old_tool_entry = next(entry for entry in history if entry.metadata.get("tool_call_id") == "call-old")
    old_assistant_entry = next(
        entry for entry in history if entry.role == MessageRole.ASSISTANT and entry.metadata.get("micro_compacted")
    ) if any(entry.metadata.get("micro_compacted") for entry in history) else None

    assert old_tool_entry.id not in compacted_ids
    compacted_assistant = next(entry for entry in compacted if entry.metadata.get("micro_compacted"))
    assert compacted_assistant.tool_calls == []
    assert "Earlier tool interaction compacted" in compacted_assistant.content
    assert "call-recent" in {entry.metadata.get("tool_call_id") for entry in compacted if entry.role == MessageRole.TOOL}
    assert old_assistant_entry is None


def test_compaction_coordinator_runs_full_then_incremental(tmp_path):
    file_store = FileStore(str(tmp_path / "data"))
    session_manager = SessionManager(file_store=file_store)
    session = session_manager.create_session()

    _append_tool_round(
        session_manager,
        session.id,
        user_text="读取 alpha",
        tool_name="read_file",
        tool_result="alpha contents " * 40,
        assistant_text="alpha 已分析",
        call_id="call-1",
    )
    _append_tool_round(
        session_manager,
        session.id,
        user_text="继续搜索 beta",
        tool_name="grep",
        tool_result="beta match " * 40,
        assistant_text="beta 已定位",
        call_id="call-2",
    )

    profile = build_turn_profile(AgentMode.CHAT).model_copy(
        update={"full_compact_threshold": 0.0001, "recent_token_budget": 20}
    )
    coordinator = CompactionCoordinator(session_manager, max_context_tokens=1000)

    first_plan = coordinator.evaluate_turn(session, "继续", profile, allow_full_compact=True)

    assert first_plan.use_full_compact is True
    assert first_plan.full_compact_scope == "full"
    assert first_plan.summary_block is not None
    assert "reads:" in first_plan.summary_block

    _append_tool_round(
        session_manager,
        session.id,
        user_text="再写回 gamma",
        tool_name="write_file",
        tool_result="gamma updated " * 40,
        assistant_text="gamma 已写回",
        call_id="call-3",
    )
    _append_tool_round(
        session_manager,
        session.id,
        user_text="最后查看 delta",
        tool_name="read_file",
        tool_result="delta contents " * 40,
        assistant_text="delta 已查看",
        call_id="call-4",
    )

    second_plan = coordinator.evaluate_turn(session, "继续", profile, allow_full_compact=True)

    assert second_plan.use_full_compact is True
    assert second_plan.full_compact_scope == "incremental"
    assert second_plan.summary_block is not None
    metadata = session_manager.get_compact_metadata(session.id)
    assert metadata is not None
    assert metadata.compact_anchor_entry_id is not None
    assert session_manager.load_compact_summary(session.id) is not None
