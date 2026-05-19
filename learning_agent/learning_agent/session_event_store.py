from __future__ import annotations

import logging
import threading
from typing import Any

from learning_agent.ai.file_store import FileStore
from learning_agent.learning_agent.session_events import SessionEvent, make_session_event

logger = logging.getLogger(__name__)


class SessionEventStore:
    """Product/Application facade for append-only session event logs."""

    def __init__(self, file_store: FileStore):
        self.file_store = file_store
        self._locks: dict[str, threading.Lock] = {}

    def append_event(
        self,
        session_id: str,
        type: str,
        payload: dict[str, Any] | None = None,
        visibility: str = "agent",
    ) -> SessionEvent:
        lock = self._lock_for(session_id)
        with lock:
            event = make_session_event(
                seq=self.next_seq(session_id),
                session_id=session_id,
                type=type,
                payload=payload or {},
                visibility=visibility,
            )
            self.file_store.append_session_event(session_id, event.model_dump(mode="json"))
            return event

    def append_existing_event(self, event: SessionEvent) -> SessionEvent:
        lock = self._lock_for(event.session_id)
        with lock:
            self.file_store.append_session_event(event.session_id, event.model_dump(mode="json"))
            return event

    def read_events(self, session_id: str, after_seq: int | None = None) -> list[SessionEvent]:
        rows = self.file_store.read_session_events(session_id, after_seq=after_seq)
        events: list[SessionEvent] = []
        for row in rows:
            try:
                events.append(SessionEvent.model_validate(row))
            except Exception:
                logger.exception("[SessionEventStore] Invalid session event skipped (session=%s)", session_id)
        return events

    def next_seq(self, session_id: str) -> int:
        events = self.file_store.read_session_events(session_id)
        max_seq = 0
        for event in events:
            try:
                max_seq = max(max_seq, int(event.get("seq", 0)))
            except Exception:
                continue
        return max_seq + 1

    def delete_events(self, session_id: str) -> bool:
        return self.file_store.delete_session_events(session_id)

    def _lock_for(self, session_id: str) -> threading.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = threading.Lock()
            self._locks[session_id] = lock
        return lock
