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
    # 观测/诊断侧事件（visibility=OBSERVABILITY）。
    # 与 TOOL_CALL_COMPLETED/FAILED 业务事件并行存在，仅用于追踪一次工具执行的完整时序与因果，
    # 不参与 replay/projection，不进入 session.messages。
    TOOL_EXEC_STARTED = "tool.exec_started"
    TOOL_EXEC_COMPLETED = "tool.exec_completed"
    TOOL_EXEC_FAILED = "tool.exec_failed"
    COMPACTION_SUMMARY_ADDED = "compaction.summary_added"
    COMPACTION_ANCHOR_MOVED = "compaction.anchor_moved"
    COMPACTION_REBASE_COMPLETED = "compaction.rebase_completed"


class EventVisibility:
    AGENT = "agent"
    UI = "ui"
    SYSTEM = "system"
    # 诊断/观测事件标记。
    # - 写入 sessions/<id>.events.jsonl 与业务事件共存于同一时间轴
    # - 但 replay_events 必须显式跳过（不参与 state 重建）
    # - 用于：tool.exec_*、llm.*、hook.* 等诊断粒度事件
    OBSERVABILITY = "observability"


class SessionEvent(BaseModel):
    seq: int
    event_id: str = Field(default_factory=lambda: f"evt-{uuid.uuid4().hex[:12]}")
    session_id: str
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    visibility: str = EventVisibility.AGENT
    # 因果链字段：trace/observability 事件应指向触发它的业务事件。
    # business 事件通常不需要 parent（靠 seq 顺序串联）。
    parent_event_id: str | None = None


def make_session_event(
    *,
    seq: int,
    session_id: str,
    type: str,
    payload: dict[str, Any] | None = None,
    visibility: str = EventVisibility.AGENT,
    parent_event_id: str | None = None,
) -> SessionEvent:
    return SessionEvent(
        seq=seq,
        session_id=session_id,
        type=type,
        payload=payload or {},
        visibility=visibility,
        parent_event_id=parent_event_id,
    )
