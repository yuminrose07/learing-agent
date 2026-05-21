from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from learning_agent.ai import ChatMessage, MessageRole, SessionEntry
from learning_agent.learning_agent.compaction import CompactionPlan, build_micro_compacted_history
from learning_agent.learning_agent.compaction.full_compact import render_summary_text_block
from learning_agent.learning_agent.mode_service import TurnExecutionProfile
from learning_agent.learning_agent.session_projection import AgentSnapshot


@dataclass
class LLMInputView:
    session_id: str
    messages: list[ChatMessage]
    source_event_range: tuple[int, int] | None = None


@dataclass
class UIViewMessage:
    id: str
    role: str
    content: str
    status: str = "complete"
    metadata: dict[str, Any] = field(default_factory=dict)


def build_llm_input_view(
    snapshot: AgentSnapshot,
    profile: TurnExecutionProfile,
    compaction_plan: CompactionPlan | None = None,
) -> LLMInputView:
    messages: list[ChatMessage] = [
        ChatMessage(role=MessageRole.SYSTEM, content=profile.system_prompt)
    ]

    history = _filter_llm_history(snapshot.messages)
    history = _compress_tool_error_history(history)
    summary_block = _resolve_summary_block(snapshot, compaction_plan)
    if summary_block:
        history = _filter_compacted_history(history, snapshot, compaction_plan)
    if compaction_plan and compaction_plan.use_micro_compact:
        history = build_micro_compacted_history(history)
    if summary_block:
        messages.append(ChatMessage(role=MessageRole.SYSTEM, content=summary_block))
    messages.extend(_history_to_chat_messages(history))

    return LLMInputView(
        session_id=snapshot.session_id,
        messages=messages,
        source_event_range=snapshot.source_event_range,
    )


def build_ui_messages(snapshot: AgentSnapshot) -> list[UIViewMessage]:
    messages: list[UIViewMessage] = []
    for entry in snapshot.messages:
        if entry.role not in {MessageRole.USER, MessageRole.ASSISTANT}:
            continue
        if entry.role == MessageRole.ASSISTANT and not entry.content.strip() and entry.tool_calls:
            continue
        messages.append(
            UIViewMessage(
                id=entry.id,
                role=entry.role.value,
                content=entry.content,
                status=entry.metadata.get("status", "complete"),
                metadata=_public_ui_metadata(entry.metadata),
            )
        )
    return messages


def _filter_llm_history(entries: list[SessionEntry]) -> list[SessionEntry]:
    result: list[SessionEntry] = []
    for entry in entries:
        if entry.metadata.get("visibility") in {"ui", "observability"}:
            continue
        if entry.metadata.get("status") in {"stream_failed", "interrupted"}:
            continue
        if entry.role not in {MessageRole.USER, MessageRole.ASSISTANT, MessageRole.TOOL}:
            continue
        result.append(entry)
    return result


def _compress_tool_error_history(entries: list[SessionEntry], max_groups: int = 3) -> list[SessionEntry]:
    if max_groups <= 0:
        return entries

    tool_group_counts: dict[str, int] = {}
    skip_indices: set[int] = set()

    index = 0
    while index < len(entries):
        entry = entries[index]
        if entry.role == MessageRole.ASSISTANT and entry.tool_calls:
            if index + 1 < len(entries) and entries[index + 1].role == MessageRole.TOOL:
                next_entry = entries[index + 1]
                is_error = next_entry.metadata.get("is_error", False)
                tool_id = next_entry.metadata.get("tool_id", "")
                result_text = next_entry.content
                if is_error and result_text and (
                    result_text.startswith("[Tool Input Validation Failed]")
                    or result_text.startswith("[Tool Unavailable]")
                ):
                    tool_group_counts[tool_id] = tool_group_counts.get(tool_id, 0) + 1
                    if tool_group_counts[tool_id] > max_groups:
                        skip_indices.add(index)
                        skip_indices.add(index + 1)
                    index += 1
        index += 1

    return [entry for idx, entry in enumerate(entries) if idx not in skip_indices]


def _resolve_summary_block(
    snapshot: AgentSnapshot,
    compaction_plan: CompactionPlan | None,
) -> str | None:
    if compaction_plan and compaction_plan.summary_block:
        return compaction_plan.summary_block
    if not snapshot.latest_compact_summary:
        return None
    return render_summary_text_block(
        snapshot.latest_compact_summary,
        compact_mode=snapshot.latest_compact_mode or "auto_prefix",
        scope=snapshot.latest_compact_scope or "full",
    )


def _filter_compacted_history(
    entries: list[SessionEntry],
    snapshot: AgentSnapshot,
    compaction_plan: CompactionPlan | None,
) -> list[SessionEntry]:
    retained_ids = set()
    compact_event_seq: int | None = None

    if compaction_plan is not None:
        retained_ids.update(compaction_plan.retained_entry_ids)
        compact_event_seq = compaction_plan.compact_event_seq

    if not retained_ids:
        retained_ids.update(snapshot.latest_compact_retained_entry_ids)
    if compact_event_seq is None:
        compact_event_seq = snapshot.latest_compact_event_seq

    if not retained_ids and compact_event_seq is None:
        return entries

    result: list[SessionEntry] = []
    for entry in entries:
        if entry.id in retained_ids:
            result.append(entry)
            continue
        event_seq = entry.metadata.get("source_event_seq")
        if compact_event_seq is not None and event_seq is not None:
            try:
                if int(event_seq) > compact_event_seq:
                    result.append(entry)
            except (TypeError, ValueError):
                continue
    return result


def _history_to_chat_messages(entries: list[SessionEntry]) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    for entry in entries:
        if entry.role == MessageRole.ASSISTANT and not entry.content.strip() and not entry.tool_calls:
            continue
        if entry.role == MessageRole.TOOL:
            messages.append(
                ChatMessage(
                    role=MessageRole.TOOL,
                    content=entry.content,
                    tool_call_id=entry.metadata.get("tool_call_id", ""),
                )
            )
            continue
        if entry.tool_calls:
            messages.append(
                ChatMessage(
                    role=entry.role,
                    content=entry.content,
                    tool_calls=[
                        {
                            "id": tool_call.call_id or "",
                            "type": "function",
                            "function": {
                                "name": tool_call.tool_id,
                                "arguments": (
                                    json.dumps(tool_call.arguments, ensure_ascii=False)
                                    if tool_call.arguments
                                    else "{}"
                                ),
                            },
                        }
                        for tool_call in entry.tool_calls
                    ],
                    reasoning_content=entry.metadata.get("reasoning_content") or "",
                )
            )
            continue
        messages.append(ChatMessage(role=entry.role, content=entry.content))
    return messages


def _public_ui_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "mode",
        "alignment",
        "persona_key",
        "persona_name",
        "persona_role",
        "turn_usage",
        "usage",
    }
    return {key: value for key, value in metadata.items() if key in allowed}
