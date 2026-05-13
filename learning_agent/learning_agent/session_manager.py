"""
会话管理器：树形会话的 CRUD、分支、导航、压缩。
会话不是线性对话，而是一棵探索树。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from learning_agent.ai import (
    EntryType,
    LearningSession,
    MessageRole,
    SessionEntry,
    SessionStatus,
)

logger = logging.getLogger(__name__)


class SessionManager:
    """
    管理会话树的创建、追加、fork、导航。
    当前版本在内存中维护会话，通过 Persistence Layer 持久化。
    """

    def __init__(self):
        self._sessions: dict[str, LearningSession] = {}
        self._on_delete_callbacks: list[Callable[[str], None]] = []

    def register_delete_callback(self, callback: Callable[[str], None]) -> None:
        """注册 session 删除时的回调函数。"""
        self._on_delete_callbacks.append(callback)

    def delete_session(self, session_id: str) -> bool:
        """删除指定会话，并触发删除回调。"""
        if session_id not in self._sessions:
            return False
        del self._sessions[session_id]
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
        logger.info(f"[SessionManager] Created session {session.id} (obj={objective_id})")
        return session

    def add_session(self, session: LearningSession) -> LearningSession:
        """将外部加载的会话注册到当前管理器。"""
        self._sessions[session.id] = session
        return session

    def get_session(self, session_id: str) -> Optional[LearningSession]:
        return self._sessions.get(session_id)

    def has_session(self, session_id: str) -> bool:
        return session_id in self._sessions

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
        return session

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
        return entry

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
        from learning_agent.ai import AskState
        session.ask_state = AskState()
        logger.info(f"[SessionManager] Cleared ask_state for session {session_id}")
        return True

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
        return True

    def to_dict(self) -> dict:
        return {sid: s.model_dump() for sid, s in self._sessions.items()}

    def from_dict(self, data: dict) -> None:
        self._sessions.clear()
        for sid, sdata in data.items():
            self._sessions[sid] = LearningSession(**sdata)
