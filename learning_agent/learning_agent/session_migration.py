from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from learning_agent.ai import LearningSession, MessageRole, SessionEntry
from learning_agent.ai.file_store import FileStore
from learning_agent.learning_agent.session_event_store import SessionEventStore
from learning_agent.learning_agent.session_events import SessionEventType
from learning_agent.learning_agent.session_projection import project_legacy_session

logger = logging.getLogger(__name__)


@dataclass
class MigrationReport:
    migrated: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)


def migrate_legacy_session(session_id: str, file_store: FileStore) -> bool:
    if file_store.read_session_events(session_id):
        return False

    session = _load_legacy_session_with_deltas(session_id, file_store)
    if session is None:
        return False

    event_store = SessionEventStore(file_store)
    snapshot = project_legacy_session(session)
    event_store.append_event(
        session.id,
        SessionEventType.SESSION_CREATED,
        {
            "objective_id": session.objective_id,
            "title": session.title,
            "mode": session.mode.value,
            "status": session.status.value,
            "mode_metadata": dict(session.mode_metadata),
            "created_at": session.created_at.isoformat(),
            "last_accessed_at": session.last_accessed_at.isoformat(),
        },
        visibility="system",
    )
    for entry in snapshot.messages:
        event_store.append_event(
            session.id,
            _event_type_for_entry(entry),
            {"entry": entry.model_dump(mode="json")},
            visibility="agent",
        )

    file_store.delete(f"sessions/{session_id}.json")
    file_store.delete(f"sessions/{session_id}.jsonl")
    return True


def migrate_all_sessions(file_store: FileStore) -> MigrationReport:
    report = MigrationReport()
    for session_id in file_store.list_legacy_sessions():
        try:
            if file_store.read_session_events(session_id):
                report.skipped[session_id] = "events_exist"
                continue
            if migrate_legacy_session(session_id, file_store):
                report.migrated.append(session_id)
            else:
                report.skipped[session_id] = "no_legacy_snapshot"
        except Exception as exc:
            file_store.delete_session_events(session_id)
            report.failed[session_id] = str(exc)
            logger.exception("[SessionMigration] Failed to migrate session %s", session_id)
    return report


def _load_legacy_session_with_deltas(
    session_id: str,
    file_store: FileStore,
) -> LearningSession | None:
    snapshot = file_store.legacy_load_session(session_id)
    if not snapshot:
        return None

    session = LearningSession(**snapshot)
    for delta in file_store.legacy_read_session_deltas(session_id):
        _apply_legacy_delta(session, delta)

    message_entries = [entry for entry in session.entries if entry.role is not None]
    if message_entries:
        session.current_leaf_id = message_entries[-1].id
    return session


def _apply_legacy_delta(session: LearningSession, delta: dict[str, Any]) -> None:
    op = delta.get("op")
    if op == "append":
        session.entries.append(SessionEntry(**delta["entry"]))
        return
    if op == "patch":
        entry_id = delta.get("entry_id")
        entry = next((item for item in session.entries if item.id == entry_id), None)
        if entry is not None:
            _apply_patch(entry, str(delta.get("path", "")), delta.get("value"))
        return
    if op == "scalar":
        path = str(delta.get("path", ""))
        value = delta.get("value")
        if hasattr(session, path):
            setattr(session, path, value)
        elif "." in path:
            obj_name, attr_name = path.split(".", 1)
            obj = getattr(session, obj_name, None)
            if isinstance(obj, dict):
                obj[attr_name] = value
        session.last_accessed_at = datetime.now(timezone.utc)


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


def _event_type_for_entry(entry: SessionEntry) -> str:
    if entry.role == MessageRole.USER:
        return SessionEventType.MESSAGE_USER_APPENDED
    if entry.role == MessageRole.TOOL:
        return (
            SessionEventType.TOOL_CALL_FAILED
            if entry.metadata.get("is_error")
            else SessionEventType.TOOL_CALL_COMPLETED
        )
    return SessionEventType.MESSAGE_END
