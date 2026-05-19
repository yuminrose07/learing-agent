from __future__ import annotations

from collections.abc import Iterable

from learning_agent.ai import MessageRole, SessionEntry


def is_micro_compactable_tool_entry(entry: SessionEntry) -> bool:
    return entry.role == MessageRole.TOOL or bool(entry.tool_calls)


def group_tool_result_units(entries: Iterable[SessionEntry]) -> list[list[SessionEntry]]:
    units: list[list[SessionEntry]] = []
    current: list[SessionEntry] = []

    for entry in entries:
        if entry.role == MessageRole.ASSISTANT and entry.tool_calls:
            if current:
                units.append(current)
            current = [entry]
            continue
        if current and entry.role == MessageRole.TOOL:
            current.append(entry)
            continue
        if current:
            units.append(current)
            current = []

    if current:
        units.append(current)
    return units


def _build_compacted_assistant_entry(entry: SessionEntry) -> SessionEntry | None:
    if entry.role != MessageRole.ASSISTANT or not entry.tool_calls:
        return entry

    tool_names = [tool_call.tool_id for tool_call in entry.tool_calls if tool_call.tool_id]
    compacted_content = (entry.content or "").strip()
    prefix = f"[Earlier tool interaction compacted: {', '.join(tool_names)}]"
    if compacted_content:
        compacted_content = f"{compacted_content}\n\n{prefix}"
    else:
        compacted_content = prefix

    compacted_entry = entry.model_copy(deep=True)
    compacted_entry.content = compacted_content
    compacted_entry.tool_calls = []
    compacted_entry.metadata = {
        **compacted_entry.metadata,
        "micro_compacted": True,
        "compacted_tool_names": tool_names,
    }
    return compacted_entry


def build_micro_compacted_history(
    entries: list[SessionEntry],
    keep_recent_groups: int = 2,
    mutate_storage: bool = False,
) -> list[SessionEntry]:
    del mutate_storage
    if keep_recent_groups < 0:
        keep_recent_groups = 0

    grouped_units = group_tool_result_units(entries)
    if not grouped_units:
        return list(entries)

    recent_groups = grouped_units[-keep_recent_groups:] if keep_recent_groups else []
    recent_ids = {entry.id for group in recent_groups for entry in group}

    compacted_history: list[SessionEntry] = []
    for entry in entries:
        if entry.id in recent_ids:
            compacted_history.append(entry)
            continue

        if entry.role == MessageRole.TOOL:
            continue

        if entry.role == MessageRole.ASSISTANT and entry.tool_calls:
            compacted_entry = _build_compacted_assistant_entry(entry)
            if compacted_entry is not None:
                compacted_history.append(compacted_entry)
            continue

        compacted_history.append(entry)

    return compacted_history
