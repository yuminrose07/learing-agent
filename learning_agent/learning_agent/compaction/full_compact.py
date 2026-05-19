from __future__ import annotations

from collections import OrderedDict
from typing import Optional

from learning_agent.ai import MessageRole, SessionEntry

from .models import (
    CompactMetadata,
    CompactSourceUnit,
    CompactTraceSummary,
    FullCompactInput,
    FullCompactResult,
    SessionMemoryState,
)


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def build_round_units(entries: list[SessionEntry]) -> list[CompactSourceUnit]:
    units: list[CompactSourceUnit] = []
    bucket: list[SessionEntry] = []

    for entry in entries:
        bucket.append(entry)
        if entry.role == MessageRole.ASSISTANT and not entry.tool_calls:
            units.append(_build_unit(bucket, len(units)))
            bucket = []

    if bucket:
        units.append(_build_unit(bucket, len(units)))
    return units


def _build_unit(entries: list[SessionEntry], index: int) -> CompactSourceUnit:
    transcript = render_role_transcript(entries)
    return CompactSourceUnit(
        unit_id=f"round-{index}",
        entry_ids=[entry.id for entry in entries],
        transcript=transcript,
        estimated_tokens=estimate_text_tokens(transcript),
        started_at=entries[0].timestamp.isoformat() if entries else None,
        ended_at=entries[-1].timestamp.isoformat() if entries else None,
    )


def find_cut_point(
    units: list[CompactSourceUnit],
    recent_token_budget: int,
) -> str | None:
    if not units:
        return None

    preserved_tokens = 0
    preserved_units: list[CompactSourceUnit] = []
    for unit in reversed(units):
        preserved_units.append(unit)
        preserved_tokens += unit.estimated_tokens
        if preserved_tokens >= recent_token_budget:
            break

    preserved_units.reverse()
    if len(preserved_units) >= len(units):
        return None
    first_preserved = preserved_units[0]
    return first_preserved.entry_ids[0] if first_preserved.entry_ids else None


def select_compact_scope(
    metadata: CompactMetadata | None,
    *,
    max_incrementals_before_rebase: int = 5,
) -> str:
    if metadata is None or metadata.last_cut_point_entry_id is None:
        return "full"
    if metadata.incremental_count_since_rebase >= max_incrementals_before_rebase:
        return "rebase"
    return "incremental"


def render_role_transcript(entries: list[SessionEntry]) -> str:
    lines: list[str] = []
    for entry in entries:
        role = (entry.role.value if entry.role else "system").upper()
        content = (entry.content or "").strip()
        if entry.tool_calls:
            tool_names = ", ".join(tool_call.tool_id for tool_call in entry.tool_calls if tool_call.tool_id)
            header = f"{role}: {content}" if content else f"{role}:"
            lines.append(header)
            lines.append(f"TOOL_CALLS: {tool_names}")
            continue
        lines.append(f"{role}: {content}" if content else f"{role}:")
    return "\n".join(lines).strip()


def build_full_compact_input(
    session_id: str,
    entries: list[SessionEntry],
    *,
    metadata: CompactMetadata | None,
    recent_token_budget: int,
    existing_summary: str | None = None,
    sm_state: SessionMemoryState | None = None,
) -> FullCompactInput | None:
    anchor_entry_id = metadata.compact_anchor_entry_id if metadata else None
    scope = select_compact_scope(metadata)

    eligible_entries = list_entries_after(entries, anchor_entry_id)
    source_units = build_round_units(eligible_entries)
    cut_point_entry_id = find_cut_point(source_units, recent_token_budget)
    if not cut_point_entry_id:
        return None

    compact_units = []
    for unit in source_units:
        if cut_point_entry_id in unit.entry_ids:
            break
        compact_units.append(unit)

    if not compact_units:
        return None

    return FullCompactInput(
        session_id=session_id,
        scope=scope,
        compact_anchor_entry_id=anchor_entry_id,
        cut_point_entry_id=cut_point_entry_id,
        recent_token_budget=recent_token_budget,
        source_units=compact_units,
        existing_summary=existing_summary,
        sm_state=sm_state,
    )


def list_entries_after(entries: list[SessionEntry], entry_id: str | None) -> list[SessionEntry]:
    if entry_id is None:
        return list(entries)
    for index, entry in enumerate(entries):
        if entry.id == entry_id:
            return entries[index + 1 :]
    return list(entries)


def merge_incremental_summary(existing_summary: str | None, delta_summary: str) -> str:
    if not existing_summary:
        return delta_summary
    return f"{existing_summary.rstrip()}\n\n[Incremental Update]\n{delta_summary.strip()}"


def build_trace_summary(entries: list[SessionEntry]) -> CompactTraceSummary:
    trace = CompactTraceSummary()
    read_tools = {"read_file"}
    write_tools = {"write_file", "edit_file"}
    search_tools = {"grep"}

    for entry in entries:
        if entry.role != MessageRole.TOOL:
            continue
        tool_name = entry.metadata.get("tool_id") or "unknown"
        label = f"{tool_name}:{entry.metadata.get('tool_call_id', '')}".rstrip(":")
        if tool_name in read_tools:
            trace.reads.append(label)
        elif tool_name in write_tools:
            trace.writes.append(label)
        elif tool_name in search_tools:
            trace.searches.append(label)
        if entry.metadata.get("is_error"):
            trace.failures.append(label)

    trace.reads = list(OrderedDict.fromkeys(trace.reads))
    trace.writes = list(OrderedDict.fromkeys(trace.writes))
    trace.searches = list(OrderedDict.fromkeys(trace.searches))
    trace.failures = list(OrderedDict.fromkeys(trace.failures))
    return trace


def build_full_compact_result(
    compact_input: FullCompactInput,
    *,
    summary_text: str,
    preserved_entry_ids: list[str],
    estimated_tokens_after: int,
) -> FullCompactResult:
    compacted_entry_ids = [entry_id for unit in compact_input.source_units for entry_id in unit.entry_ids]
    trace_summary = build_trace_summary_from_units(compact_input.source_units)
    return FullCompactResult(
        session_id=compact_input.session_id,
        scope=compact_input.scope,
        summary_text=summary_text,
        compact_anchor_entry_id=compact_input.compact_anchor_entry_id,
        cut_point_entry_id=compact_input.cut_point_entry_id,
        next_anchor_entry_id=compacted_entry_ids[-1] if compacted_entry_ids else compact_input.compact_anchor_entry_id,
        preserved_entry_ids=preserved_entry_ids,
        trace_summary=trace_summary,
        estimated_tokens_after=estimated_tokens_after,
    )


def build_trace_summary_from_units(units: list[CompactSourceUnit]) -> CompactTraceSummary:
    trace = CompactTraceSummary()
    for unit in units:
        transcript = unit.transcript
        if "TOOL_CALLS: read_file" in transcript:
            trace.reads.append("read_file")
        if "TOOL_CALLS: write_file" in transcript or "TOOL_CALLS: edit_file" in transcript:
            trace.writes.append("write_or_edit")
        if "TOOL_CALLS: grep" in transcript:
            trace.searches.append("grep")
    trace.reads = list(OrderedDict.fromkeys(trace.reads))
    trace.writes = list(OrderedDict.fromkeys(trace.writes))
    trace.searches = list(OrderedDict.fromkeys(trace.searches))
    return trace


def render_summary_block(result: FullCompactResult) -> str:
    trace = result.trace_summary
    lines = [
        f"[Compact Summary | scope={result.scope}]",
        result.summary_text.strip(),
        "",
        f"reads: {', '.join(trace.reads) if trace.reads else 'none'}",
        f"writes: {', '.join(trace.writes) if trace.writes else 'none'}",
        f"searches: {', '.join(trace.searches) if trace.searches else 'none'}",
        f"failures: {', '.join(trace.failures) if trace.failures else 'none'}",
    ]
    return "\n".join(lines).strip()
