from __future__ import annotations

import logging
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from learning_agent.ai import (
    AgentMode,
    EntryType,
    LearningSession,
    MessageRole,
    SessionEntry,
    SessionStatus,
)
from learning_agent.learning_agent.compaction.models import CompactMetadata
from learning_agent.learning_agent.session_events import EventVisibility, SessionEvent, SessionEventType

logger = logging.getLogger(__name__)


@dataclass
class AgentSnapshot:
    session_id: str
    objective_id: str | None = None
    learning_unit_id: str | None = None
    title: str | None = None
    status: SessionStatus = SessionStatus.ACTIVE
    mode: AgentMode = AgentMode.CHAT
    mode_metadata: dict[str, Any] = field(default_factory=dict)
    messages: list[SessionEntry] = field(default_factory=list)
    compact_metadata: CompactMetadata | None = None
    latest_compact_summary: str | None = None
    latest_compact_event_id: str | None = None
    latest_compact_event_seq: int | None = None
    latest_compact_mode: str | None = None
    latest_compact_scope: str | None = None
    latest_compact_source_entry_ids: list[str] = field(default_factory=list)
    latest_compact_retained_entry_ids: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_accessed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source_event_range: tuple[int, int] | None = None
    corrupt_events: list[dict[str, Any]] = field(default_factory=list)

    def to_learning_session(self) -> LearningSession:
        last_entry_id = self.messages[-1].id if self.messages else None
        return LearningSession(
            id=self.session_id,
            objective_id=self.objective_id,
            learning_unit_id=self.learning_unit_id,
            title=self.title,
            root_entry_id=None,
            current_leaf_id=last_entry_id,
            status=self.status,
            mode=self.mode,
            mode_metadata=dict(self.mode_metadata),
            created_at=self.created_at,
            last_accessed_at=self.last_accessed_at,
            entries=[entry.model_copy(deep=True) for entry in self.messages],
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

        # L1 事实源护栏：visibility=OBSERVABILITY 的事件是诊断/观测事件，
        # 与业务事件共存于同一 JSONL 时间轴，但不参与 state 重建。
        # 必须在 seq 检查之后跳过（保持 seq 连续性校验），在 _apply_event 之前跳过（避免污染 state）。
        # 测试护栏：tests/test_session_projection.py::test_replay_filters_business_event_types_marked_observability
        if event.visibility == EventVisibility.OBSERVABILITY:
            continue

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
        snapshot.learning_unit_id = payload.get("learning_unit_id")
        snapshot.title = payload.get("title")
        if payload.get("mode"):
            snapshot.mode = AgentMode(payload["mode"])
        if payload.get("status"):
            snapshot.status = SessionStatus(payload["status"])
        if isinstance(payload.get("mode_metadata"), dict):
            snapshot.mode_metadata = dict(payload["mode_metadata"])
        snapshot.created_at = _parse_datetime(payload.get("created_at")) or event.ts
        snapshot.last_accessed_at = _parse_datetime(payload.get("last_accessed_at")) or event.ts
        return

    if event.type == SessionEventType.SESSION_LEARNING_UNIT_BOUND:
        snapshot.learning_unit_id = payload.get("learning_unit_id")
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

    if event.type == SessionEventType.SESSION_TITLE_UPDATED:
        snapshot.title = payload.get("title")
        return

    if event.type == SessionEventType.SESSION_STATUS_CHANGED:
        status_value = payload.get("status")
        if status_value:
            snapshot.status = SessionStatus(status_value)
        return

    if event.type == SessionEventType.LEARNING_UNIT_ALIGNMENT_RATE_LIMITED:
        # Timeline-only product event. It documents a suggested→none downgrade
        # without changing the replayed session snapshot.
        return

    if event.type == SessionEventType.COMPACTION_SUMMARY_ADDED:
        metadata = snapshot.compact_metadata or CompactMetadata(session_id=event.session_id)
        summary_text = payload.get("summary_text")
        summary_hash = payload.get("summary_hash")
        if summary_text and summary_hash:
            actual_hash = hashlib.sha256(str(summary_text).encode("utf-8")).hexdigest()
            if actual_hash != summary_hash:
                snapshot.corrupt_events.append(
                    {
                        "event_id": event.event_id,
                        "seq": event.seq,
                        "reason": "summary_hash_mismatch",
                        "type": event.type,
                    }
                )
                return
        metadata.last_compact_event_id = event.event_id
        metadata.previous_compact_event_id = payload.get("previous_compact_event_id") or metadata.previous_compact_event_id
        metadata.last_summary_file = payload.get("summary_path") or metadata.last_summary_file
        metadata.last_summary_artifact_ref = payload.get("summary_artifact_ref") or metadata.last_summary_artifact_ref
        metadata.last_summary_hash = payload.get("summary_hash") or metadata.last_summary_hash
        metadata.last_cut_point_entry_id = payload.get("cut_point_entry_id") or metadata.last_cut_point_entry_id
        metadata.compact_anchor_entry_id = payload.get("anchor_entry_id") or metadata.compact_anchor_entry_id
        metadata.compact_anchor_event_seq = event.seq
        metadata.last_compact_mode = payload.get("mode") or payload.get("compact_mode") or metadata.last_compact_mode
        metadata.last_compact_scope = payload.get("scope") or metadata.last_compact_scope
        if payload.get("source_event_start_seq") is not None:
            metadata.source_event_start_seq = payload.get("source_event_start_seq")
        if payload.get("source_event_end_seq") is not None:
            metadata.source_event_end_seq = payload.get("source_event_end_seq")
        if payload.get("source_snapshot_seq") is not None:
            metadata.last_source_snapshot_seq = payload.get("source_snapshot_seq")
        if isinstance(payload.get("source_event_ids"), list):
            metadata.source_event_ids = list(payload["source_event_ids"])
        if isinstance(payload.get("retained_event_ids"), list):
            metadata.retained_event_ids = list(payload["retained_event_ids"])
        if isinstance(payload.get("source_entry_ids"), list):
            metadata.last_source_entry_ids = list(payload["source_entry_ids"])
        if isinstance(payload.get("retained_entry_ids"), list):
            metadata.last_retained_entry_ids = list(payload["retained_entry_ids"])
        metadata.next_jsonl_cursor = payload.get("next_jsonl_cursor") or metadata.next_jsonl_cursor
        metadata.last_source_jsonl_cursor = payload.get("source_jsonl_cursor") or metadata.last_source_jsonl_cursor
        metadata.template_version = payload.get("template_version") or metadata.template_version
        metadata.last_prompt_template = payload.get("template_version") or metadata.last_prompt_template
        snapshot.compact_metadata = metadata
        if summary_text:
            snapshot.latest_compact_summary = str(summary_text)
            snapshot.latest_compact_event_id = event.event_id
            snapshot.latest_compact_event_seq = event.seq
            snapshot.latest_compact_mode = metadata.last_compact_mode
            snapshot.latest_compact_scope = metadata.last_compact_scope
            snapshot.latest_compact_source_entry_ids = list(metadata.last_source_entry_ids)
            snapshot.latest_compact_retained_entry_ids = list(metadata.last_retained_entry_ids)
        snapshot.mode_metadata["compaction"] = {
            "compact_event_id": metadata.last_compact_event_id,
            "mode": metadata.last_compact_mode,
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
