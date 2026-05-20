from __future__ import annotations

from collections import OrderedDict
import re
from typing import Optional

from learning_agent.ai import MessageRole, SessionEntry

from .models import (
    CompactMode,
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
    event_ids = [
        str(entry.metadata.get("source_event_id"))
        for entry in entries
        if entry.metadata.get("source_event_id")
    ]
    event_seqs = [
        int(entry.metadata["source_event_seq"])
        for entry in entries
        if entry.metadata.get("source_event_seq") is not None
    ]
    unit_type = "tool_interaction" if any(entry.tool_calls or entry.role == MessageRole.TOOL for entry in entries) else "message_round"
    return CompactSourceUnit(
        unit_id=f"round-{index}",
        unit_type=unit_type,
        entry_ids=[entry.id for entry in entries],
        event_ids=event_ids,
        source_event_start_seq=min(event_seqs) if event_seqs else None,
        source_event_end_seq=max(event_seqs) if event_seqs else None,
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
        if entry.role == MessageRole.TOOL and entry.metadata.get("sensitive"):
            content = "[Tool output omitted from compact source]"
        else:
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
    compact_mode: str = CompactMode.AUTO_PREFIX.value,
    current_user_event_id: str | None = None,
    source_snapshot_seq: int | None = None,
    existing_summary: str | None = None,
    sm_state: SessionMemoryState | None = None,
) -> FullCompactInput | None:
    anchor_entry_id = metadata.compact_anchor_entry_id if metadata else None
    scope = select_compact_scope(metadata)

    eligible_entries = _filter_entries_for_compact_source(
        list_entries_after(entries, anchor_entry_id),
        source_snapshot_seq=source_snapshot_seq,
        current_user_event_id=current_user_event_id,
    )
    source_units = build_round_units(eligible_entries)
    cut_point_entry_id = find_cut_point(source_units, recent_token_budget)
    if not cut_point_entry_id:
        return None

    compact_units = []
    retained_units = []
    found_cut_point = False
    for unit in source_units:
        if cut_point_entry_id in unit.entry_ids:
            found_cut_point = True
        if found_cut_point:
            retained_units.append(unit)
        else:
            compact_units.append(unit)

    if not compact_units:
        return None

    source_event_ids = [event_id for unit in compact_units for event_id in unit.event_ids]
    source_entry_ids = [entry_id for unit in compact_units for entry_id in unit.entry_ids]
    retained_entry_ids = [entry_id for unit in retained_units for entry_id in unit.entry_ids]
    source_starts = [
        unit.source_event_start_seq
        for unit in compact_units
        if unit.source_event_start_seq is not None
    ]
    source_ends = [
        unit.source_event_end_seq
        for unit in compact_units
        if unit.source_event_end_seq is not None
    ]
    source_end_seq = max(source_ends) if source_ends else None

    return FullCompactInput(
        session_id=session_id,
        compact_mode=compact_mode,
        scope=scope,
        compact_anchor_entry_id=anchor_entry_id,
        cut_point_entry_id=cut_point_entry_id,
        recent_token_budget=recent_token_budget,
        summary_position="before_retained",
        source_event_start_seq=min(source_starts) if source_starts else None,
        source_event_end_seq=source_end_seq,
        source_snapshot_seq=source_snapshot_seq if source_snapshot_seq is not None else source_end_seq,
        source_event_ids=source_event_ids,
        source_entry_ids=source_entry_ids,
        retained_entry_ids=retained_entry_ids,
        source_units=compact_units,
        existing_summary=existing_summary,
        sm_state=sm_state,
        previous_compact_event_id=metadata.last_compact_event_id if metadata else None,
        current_user_event_id=current_user_event_id,
    )


def list_entries_after(entries: list[SessionEntry], entry_id: str | None) -> list[SessionEntry]:
    if entry_id is None:
        return list(entries)
    for index, entry in enumerate(entries):
        if entry.id == entry_id:
            return entries[index + 1 :]
    return list(entries)


def list_entries_from(entries: list[SessionEntry], entry_id: str | None) -> list[SessionEntry]:
    if entry_id is None:
        return list(entries)
    for index, entry in enumerate(entries):
        if entry.id == entry_id:
            return entries[index:]
    return list(entries)


def _filter_entries_for_compact_source(
    entries: list[SessionEntry],
    *,
    source_snapshot_seq: int | None,
    current_user_event_id: str | None,
) -> list[SessionEntry]:
    filtered: list[SessionEntry] = []
    for entry in entries:
        if current_user_event_id and entry.metadata.get("source_event_id") == current_user_event_id:
            continue
        event_seq = entry.metadata.get("source_event_seq")
        if source_snapshot_seq is not None and event_seq is not None:
            try:
                if int(event_seq) > source_snapshot_seq:
                    continue
            except (TypeError, ValueError):
                continue
        if entry.metadata.get("status") in {"stream_failed", "interrupted"}:
            continue
        filtered.append(entry)
    return filtered


def merge_incremental_summary(existing_summary: str | None, delta_summary: str) -> str:
    if not existing_summary:
        return delta_summary.strip()
    if not delta_summary:
        return existing_summary.strip()

    existing_sections = _parse_summary_sections(existing_summary)
    delta_sections = _parse_summary_sections(delta_summary)
    merged_titles: list[str] = []
    for title in [*existing_sections.keys(), *delta_sections.keys()]:
        if title not in merged_titles:
            merged_titles.append(title)

    merged_parts: list[str] = []
    for title in merged_titles:
        existing_body = existing_sections.get(title, "").strip()
        delta_body = delta_sections.get(title, "").strip()
        if existing_body and delta_body:
            body = _merge_section_bodies(existing_body, delta_body)
        else:
            body = delta_body or existing_body
        merged_parts.append(f"{title}:\n{body}".strip())
    return "\n\n".join(part for part in merged_parts if part).strip()


def _parse_summary_sections(summary_text: str) -> OrderedDict[str, str]:
    sections: OrderedDict[str, str] = OrderedDict()
    matches = list(re.finditer(r"(?m)^(\d+\.\s.+?):\s*$", summary_text.strip()))
    if not matches:
        return OrderedDict({"1. Summary": summary_text.strip()})

    for index, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(summary_text)
        body = summary_text[start:end].strip()
        sections[title] = body
    return sections


def _merge_section_bodies(existing_body: str, delta_body: str) -> str:
    chunks = [* _split_section_chunks(existing_body), * _split_section_chunks(delta_body)]
    deduped = list(OrderedDict.fromkeys(chunk for chunk in chunks if chunk))
    return "\n\n".join(deduped).strip()


def _split_section_chunks(body: str) -> list[str]:
    if not body.strip():
        return []
    return [chunk.strip() for chunk in re.split(r"\n\s*\n", body.strip()) if chunk.strip()]


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
        compact_mode=compact_input.compact_mode,
        scope=compact_input.scope,
        summary_text=summary_text,
        compact_anchor_entry_id=compact_input.compact_anchor_entry_id,
        cut_point_entry_id=compact_input.cut_point_entry_id,
        next_anchor_entry_id=compacted_entry_ids[-1] if compacted_entry_ids else compact_input.compact_anchor_entry_id,
        previous_compact_event_id=compact_input.previous_compact_event_id,
        current_user_event_id=compact_input.current_user_event_id,
        source_event_start_seq=compact_input.source_event_start_seq,
        source_event_end_seq=compact_input.source_event_end_seq,
        source_snapshot_seq=compact_input.source_snapshot_seq,
        source_event_ids=list(compact_input.source_event_ids),
        source_entry_ids=list(compact_input.source_entry_ids),
        retained_event_ids=[],
        retained_entry_ids=list(compact_input.retained_entry_ids),
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


def render_summary_text_block(
    summary_text: str,
    *,
    compact_mode: str,
    scope: str,
    trace: CompactTraceSummary | None = None,
) -> str:
    trace = trace or CompactTraceSummary()
    lines = [
        "[Compact Summary]",
        "This session is being continued after context compaction.",
        "The summary below covers earlier conversation content.",
        "Recent messages may be preserved verbatim after this block.",
        "Do not acknowledge this summary to the user.",
        "Continue naturally from the latest user-visible request.",
    ]
    if compact_mode == CompactMode.AUTO_PREFIX.value:
        lines.extend(
            [
                "This compact summary was inserted automatically.",
                "Do not ask follow-up questions just because this summary exists.",
                "Resume the current task directly.",
            ]
        )
    lines.extend(
        [
        "",
        f"mode: {compact_mode}",
        f"scope: {scope}",
        summary_text.strip(),
        "",
        f"reads: {', '.join(trace.reads) if trace.reads else 'none'}",
        f"writes: {', '.join(trace.writes) if trace.writes else 'none'}",
        f"searches: {', '.join(trace.searches) if trace.searches else 'none'}",
        f"failures: {', '.join(trace.failures) if trace.failures else 'none'}",
        ]
    )
    return "\n".join(lines).strip()


def render_summary_block(result: FullCompactResult) -> str:
    return render_summary_text_block(
        result.summary_text,
        compact_mode=result.compact_mode,
        scope=result.scope,
        trace=result.trace_summary,
    )
