"""
会话管理器：Product/Application 层 session 状态 facade。

新写入路径采用线性消息序列，并通过 session event log 持久化。
旧树形字段仅保留为兼容数据，不再作为主路径语义。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from learning_agent.ai.file_store import FileStore
from learning_agent.ai import (
    AgentMode,
    EntryType,
    Event,
    LearningSession,
    MessageRole,
    SessionEntry,
    SessionStatus,
)
from learning_agent.learning_agent.compaction.models import (
    CompactMetadata,
    CompactionPlan,
    FullCompactResult,
    SessionMemoryState,
)
from learning_agent.learning_agent.mode_service import TurnExecutionProfile
from learning_agent.learning_agent.session_event_store import SessionEventStore
from learning_agent.learning_agent.session_events import SessionEvent, SessionEventType
from learning_agent.learning_agent.session_projection import (
    AgentSnapshot,
    project_legacy_session,
    replay_events,
)
from learning_agent.learning_agent.views import LLMInputView, UIViewMessage, build_llm_input_view, build_ui_messages

logger = logging.getLogger(__name__)


class SessionManager:
    """
    管理线性 session 消息与产品状态。
    当前版本在内存中维护派生状态，通过 session event log 持久化事实。
    """

    def __init__(
        self,
        event_bus: Any = None,
        file_store: FileStore | None = None,
        event_store: SessionEventStore | None = None,
    ):
        self._sessions: dict[str, LearningSession] = {}
        self._session_memory_states: dict[str, SessionMemoryState] = {}
        self._compact_metadata: dict[str, CompactMetadata] = {}
        self._on_delete_callbacks: list[Callable[[str], None]] = []
        self._event_bus = event_bus
        self._file_store = file_store
        self._event_store = event_store or (SessionEventStore(file_store) if file_store else None)

    def register_delete_callback(self, callback: Callable[[str], None]) -> None:
        """注册 session 删除时的回调函数。"""
        self._on_delete_callbacks.append(callback)

    def delete_session(self, session_id: str) -> bool:
        """删除指定会话，并触发删除回调。"""
        if session_id not in self._sessions:
            return False
        del self._sessions[session_id]
        self._session_memory_states.pop(session_id, None)
        self._compact_metadata.pop(session_id, None)
        logger.info(f"[SessionManager] Deleted session {session_id}")
        for cb in self._on_delete_callbacks:
            try:
                cb(session_id)
            except Exception:
                logger.exception(f"[SessionManager] Delete callback failed for {session_id}")
        return True

    def create_session(
        self,
        objective_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> LearningSession:
        """创建新会话。新模型不再写入 root/fork 节点。"""
        session = LearningSession(
            objective_id=objective_id,
            title=title or "New Session",
            root_entry_id=None,
            current_leaf_id=None,
            entries=[],
        )
        self._sessions[session.id] = session
        self._session_memory_states[session.id] = SessionMemoryState(
            session_id=session.id,
            mode=session.mode.value,
        )
        self._compact_metadata[session.id] = CompactMetadata(session_id=session.id)
        self._append_session_event(
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
        self._persist_session_memory_state(session.id)
        self._persist_compact_metadata(session.id)
        logger.info(f"[SessionManager] Created session {session.id} (obj={objective_id})")
        return session

    def add_session(self, session: LearningSession) -> LearningSession:
        """将外部加载的会话注册到当前管理器。"""
        self._sessions[session.id] = session
        self._session_memory_states[session.id] = self._load_or_create_session_memory_state(session)
        self._compact_metadata[session.id] = self._load_or_create_compact_metadata(session.id)
        return session

    def add_snapshot(self, snapshot: AgentSnapshot) -> LearningSession:
        session = snapshot.to_learning_session()
        self._sessions[session.id] = session
        self._session_memory_states[session.id] = self._load_or_create_session_memory_state(session)
        self._compact_metadata[session.id] = snapshot.compact_metadata or self._load_or_create_compact_metadata(session.id)
        return session

    def get_session(self, session_id: str) -> Optional[LearningSession]:
        return self._sessions.get(session_id)

    def has_session(self, session_id: str) -> bool:
        return session_id in self._sessions

    def get_session_memory_state(self, session_id: str) -> SessionMemoryState | None:
        return self._session_memory_states.get(session_id)

    def set_session_memory_state(self, session_id: str, state: SessionMemoryState) -> None:
        self._session_memory_states[session_id] = state
        self._persist_session_memory_state(session_id)

    def persist_mode_metadata(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        self._append_session_event(
            session_id,
            SessionEventType.SESSION_MODE_CHANGED,
            {
                "from": session.mode.value,
                "to": session.mode.value,
                "mode_metadata": dict(session.mode_metadata),
            },
            visibility="system",
        )

    def bind_learning_unit(self, session_id: str, learning_unit_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session is None:
            return False
        if session.learning_unit_id == learning_unit_id:
            return True
        session.learning_unit_id = learning_unit_id
        self._append_session_event(
            session_id,
            SessionEventType.SESSION_LEARNING_UNIT_BOUND,
            {"learning_unit_id": learning_unit_id},
            visibility="system",
        )
        self._emit_event(
            "session.scalarChanged",
            {
                "session_id": session_id,
                "path": "learning_unit_id",
                "value": learning_unit_id,
            },
        )
        return True

    def get_compact_metadata(self, session_id: str) -> CompactMetadata | None:
        if self._event_store is not None:
            snapshot = self.get_agent_snapshot(session_id)
            if snapshot.compact_metadata is not None:
                self._compact_metadata[session_id] = snapshot.compact_metadata
        return self._compact_metadata.get(session_id)

    def set_compact_metadata(self, session_id: str, metadata: CompactMetadata) -> None:
        self._compact_metadata[session_id] = metadata
        self._persist_compact_metadata(session_id)

    def save_compact_summary(self, session_id: str, content: str) -> str:
        if self._file_store is None:
            return ""
        return self._file_store.save_compact_summary(session_id, content)

    def load_compact_summary(self, session_id: str) -> str | None:
        snapshot = self.get_agent_snapshot(session_id)
        if snapshot.latest_compact_summary:
            return snapshot.latest_compact_summary
        return None

    def get_latest_compact_summary_block(self, session_id: str) -> tuple[str, CompactMetadata] | None:
        snapshot = self.get_agent_snapshot(session_id)
        if not snapshot.latest_compact_summary or snapshot.compact_metadata is None:
            return None
        return snapshot.latest_compact_summary, snapshot.compact_metadata

    def _load_or_create_session_memory_state(self, session: LearningSession) -> SessionMemoryState:
        if self._file_store is not None:
            data = self._file_store.load_session_memory_state(session.id)
            if data:
                return SessionMemoryState.from_dict(data)
        return SessionMemoryState(session_id=session.id, mode=session.mode.value)

    def _load_or_create_compact_metadata(self, session_id: str) -> CompactMetadata:
        if self._file_store is not None:
            data = self._file_store.load_compact_metadata(session_id)
            if data:
                return CompactMetadata.from_dict(data)
        return CompactMetadata(session_id=session_id)

    def _persist_session_memory_state(self, session_id: str) -> None:
        if self._file_store is None:
            return
        state = self._session_memory_states.get(session_id)
        if state is not None:
            self._file_store.save_session_memory_state(session_id, state.to_dict())

    def _persist_compact_metadata(self, session_id: str) -> None:
        if self._file_store is None:
            return
        metadata = self._compact_metadata.get(session_id)
        if metadata is not None:
            self._file_store.save_compact_metadata(session_id, metadata.to_dict())

    def update_session_title(
        self,
        session_id: str,
        title: Optional[str],
    ) -> Optional[LearningSession]:
        session = self._sessions.get(session_id)
        if not session:
            return None
        session.title = title
        session.last_accessed_at = datetime.now(timezone.utc)
        self._append_session_event(
            session_id,
            SessionEventType.SESSION_TITLE_UPDATED,
            {"title": title},
            visibility="system",
        )
        self._emit_event(
            "session.scalarChanged",
            {"session_id": session_id, "path": "title", "value": title},
        )
        return session

    def _emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._event_bus is not None:
            try:
                asyncio.create_task(
                    self._event_bus.publish(
                        Event(
                            type=event_type,
                            payload=payload,
                            source="session_manager",
                        )
                    )
                )
            except Exception:
                logger.exception("[SessionManager] Failed to emit event %s", event_type)

    def _append_session_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        visibility: str = "agent",
    ) -> SessionEvent | None:
        if self._event_store is None:
            return None
        try:
            return self._event_store.append_event(
                session_id=session_id,
                type=event_type,
                payload=payload,
                visibility=visibility,
            )
        except Exception:
            logger.exception(
                "[SessionManager] Failed to append session event %s (session=%s)",
                event_type,
                session_id,
            )
        return None

    def _event_type_for_entry(self, entry: SessionEntry) -> str:
        if entry.role == MessageRole.USER:
            return SessionEventType.MESSAGE_USER_APPENDED
        if entry.role == MessageRole.TOOL:
            return (
                SessionEventType.TOOL_CALL_FAILED
                if entry.metadata.get("is_error")
                else SessionEventType.TOOL_CALL_COMPLETED
            )
        return SessionEventType.MESSAGE_END

    def append_message(
        self,
        session_id: str,
        role: MessageRole,
        content: str,
        parent_id: Optional[str] = None,
        metadata: Optional[dict] = None,
        tool_calls: Optional[list[Any]] = None,
        tool_results: Optional[list[dict[str, Any]]] = None,
    ) -> Optional[SessionEntry]:
        """
        追加消息到线性会话序列。
        `parent_id` 仅作为 legacy 参数保留，新写入不会依赖它。
        """
        session = self._sessions.get(session_id)
        if not session:
            return None

        entry = SessionEntry(
            parent_id=None,
            type=EntryType.MESSAGE,
            role=role,
            content=content,
            metadata=metadata or {},
            tool_calls=tool_calls or [],
            tool_results=tool_results or [],
        )
        session.entries.append(entry)
        session.current_leaf_id = entry.id
        session.last_accessed_at = datetime.now(timezone.utc)
        logger.debug(f"[SessionManager] Appended entry {entry.id} to session {session_id}")
        self._append_session_event(
            session_id,
            self._event_type_for_entry(entry),
            {"entry": entry.model_dump(mode="json")},
            visibility="agent",
        )
        self._emit_event(
            "session.entryAppended",
            {"session_id": session_id, "entry": entry.model_dump()},
        )
        return entry

    def patch_entry(
        self,
        session_id: str,
        entry_id: str,
        path: str,
        value: Any,
    ) -> bool:
        """修改已有 entry 的字段，用于运行时紧急截断等场景。"""
        session = self._sessions.get(session_id)
        if not session:
            return False

        entry = next((e for e in session.entries if e.id == entry_id), None)
        if entry is None:
            return False

        parts = path.split(".")
        current: Any = entry
        for part in parts[:-1]:
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list):
                try:
                    current = current[int(part)]
                except (ValueError, IndexError):
                    return False
            else:
                current = getattr(current, part, None)
            if current is None:
                return False

        last = parts[-1]
        if isinstance(current, dict):
            current[last] = value
        elif isinstance(current, list):
            try:
                current[int(last)] = value
            except (ValueError, IndexError):
                return False
        else:
            setattr(current, last, value)

        logger.debug(
            "[SessionManager] Patched entry %s.%s = %s (session=%s)",
            entry_id,
            path,
            repr(value)[:100],
            session_id,
        )
        self._append_session_event(
            session_id,
            SessionEventType.MESSAGE_PATCH,
            {"entry_id": entry_id, "path": path, "value": value},
            visibility="agent",
        )
        self._emit_event(
            "session.entryPatched",
            {"session_id": session_id, "entry_id": entry_id, "path": path, "value": value},
        )
        return True

    def get_message_history(self, session_id: str, leaf_id: Optional[str] = None) -> list[SessionEntry]:
        """获取线性消息历史。`leaf_id` 仅保留兼容，不参与主路径。"""
        del leaf_id
        snapshot = self.get_agent_snapshot(session_id)
        return [entry.model_copy(deep=True) for entry in snapshot.messages]

    def iter_message_entries(self, session_id: str, leaf_id: str | None = None) -> list[SessionEntry]:
        return self.get_message_history(session_id, leaf_id)

    def list_entries_after(self, session_id: str, entry_id: str | None) -> list[SessionEntry]:
        history = self.get_message_history(session_id)
        if entry_id is None:
            return history
        for index, entry in enumerate(history):
            if entry.id == entry_id:
                return history[index + 1 :]
        return history

    def get_agent_snapshot(self, session_id: str) -> AgentSnapshot:
        if self._event_store is not None:
            events = self._event_store.read_events(session_id)
            if events:
                return replay_events(events, session_id=session_id)

        session = self._sessions.get(session_id)
        if session is None:
            return AgentSnapshot(session_id=session_id, compact_metadata=CompactMetadata(session_id=session_id))
        snapshot = project_legacy_session(session)
        snapshot.compact_metadata = self._compact_metadata.get(session_id) or snapshot.compact_metadata
        return snapshot

    def build_llm_input_view(
        self,
        session_id: str,
        profile: TurnExecutionProfile,
        compaction_plan: CompactionPlan | None = None,
    ) -> LLMInputView:
        return build_llm_input_view(
            self.get_agent_snapshot(session_id),
            profile,
            compaction_plan=compaction_plan,
        )

    def build_ui_messages(self, session_id: str) -> list[UIViewMessage]:
        return build_ui_messages(self.get_agent_snapshot(session_id))

    def find_truncatable_tool_entry(
        self,
        session_id: str,
        min_chars: int,
    ) -> Optional[SessionEntry]:
        session = self._sessions.get(session_id)
        if not session:
            return None
        longest_tool_entry: Optional[SessionEntry] = None
        longest_tool_length = 0
        for entry in session.entries:
            if entry.role != MessageRole.TOOL:
                continue
            if entry.metadata.get("emergency_truncated"):
                continue
            content_length = len(entry.content or "")
            if content_length > min_chars and content_length > longest_tool_length:
                longest_tool_entry = entry
                longest_tool_length = content_length
        return longest_tool_entry

    def record_message_stream_failed(
        self,
        session_id: str,
        reason: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        self._append_session_event(
            session_id,
            SessionEventType.MESSAGE_STREAM_FAILED,
            {"reason": reason, "metadata": metadata or {}},
            visibility="agent",
        )

    def record_message_interrupted(
        self,
        session_id: str,
        reason: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        self._append_session_event(
            session_id,
            SessionEventType.MESSAGE_INTERRUPTED,
            {"reason": reason, "metadata": metadata or {}},
            visibility="agent",
        )

    def list_sessions(self, objective_id: Optional[str] = None) -> list[LearningSession]:
        sessions = list(self._sessions.values())
        if objective_id:
            sessions = [s for s in sessions if s.objective_id == objective_id]
        return sessions

    def archive_session(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session:
            session.status = SessionStatus.ARCHIVED
            logger.info(f"[SessionManager] Archived session {session_id}")
            return True
        return False

    def switch_session_mode(
        self,
        session_id: str,
        mode: AgentMode,
    ) -> LearningSession:
        """切换会话模式。"""
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")

        from_mode = session.mode
        if from_mode == mode:
            return session

        session.mode = mode
        session.mode_metadata["last_mode_switch"] = {
            "from": from_mode.value,
            "to": mode.value,
        }
        session.last_accessed_at = datetime.now(timezone.utc)
        state = self._session_memory_states.get(session_id)
        if state is not None:
            state.mode = session.mode.value
            state.updated_at = datetime.now(timezone.utc).timestamp()
            self._persist_session_memory_state(session_id)
        logger.info(
            "[SessionManager] Switched session %s mode: %s -> %s",
            session_id,
            from_mode.value,
            mode.value,
        )
        self._append_session_event(
            session_id,
            SessionEventType.SESSION_MODE_CHANGED,
            {
                "from": from_mode.value,
                "to": mode.value,
                "mode_metadata": dict(session.mode_metadata),
            },
            visibility="system",
        )
        self._emit_event(
            "session.scalarChanged",
            {"session_id": session_id, "path": "mode", "value": mode.value},
        )
        return session

    def apply_full_compact_result(self, session_id: str, result: FullCompactResult) -> bool:
        session = self._sessions.get(session_id)
        if not session:
            return False
        metadata = self._compact_metadata.get(session_id)
        summary_hash = result.summary_hash or (metadata.last_summary_hash if metadata else None)
        if summary_hash is None:
            summary_hash = hashlib.sha256(result.summary_text.encode("utf-8")).hexdigest()
        summary_event = self._append_session_event(
            session_id,
            SessionEventType.COMPACTION_SUMMARY_ADDED,
            {
                "schema_version": "v1",
                "mode": result.compact_mode,
                "scope": result.scope,
                "summary_text": result.summary_text,
                "summary_hash": summary_hash,
                "cut_point_entry_id": result.cut_point_entry_id,
                "anchor_entry_id": result.next_anchor_entry_id,
                "previous_compact_event_id": result.previous_compact_event_id,
                "current_user_event_id": result.current_user_event_id,
                "source_event_start_seq": result.source_event_start_seq,
                "source_event_end_seq": result.source_event_end_seq,
                "source_snapshot_seq": result.source_snapshot_seq,
                "source_event_ids": list(result.source_event_ids),
                "source_entry_ids": list(result.source_entry_ids),
                "retained_event_ids": list(result.retained_event_ids),
                "retained_entry_ids": list(result.retained_entry_ids),
                "template_version": result.template_version,
                "validation_status": dict(result.validation_status),
                "estimated_tokens_after": result.estimated_tokens_after,
                "next_jsonl_cursor": (
                    result.source_event_end_seq
                    if result.source_event_end_seq is not None
                    else None
                ),
            },
            visibility="agent",
        )
        if self._event_store is not None and summary_event is None:
            return False
        session.status = SessionStatus.COMPACTED
        session.mode_metadata["compaction"] = {
            "compact_event_id": result.compact_event_id,
            "mode": result.compact_mode,
            "scope": result.scope,
            "cut_point_entry_id": result.cut_point_entry_id,
            "anchor_entry_id": result.next_anchor_entry_id,
            "estimated_tokens_after": result.estimated_tokens_after,
        }
        if summary_event is not None:
            result.compact_event_id = summary_event.event_id
            result.compact_event_seq = summary_event.seq
            session.mode_metadata["compaction"]["compact_event_id"] = summary_event.event_id
            if metadata is not None:
                metadata.last_compact_event_id = summary_event.event_id
                metadata.compact_anchor_event_seq = summary_event.seq
                self._compact_metadata[session_id] = metadata
        self._emit_event(
            "session.scalarChanged",
            {
                "session_id": session_id,
                "path": "mode_metadata.compaction",
                "value": dict(session.mode_metadata["compaction"]),
            },
        )
        self._emit_event(
            "session.scalarChanged",
            {"session_id": session_id, "path": "status", "value": SessionStatus.COMPACTED.value},
        )
        self._append_session_event(
            session_id,
            SessionEventType.SESSION_STATUS_CHANGED,
            {"status": SessionStatus.COMPACTED.value},
            visibility="system",
        )
        return True

    def to_dict(self) -> dict:
        return {sid: s.model_dump() for sid, s in self._sessions.items()}

    def from_dict(self, data: dict) -> None:
        self._sessions.clear()
        for sid, sdata in data.items():
            self._sessions[sid] = LearningSession(**sdata)
