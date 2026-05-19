from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from learning_agent.ai import (
    AgentMode,
    AskState,
    EntryType,
    LearningSession,
    MessageRole,
    SessionEntry,
    SessionStatus,
)
from learning_agent.learning_agent.compaction.models import CompactMetadata
from learning_agent.learning_agent.session_events import SessionEvent, SessionEventType

logger = logging.getLogger(__name__)


@dataclass
class AgentSnapshot:
    session_id: str
    objective_id: str | None = None
    title: str | None = None
    status: SessionStatus = SessionStatus.ACTIVE
    mode: AgentMode = AgentMode.CHAT
    ask_state: AskState = field(default_factory=AskState)
    mode_metadata: dict[str, Any] = field(default_factory=dict)
    messages: list[SessionEntry] = field(default_factory=list)
    compact_metadata: CompactMetadata | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_accessed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source_event_range: tuple[int, int] | None = None
    corrupt_events: list[dict[str, Any]] = field(default_factory=list)

    def to_learning_session(self) -> LearningSession:
        last_entry_id = self.messages[-1].id if self.messages else None
        return LearningSession(
            id=self.session_id,
            objective_id=self.objective_id,
            title=self.title,
            root_entry_id=None,
            current_leaf_id=last_entry_id,
            status=self.status,
            mode=self.mode,
            mode_metadata=dict(self.mode_metadata),
            created_at=self.created_at,
            last_accessed_at=self.last_accessed_at,
            entries=[entry.model_copy(deep=True) for entry in self.messages],
            ask_state=self.ask_state.model_copy(deep=True),
        )


def replay_events(events: list[SessionEvent], session_id: str | None = None) -> AgentSnapshot:
    sid = session_id or (events[0].session_id if events else "")
    snapshot = AgentSnapshot(
        session_id=sid,
        compact_metadata=CompactMetadata(session_id=sid) if sid else None,
    )
    seen_event_ids: set[str] = set()
    last_seq = 0
    first_seq: int | None = None
    entry_index: dict[str, SessionEntry] = {}

    for event in events:
        if event.event_id in seen_event_ids:
            continue
        if last_seq and event.seq != last_seq + 1:
            snapshot.corrupt_events.append(
                {
                    "event_id": event.event_id,
                    "seq": event.seq,
                    "expected_seq": last_seq + 1,
                    "reason": "seq_gap_or_out_of_order",
                }
            )
            break
        seen_event_ids.add(event.event_id)
        last_seq = event.seq
        first_seq = event.seq if first_seq is None else first_seq
        snapshot.last_accessed_at = event.ts

        try:
            _apply_event(snapshot, entry_index, event)
        except Exception:
            snapshot.corrupt_events.append(
                {
                    "event_id": event.event_id,
                    "seq": event.seq,
                    "reason": "apply_failed",
                    "type": event.type,
                }
            )
            logger.exception("[SessionProjection] Failed to replay event %s", event.event_id)
            break

    if first_seq is not None:
        snapshot.source_event_range = (first_seq, last_seq)
    return snapshot


def project_legacy_session(session: LearningSession) -> AgentSnapshot:
    messages = _linearize_legacy_entries(session)
    return AgentSnapshot(
        session_id=session.id,
        objective_id=session.objective_id,
        title=session.title,
        status=session.status,
        mode=session.mode,
        ask_state=session.ask_state.model_copy(deep=True),
        mode_metadata=dict(session.mode_metadata),
        messages=messages,
        compact_metadata=CompactMetadata(session_id=session.id),
        created_at=session.created_at,
        last_accessed_at=session.last_accessed_at,
    )


def _apply_event(
    snapshot: AgentSnapshot,
    entry_index: dict[str, SessionEntry],
    event: SessionEvent,
) -> None:
    payload = event.payload

    if event.type == SessionEventType.SESSION_CREATED:
        snapshot.session_id = event.session_id
        snapshot.objective_id = payload.get("objective_id")
        snapshot.title = payload.get("title")
        if payload.get("mode"):
            snapshot.mode = AgentMode(payload["mode"])
        if payload.get("status"):
            snapshot.status = SessionStatus(payload["status"])
        if isinstance(payload.get("mode_metadata"), dict):
            snapshot.mode_metadata = dict(payload["mode_metadata"])
        if isinstance(payload.get("ask_state"), dict):
            snapshot.ask_state = AskState.model_validate(payload["ask_state"])
        snapshot.created_at = _parse_datetime(payload.get("created_at")) or event.ts
        snapshot.last_accessed_at = _parse_datetime(payload.get("last_accessed_at")) or event.ts
        return

    if event.type in {
        SessionEventType.MESSAGE_USER_APPENDED,
        SessionEventType.MESSAGE_END,
        SessionEventType.TOOL_CALL_COMPLETED,
        SessionEventType.TOOL_CALL_FAILED,
    }:
        entry = _entry_from_payload(payload, event)
        if entry.id in entry_index:
            return
        snapshot.messages.append(entry)
        entry_index[entry.id] = entry
        return

    if event.type == SessionEventType.MESSAGE_PATCH:
        entry_id = payload.get("entry_id")
        entry = entry_index.get(entry_id)
        if entry is not None:
            _apply_patch(entry, str(payload.get("path", "")), payload.get("value"))
        return

    if event.type == SessionEventType.SESSION_MODE_CHANGED:
        mode_value = payload.get("to") or payload.get("mode")
        if mode_value:
            snapshot.mode = AgentMode(mode_value)
        if isinstance(payload.get("mode_metadata"), dict):
            snapshot.mode_metadata.update(payload["mode_metadata"])
        return

    if event.type == SessionEventType.SESSION_ASK_STATE_UPDATED:
        if isinstance(payload.get("ask_state"), dict):
            snapshot.ask_state = AskState.model_validate(payload["ask_state"])
        return

    if event.type == SessionEventType.SESSION_TITLE_UPDATED:
        snapshot.title = payload.get("title")
        return

    if event.type == SessionEventType.SESSION_STATUS_CHANGED:
        status_value = payload.get("status")
        if status_value:
            snapshot.status = SessionStatus(status_value)
        return

    if event.type == SessionEventType.COMPACTION_SUMMARY_ADDED:
        metadata = snapshot.compact_metadata or CompactMetadata(session_id=event.session_id)
        metadata.last_summary_file = payload.get("summary_path") or metadata.last_summary_file
        metadata.last_summary_hash = payload.get("summary_hash") or metadata.last_summary_hash
        metadata.last_cut_point_entry_id = payload.get("cut_point_entry_id") or metadata.last_cut_point_entry_id
        metadata.compact_anchor_entry_id = payload.get("anchor_entry_id") or metadata.compact_anchor_entry_id
        metadata.last_compact_scope = payload.get("scope") or metadata.last_compact_scope
        if payload.get("source_event_start_seq") is not None:
            metadata.source_event_start_seq = payload.get("source_event_start_seq")
        if payload.get("source_event_end_seq") is not None:
            metadata.source_event_end_seq = payload.get("source_event_end_seq")
        snapshot.compact_metadata = metadata
        snapshot.mode_metadata["compaction"] = {
            "scope": metadata.last_compact_scope,
            "cut_point_entry_id": metadata.last_cut_point_entry_id,
            "anchor_entry_id": metadata.compact_anchor_entry_id,
            "summary_hash": metadata.last_summary_hash,
        }


def _entry_from_payload(payload: dict[str, Any], event: SessionEvent) -> SessionEntry:
    raw_entry = payload.get("entry")
    if isinstance(raw_entry, dict):
        entry = SessionEntry.model_validate(raw_entry)
    else:
        entry_kwargs: dict[str, Any] = {
            "parent_id": None,
            "type": EntryType.MESSAGE,
            "role": MessageRole(payload["role"]) if payload.get("role") else None,
            "content": payload.get("content", ""),
            "metadata": dict(payload.get("metadata") or {}),
        }
        if payload.get("entry_id"):
            entry_kwargs["id"] = payload["entry_id"]
        entry = SessionEntry(
            **entry_kwargs,
        )
    entry.parent_id = None
    entry.metadata = dict(entry.metadata)
    entry.metadata.setdefault("source_event_seq", event.seq)
    entry.metadata.setdefault("source_event_id", event.event_id)
    return entry


def _linearize_legacy_entries(session: LearningSession) -> list[SessionEntry]:
    entries = list(session.entries)
    if session.current_leaf_id and any(entry.parent_id for entry in entries):
        entry_map = {entry.id: entry for entry in entries}
        path: list[SessionEntry] = []
        current = session.current_leaf_id
        while current and current in entry_map:
            entry = entry_map[current]
            path.append(entry)
            current = entry.parent_id
        if path:
            entries = list(reversed(path))

    messages: list[SessionEntry] = []
    for entry in entries:
        if entry.type == EntryType.FORK_POINT:
            continue
        if entry.role is None:
            continue
        clean_entry = entry.model_copy(deep=True)
        clean_entry.parent_id = None
        messages.append(clean_entry)
    return messages


def _apply_patch(entry: SessionEntry, path: str, value: Any) -> None:
    if not path:
        return
    parts = path.split(".")
    current: Any = entry
    for part in parts[:-1]:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            current = current[int(part)]
        else:
            current = getattr(current, part, None)
        if current is None:
            return

    last = parts[-1]
    if isinstance(current, dict):
        current[last] = value
    elif isinstance(current, list):
        current[int(last)] = value
    else:
        setattr(current, last, value)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
