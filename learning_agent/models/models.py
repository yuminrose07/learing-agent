"""
Learning-Agent 核心数据模型
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncIterable, Callable, Coroutine, Optional

from pydantic import BaseModel, Field


# ───────────────────────────────
# 枚举类型
# ───────────────────────────────

class KnowledgeNodeType(str, Enum):
    CONCEPT = "concept"
    PROCEDURE = "procedure"
    PRINCIPLE = "principle"
    ANALOGY = "analogy"
    QUESTION = "question"
    CODE_PATTERN = "code_pattern"


class MasteryLevel(str, Enum):
    ESTIMATED = "estimated"
    FAMILIAR = "familiar"
    UNDERSTOOD = "understood"
    MASTERED = "mastered"


class MemoryLevel(str, Enum):
    L0_TRANSIENT = "L0_transient"
    L1_WORKING = "L1_working"
    L2_LONGTERM = "L2_longterm"
    L3_ARCHIVE = "L3_archive"


class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class EntryType(str, Enum):
    MESSAGE = "message"
    FORK_POINT = "fork_point"
    TOOL_RESULT = "tool_result"
    SUMMARY = "summary"


class SessionStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    COMPACTED = "compacted"


class ObjectiveStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class MaterialType(str, Enum):
    PDF = "pdf"
    WEB_PAGE = "web_page"
    VIDEO = "video"
    CODE_REPO = "code_repo"
    MARKDOWN_NOTE = "markdown_note"
    BOOK = "book"
    AUDIO = "audio"
    PLAIN_TEXT = "plain_text"


class ExtensionType(str, Enum):
    BUILTIN = "builtin"
    EXTERNAL = "external"
    TEMPORARY = "temporary"


class EdgeType(str, Enum):
    PREREQUISITE = "prerequisite"
    RELATED = "related"
    ANALOGY = "analogy"
    EXTENDS = "extends"
    CONTRADICTS = "contradicts"


# ───────────────────────────────
# 知识相关
# ───────────────────────────────

class SourceLocation(BaseModel):
    type: str = "unknown"
    page: Optional[int] = None
    paragraph: Optional[int] = None
    line: Optional[int] = None
    url: Optional[str] = None


class ReviewRecord(BaseModel):
    date: datetime
    result: str  # pass | struggle | fail
    interval_days: int = 1
    ease_factor: float = 2.5


class KnowledgeNode(BaseModel):
    id: str = Field(default_factory=lambda: f"kn-{uuid.uuid4().hex[:8]}")
    type: KnowledgeNodeType = KnowledgeNodeType.CONCEPT
    content: str
    code: Optional[str] = None
    formula: Optional[str] = None
    source_material_id: Optional[str] = None
    source_location: Optional[SourceLocation] = None
    confidence: float = 1.0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    mastery_level: MasteryLevel = MasteryLevel.ESTIMATED
    last_reviewed_at: Optional[datetime] = None
    next_review_at: Optional[datetime] = None
    tags: list[str] = Field(default_factory=list)
    related_node_ids: list[str] = Field(default_factory=list)
    conversation_ids: list[str] = Field(default_factory=list)
    review_history: list[ReviewRecord] = Field(default_factory=list)
    memory_level: MemoryLevel = MemoryLevel.L1_WORKING


class KnowledgeEdge(BaseModel):
    id: str = Field(default_factory=lambda: f"ke-{uuid.uuid4().hex[:8]}")
    source_id: str
    target_id: str
    edge_type: EdgeType = EdgeType.RELATED
    strength: float = 1.0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


# ───────────────────────────────
# 学习目标与会话
# ───────────────────────────────

class ProgressMetrics(BaseModel):
    total_nodes: int = 0
    mastered_nodes: int = 0
    in_progress_nodes: int = 0
    last_activity: Optional[datetime] = None


class LearningObjective(BaseModel):
    id: str = Field(default_factory=lambda: f"obj-{uuid.uuid4().hex[:8]}")
    title: str
    description: Optional[str] = None
    parent_id: Optional[str] = None
    status: ObjectiveStatus = ObjectiveStatus.ACTIVE
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    target_date: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    related_material_ids: list[str] = Field(default_factory=list)
    progress_metrics: ProgressMetrics = Field(default_factory=ProgressMetrics)


class SessionEntry(BaseModel):
    id: str = Field(default_factory=lambda: f"entry-{uuid.uuid4().hex[:8]}")
    parent_id: Optional[str] = None
    type: EntryType = EntryType.MESSAGE
    role: Optional[MessageRole] = None
    content: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)
    tool_calls: list[ToolCall] = Field(default_factory=list)


class LearningSession(BaseModel):
    id: str = Field(default_factory=lambda: f"sess-{uuid.uuid4().hex[:8]}")
    objective_id: Optional[str] = None
    title: Optional[str] = None
    root_entry_id: Optional[str] = None
    current_leaf_id: Optional[str] = None
    status: SessionStatus = SessionStatus.ACTIVE
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_accessed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    extracted_knowledge_ids: list[str] = Field(default_factory=list)
    entries: list[SessionEntry] = Field(default_factory=list)


# ───────────────────────────────
# 材料
# ───────────────────────────────

class MaterialChunk(BaseModel):
    id: str = Field(default_factory=lambda: f"chunk-{uuid.uuid4().hex[:8]}")
    content: str
    embedding: Optional[list[float]] = None
    start_loc: Optional[dict[str, Any]] = None
    end_loc: Optional[dict[str, Any]] = None


class Material(BaseModel):
    id: str = Field(default_factory=lambda: f"mat-{uuid.uuid4().hex[:8]}")
    type: MaterialType = MaterialType.PLAIN_TEXT
    title: str
    uri: Optional[str] = None
    local_path: Optional[str] = None
    ingestion_status: str = "pending"  # pending | processing | indexed | failed
    chunks: list[MaterialChunk] = Field(default_factory=list)
    extracted_knowledge_ids: list[str] = Field(default_factory=list)
    added_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ───────────────────────────────
# 扩展与工具
# ───────────────────────────────

class ToolDefinition(BaseModel):
    id: str
    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    handler: Optional[Callable[..., Coroutine[Any, Any, Any]]] = Field(default=None, exclude=True)


class ToolCall(BaseModel):
    tool_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Optional[Any] = None
    error: Optional[str] = None
    duration_ms: Optional[int] = None


class HookPoint(str, Enum):
    BEFORE_INTENT_PARSE = "agent.beforeIntentParse"
    AFTER_INTENT_PARSE = "agent.afterIntentParse"
    BEFORE_CONTEXT_BUILD = "agent.beforeContextBuild"
    BEFORE_LLM_CALL = "agent.beforeLLMCall"
    ON_STREAM_CHUNK = "agent.onStreamChunk"
    AFTER_RESPONSE = "agent.afterResponse"
    ON_TOOL_CALL = "agent.onToolCall"
    AFTER_TOOL_RESULT = "agent.afterToolResult"
    BEFORE_MEMORY_STORE = "memory.beforeStore"
    AFTER_MEMORY_RECALL = "memory.afterRecall"
    ON_SESSION_FORK = "session.onFork"
    ON_SESSION_END = "session.onEnd"


class HookResult(BaseModel):
    modified: bool = False
    data: Any = None
    abort: bool = False
    abort_reason: Optional[str] = None


class Event(BaseModel):
    id: str = Field(default_factory=lambda: f"evt-{uuid.uuid4().hex[:8]}")
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str = "system"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    session_id: Optional[str] = None
    trace_id: Optional[str] = None


# ───────────────────────────────
# Provider
# ───────────────────────────────

class ChatMessage(BaseModel):
    role: MessageRole = MessageRole.USER
    content: str
    name: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None
    tool_call_id: Optional[str] = None


class ChatParams(BaseModel):
    model: str
    messages: list[ChatMessage]
    temperature: float = 0.7
    tools: Optional[list[ToolDefinition]] = None
    stream: bool = True
    max_tokens: Optional[int] = None


class ChatChunk(BaseModel):
    content: str = ""
    tool_call: Optional[dict[str, Any]] = None
    finish_reason: Optional[str] = None


class ProviderConfig(BaseModel):
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: str = "gpt-4o"
    timeout: float = 60.0
    max_retries: int = 3


# ───────────────────────────────
# 可观测性
# ───────────────────────────────

class TraceSpan(BaseModel):
    span_id: str = Field(default_factory=lambda: f"span-{uuid.uuid4().hex[:8]}")
    parent_id: Optional[str] = None
    name: str
    start_time: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    end_time: Optional[datetime] = None
    duration_ms: Optional[int] = None
    tags: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None

    def end(self):
        self.end_time = datetime.now(timezone.utc)
        if self.start_time:
            self.duration_ms = int((self.end_time - self.start_time).total_seconds() * 1000)


class Trace(BaseModel):
    trace_id: str = Field(default_factory=lambda: f"trace-{uuid.uuid4().hex[:8]}")
    session_id: Optional[str] = None
    objective_id: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: Optional[int] = None
    spans: list[TraceSpan] = Field(default_factory=list)

    def start_span(self, name: str, parent: Optional[TraceSpan] = None) -> TraceSpan:
        span = TraceSpan(
            name=name,
            parent_id=parent.span_id if parent else None,
        )
        self.spans.append(span)
        return span

    def end(self):
        self.duration_ms = int(
            (datetime.now(timezone.utc) - self.timestamp).total_seconds() * 1000
        )


class Snapshot(BaseModel):
    snapshot_id: str = Field(default_factory=lambda: f"snap-{uuid.uuid4().hex[:8]}")
    trace_id: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    node: str
    state: dict[str, Any] = Field(default_factory=dict)


# ───────────────────────────────
# Agent 循环中间产物
# ───────────────────────────────

class IntentResult(BaseModel):
    type: str = "chat"
    confidence: float = 0.8
    entities: list[dict[str, Any]] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification_question: Optional[str] = None


class ContextComponent(BaseModel):
    type: str
    tokens: int = 0
    content: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict)
