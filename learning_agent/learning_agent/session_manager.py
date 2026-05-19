"""
会话管理器：树形会话的 CRUD、分支、导航、压缩。
会话不是线性对话，而是一棵探索树。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from learning_agent.ai.file_store import FileStore
from learning_agent.ai import (
    AgentMode,
    AskState,
    EntryType,
    Event,
    LearningSession,
    MessageRole,
    SessionEntry,
    SessionStatus,
)
from learning_agent.learning_agent.compaction.models import (
    CompactMetadata,
    FullCompactResult,
    SessionMemoryState,
)

logger = logging.getLogger(__name__)


class SessionManager:
    """
    管理会话树的创建、追加、fork、导航。
    当前版本在内存中维护会话，通过 Persistence Layer 持久化。
    """

    def __init__(self, event_bus: Any = None, file_store: FileStore | None = None):
        self._sessions: dict[str, LearningSession] = {}
        self._session_memory_states: dict[str, SessionMemoryState] = {}
        self._compact_metadata: dict[str, CompactMetadata] = {}
        self._on_delete_callbacks: list[Callable[[str], None]] = []
        self._event_bus = event_bus
        self._file_store = file_store

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
        """创建新会话，生成根节点。"""
        root = SessionEntry(
            id=f"entry-root-{__import__('uuid').uuid4().hex[:6]}",
            parent_id=None,
            type=EntryType.MESSAGE,
            role=MessageRole.SYSTEM,
            content="Session started.",
        )
        session = LearningSession(
            objective_id=objective_id,
            title=title or "New Session",
            root_entry_id=root.id,
            current_leaf_id=root.id,
            entries=[root],
        )
        self._sessions[session.id] = session
        self._session_memory_states[session.id] = SessionMemoryState(
            session_id=session.id,
            mode=session.mode.value,
        )
        self._compact_metadata[session.id] = CompactMetadata(session_id=session.id)
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

    def get_session(self, session_id: str) -> Optional[LearningSession]:
        return self._sessions.get(session_id)

    def has_session(self, session_id: str) -> bool:
        return session_id in self._sessions

    def get_session_memory_state(self, session_id: str) -> SessionMemoryState | None:
        return self._session_memory_states.get(session_id)

    def set_session_memory_state(self, session_id: str, state: SessionMemoryState) -> None:
        self._session_memory_states[session_id] = state
        self._persist_session_memory_state(session_id)

    def get_compact_metadata(self, session_id: str) -> CompactMetadata | None:
        return self._compact_metadata.get(session_id)

    def set_compact_metadata(self, session_id: str, metadata: CompactMetadata) -> None:
        self._compact_metadata[session_id] = metadata
        self._persist_compact_metadata(session_id)

    def save_compact_summary(self, session_id: str, content: str) -> str:
        if self._file_store is None:
            return ""
        return self._file_store.save_compact_summary(session_id, content)

    def load_compact_summary(self, session_id: str) -> str | None:
        if self._file_store is None:
            return None
        return self._file_store.load_compact_summary(session_id)

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

    def append_message(
        self,
        session_id: str,
        role: MessageRole,
        content: str,
        parent_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Optional[SessionEntry]:
        """
        追加消息到会话树。
        如果不指定 parent_id，默认追加到 current_leaf。
        """
        session = self._sessions.get(session_id)
        if not session:
            return None

        parent = parent_id or session.current_leaf_id
        entry = SessionEntry(
            parent_id=parent,
            type=EntryType.MESSAGE,
            role=role,
            content=content,
            metadata=metadata or {},
        )
        session.entries.append(entry)
        session.current_leaf_id = entry.id
        session.last_accessed_at = datetime.now(timezone.utc)
        logger.debug(f"[SessionManager] Appended entry {entry.id} to session {session_id}")
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
        self._emit_event(
            "session.entryPatched",
            {"session_id": session_id, "entry_id": entry_id, "path": path, "value": value},
        )
        return True

    def fork_at(
        self,
        session_id: str,
        entry_id: str,
        fork_content: Optional[str] = None,
    ) -> Optional[SessionEntry]:
        """
        在指定节点创建分支。
        插入一个 fork_point 标记节点，然后新消息可以挂在这个标记下。
        """
        session = self._sessions.get(session_id)
        if not session:
            return None

        fork_entry = SessionEntry(
            parent_id=entry_id,
            type=EntryType.FORK_POINT,
            content=fork_content or "Forked branch",
        )
        session.entries.append(fork_entry)
        session.current_leaf_id = fork_entry.id
        session.last_accessed_at = datetime.now(timezone.utc)
        logger.info(f"[SessionManager] Forked at {entry_id} -> new leaf {fork_entry.id}")
        self._emit_event(
            "session.entryAppended",
            {"session_id": session_id, "entry": fork_entry.model_dump()},
        )
        return fork_entry

    def get_path_to_leaf(self, session_id: str, leaf_id: Optional[str] = None) -> list[SessionEntry]:
        """获取从根到指定叶子的完整路径。"""
        session = self._sessions.get(session_id)
        if not session:
            return []

        target = leaf_id or session.current_leaf_id
        entry_map = {e.id: e for e in session.entries}
        path = []
        current = target
        while current and current in entry_map:
            entry = entry_map[current]
            path.append(entry)
            current = entry.parent_id
        path.reverse()
        return path

    def get_message_history(self, session_id: str, leaf_id: Optional[str] = None) -> list[SessionEntry]:
        """获取当前分支的消息历史（过滤掉 fork_point 等非消息节点）。"""
        path = self.get_path_to_leaf(session_id, leaf_id)
        return [e for e in path if e.type == EntryType.MESSAGE and e.role is not None]

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

    def clear_ask_state(self, session_id: str) -> bool:
        """清除会话的 Ask 对齐状态。"""
        session = self._sessions.get(session_id)
        if not session:
            return False
        session.ask_state = AskState()
        logger.info(f"[SessionManager] Cleared ask_state for session {session_id}")
        self._emit_event(
            "session.scalarChanged",
            {"session_id": session_id, "path": "ask_state", "value": session.ask_state.model_dump()},
        )
        return True

    def switch_session_mode(
        self,
        session_id: str,
        mode: AgentMode,
        *,
        clear_ask_state: bool = True,
    ) -> LearningSession:
        """切换会话模式，并在需要时清理 Ask 临时状态。"""
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")

        from_mode = session.mode
        if from_mode == mode:
            return session

        if clear_ask_state and from_mode == AgentMode.ASK and mode != AgentMode.ASK:
            session.ask_state = AskState()

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
        self._emit_event(
            "session.scalarChanged",
            {"session_id": session_id, "path": "mode", "value": mode.value},
        )
        return session

    def compact_session(self, session_id: str) -> bool:
        """
        压缩会话：将深层旧分支替换为摘要节点。
        当前版本为占位实现。
        """
        session = self._sessions.get(session_id)
        if not session:
            return False
        # TODO: 实现真正的 compaction 逻辑
        session.status = SessionStatus.COMPACTED
        logger.info(f"[SessionManager] Compacted session {session_id}")
        self._emit_event(
            "session.scalarChanged",
            {"session_id": session_id, "path": "status", "value": SessionStatus.COMPACTED.value},
        )
        return True

    def apply_full_compact_result(self, session_id: str, result: FullCompactResult) -> bool:
        session = self._sessions.get(session_id)
        if not session:
            return False
        session.status = SessionStatus.COMPACTED
        session.mode_metadata["compaction"] = {
            "scope": result.scope,
            "cut_point_entry_id": result.cut_point_entry_id,
            "anchor_entry_id": result.next_anchor_entry_id,
            "estimated_tokens_after": result.estimated_tokens_after,
        }
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
        return True

    def to_dict(self) -> dict:
        return {sid: s.model_dump() for sid, s in self._sessions.items()}

    def from_dict(self, data: dict) -> None:
        self._sessions.clear()
        for sid, sdata in data.items():
            self._sessions[sid] = LearningSession(**sdata)
