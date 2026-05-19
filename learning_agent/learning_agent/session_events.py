from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class SessionEventType:
    SESSION_CREATED = "session.created"
    MESSAGE_USER_APPENDED = "message.user_appended"
    MESSAGE_ASSISTANT_STARTED = "message.assistant_started"
    MESSAGE_END = "message_end"
    MESSAGE_STREAM_FAILED = "message.stream_failed"
    MESSAGE_INTERRUPTED = "message.interrupted"
    MESSAGE_PATCH = "message.patch"
    SESSION_MODE_CHANGED = "session.mode_changed"
    SESSION_ASK_STATE_UPDATED = "session.ask_state_updated"
    SESSION_TITLE_UPDATED = "session.title_updated"
    SESSION_STATUS_CHANGED = "session.status_changed"
    TOOL_CALL_STARTED = "tool.call_started"
    TOOL_CALL_COMPLETED = "tool.call_completed"
    TOOL_CALL_FAILED = "tool.call_failed"
    COMPACTION_SUMMARY_ADDED = "compaction.summary_added"
    COMPACTION_ANCHOR_MOVED = "compaction.anchor_moved"
    COMPACTION_REBASE_COMPLETED = "compaction.rebase_completed"


class EventVisibility:
    AGENT = "agent"
    UI = "ui"
    SYSTEM = "system"
    OBSERVABILITY = "observability"


class SessionEvent(BaseModel):
    seq: int
    event_id: str = Field(default_factory=lambda: f"evt-{uuid.uuid4().hex[:12]}")
    session_id: str
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    visibility: str = EventVisibility.AGENT


def make_session_event(
    *,
    seq: int,
    session_id: str,
    type: str,
    payload: dict[str, Any] | None = None,
    visibility: str = EventVisibility.AGENT,
) -> SessionEvent:
    return SessionEvent(
        seq=seq,
        session_id=session_id,
        type=type,
        payload=payload or {},
        visibility=visibility,
    )
