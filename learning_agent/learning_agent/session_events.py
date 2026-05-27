from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class SessionEventType:
    SESSION_CREATED = "session.created"
    SESSION_LEARNING_UNIT_BOUND = "session.learning_unit_bound"
    MESSAGE_USER_APPENDED = "message.user_appended"
    MESSAGE_ASSISTANT_STARTED = "message.assistant_started"
    MESSAGE_END = "message_end"
    MESSAGE_STREAM_FAILED = "message.stream_failed"
    MESSAGE_INTERRUPTED = "message.interrupted"
    MESSAGE_PATCH = "message.patch"
    SESSION_MODE_CHANGED = "session.mode_changed"
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
    # 学习卷产品事件（adaptive alignment §12.2）。visibility=AGENT，参与时间轴重建。
    # Payload 最小集：{learning_unit_id, phase, alignment_state, objective_status,
    # reason, clarification_count}。
    LEARNING_UNIT_CREATED = "learning_unit.created"
    LEARNING_UNIT_PHASE_CHANGED = "learning_unit.phase_changed"
    LEARNING_UNIT_ALIGNMENT_SUGGESTED = "learning_unit.alignment_suggested"
    LEARNING_UNIT_ALIGNMENT_STARTED = "learning_unit.alignment_started"
    LEARNING_UNIT_ALIGNMENT_RESOLVED = "learning_unit.alignment_resolved"
    LEARNING_UNIT_ALIGNMENT_SKIPPED = "learning_unit.alignment_skipped"
    LEARNING_UNIT_OBJECTIVE_REFINED = "learning_unit.objective_refined"
    LEARNING_UNIT_ASSUMPTION_ACCEPTED = "learning_unit.assumption_accepted"
    LEARNING_UNIT_FIRST_VALUE_DELIVERED = "learning_unit.first_value_delivered"
    LEARNING_UNIT_TEACH_ENTERED = "learning_unit.teach_entered"
    LEARNING_UNIT_CONSOLIDATED = "learning_unit.consolidated"
    LEARNING_UNIT_STOPPED = "learning_unit.stopped"
    LEARNING_UNIT_REUSE_FEEDBACK = "learning_unit.reuse_feedback"
    LEARNING_UNIT_FORGE_STAGE_CHANGED = "learning_unit.forge_stage_changed"
    # 闲聊陪伴档案事件。陪伴状态仍通过 session.mode_metadata 重建；
    # 这些事件用于观测触发、偏好变更与研学后的减压建议。
    COMPANION_PROFILE_CHANGED = "companion.profile_changed"
    COMPANION_SIGNAL_DETECTED = "companion.signal_detected"
    COMPANION_RECOVERY_SUGGESTED = "companion.recovery_suggested"


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
