"""
Learning-Agent 核心数据模型
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncIterable, Callable, Coroutine, Optional, Type

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


class AgentMode(str, Enum):
    CHAT = "chat"
    ASK = "ask"
    STUDY = "study"
    TEACH = "teach"


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
    tool_results: list[dict[str, Any]] = Field(default_factory=list)


class LearningSession(BaseModel):
    id: str = Field(default_factory=lambda: f"sess-{uuid.uuid4().hex[:8]}")
    objective_id: Optional[str] = None
    learning_unit_id: Optional[str] = None
    title: Optional[str] = None
    root_entry_id: Optional[str] = None
    current_leaf_id: Optional[str] = None
    status: SessionStatus = SessionStatus.ACTIVE
    mode: AgentMode = AgentMode.CHAT
    mode_metadata: dict[str, Any] = Field(default_factory=dict)
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
    input_model: Optional[Type[BaseModel]] = None              # 【新增】显式 Pydantic 模型
    handler: Optional[Callable[..., Coroutine[Any, Any, Any]]] = Field(default=None, exclude=True)


class ToolCall(BaseModel):
    tool_id: str
    call_id: Optional[str] = None   # OpenAI tool call id
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Optional[Any] = None
    error: Optional[str] = None
    duration_ms: Optional[int] = None


class ToolResultsBatch(BaseModel):
    """BEFORE_TOOL_RESULTS_PERSIST Hook 的入参数据结构。"""
    tool_calls: list[ToolCall]           # 声明的工具调用列表（按索引有序）
    results: list[tuple[ToolCall, Any, bool]]  # 执行结果


class HookName(str, Enum):
    BEFORE_AGENT_RUN = "before_agent_run"
    BEFORE_TOOL_EXECUTE = "before_tool_execute"
    AFTER_TOOL_EXECUTE = "after_tool_execute"
    ON_STREAM_CHUNK = "on_stream_chunk"
    AFTER_RESPONSE = "after_response"


class HookDecision(str, Enum):
    CONTINUE = "continue"
    ASK = "ask"
    DENY = "deny"


class HookWarning(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class HookAuditRecord(BaseModel):
    category: str
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)


class HookContext(BaseModel):
    session_id: str
    trace_id: Optional[str] = None
    turn_id: Optional[int] = None
    agent_state: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BeforeAgentRunInput(BaseModel):
    user_input: str
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    provider_summary: dict[str, Any] = Field(default_factory=dict)
    tools_summary: list[dict[str, Any]] = Field(default_factory=list)
    context: HookContext


class BeforeAgentRunResult(BaseModel):
    decision: HookDecision = HookDecision.CONTINUE
    ask_message: Optional[str] = None
    deny_reason: Optional[str] = None
    runtime_patch: dict[str, Any] = Field(default_factory=dict)
    warnings: list[HookWarning] = Field(default_factory=list)
    audit_records: list[HookAuditRecord] = Field(default_factory=list)


class BeforeToolExecuteInput(BaseModel):
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    tool_schema: dict[str, Any] = Field(default_factory=dict)
    context: HookContext


class BeforeToolExecuteResult(BaseModel):
    decision: HookDecision = HookDecision.CONTINUE
    ask_message: Optional[str] = None
    deny_reason: Optional[str] = None
    patched_arguments: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)
    warnings: list[HookWarning] = Field(default_factory=list)
    audit_records: list[HookAuditRecord] = Field(default_factory=list)


class AfterToolExecuteInput(BaseModel):
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    success: bool
    result: Any = None
    error: Optional[str] = None
    duration_ms: int = 0
    retry_count: int = 0
    annotations: dict[str, Any] = Field(default_factory=dict)
    context: HookContext


class AfterToolExecuteResult(BaseModel):
    display_result_override: Optional[str] = None
    extra_metadata: dict[str, Any] = Field(default_factory=dict)
    warnings: list[HookWarning] = Field(default_factory=list)
    audit_records: list[HookAuditRecord] = Field(default_factory=list)


class OnStreamChunkInput(BaseModel):
    chunk_index: int
    content: str = ""
    reasoning_content: str = ""
    finish_reason: Optional[str] = None
    context: HookContext


class OnStreamChunkResult(BaseModel):
    content_override: Optional[str] = None
    reasoning_content_override: Optional[str] = None
    stream_tags: dict[str, Any] = Field(default_factory=dict)
    warnings: list[HookWarning] = Field(default_factory=list)


class AfterResponseInput(BaseModel):
    response_text: str
    tool_calls_present: bool = False
    response_metadata: dict[str, Any] = Field(default_factory=dict)
    context: HookContext


class AfterResponseResult(BaseModel):
    response_override: Optional[str] = None
    extra_metadata: dict[str, Any] = Field(default_factory=dict)
    followup_signals: list[str] = Field(default_factory=list)
    warnings: list[HookWarning] = Field(default_factory=list)
    audit_records: list[HookAuditRecord] = Field(default_factory=list)


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
    reasoning_content: Optional[str] = None


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
    tool_call_index: Optional[int] = None
    finish_reason: Optional[str] = None
    reasoning_content: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


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
    mode: Optional[str] = None
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


# ───────────────────────────────
# 权限决策（借鉴 Claude Code 设计）
# ───────────────────────────────

class PermissionBehavior(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionDecisionReason(BaseModel):
    type: str  # rule | mode | hook | safety_check | path_check | default
    detail: Optional[str] = None
    rule: Optional[dict[str, Any]] = None


class PermissionDecision(BaseModel):
    behavior: PermissionBehavior
    message: str = ""
    decision_reason: Optional[PermissionDecisionReason] = None
    suggestions: Optional[list[dict[str, Any]]] = None


class PermissionRule(BaseModel):
    tool_name: str
    rule_content: Optional[str] = None
    behavior: PermissionBehavior = PermissionBehavior.ASK


class FileOperationType(str, Enum):
    READ = "read"
    WRITE = "write"
    CREATE = "create"


class ContextComponent(BaseModel):
    type: str
    tokens: int = 0
    content: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderUsage(BaseModel):
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


class TurnCompactionUsage(BaseModel):
    micro_compact_applied: bool = False
    full_compact_applied: bool = False
    summary_block_present: bool = False
    full_compact_scope: Optional[str] = None
    recent_token_budget: int = 0


class TurnUsage(BaseModel):
    estimated_prompt_tokens: int = 0
    actual_prompt_tokens: Optional[int] = None
    actual_completion_tokens: Optional[int] = None
    actual_total_tokens: Optional[int] = None
    context_limit: int = 0
    utilization_ratio: float = 0.0
    is_estimated: bool = True
    compaction: TurnCompactionUsage = Field(default_factory=TurnCompactionUsage)

    def with_provider_usage(self, usage: ProviderUsage | None) -> "TurnUsage":
        if usage is None:
            return self.model_copy(deep=True)
        has_actual_usage = any(
            value is not None
            for value in (
                usage.prompt_tokens,
                usage.completion_tokens,
                usage.total_tokens,
            )
        )
        return self.model_copy(
            update={
                "actual_prompt_tokens": usage.prompt_tokens,
                "actual_completion_tokens": usage.completion_tokens,
                "actual_total_tokens": usage.total_tokens,
                "is_estimated": not has_actual_usage,
            }
        )


# ───────────────────────────────
# Agent 事件（参照 pi-mono 设计）
# ───────────────────────────────

class AgentEventType(str, Enum):
    AGENT_START = "agent_start"
    AGENT_END = "agent_end"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    MESSAGE_START = "message_start"
    MESSAGE_UPDATE = "message_update"
    MESSAGE_END = "message_end"
    TOOL_EXECUTION_START = "tool_execution_start"
    TOOL_EXECUTION_END = "tool_execution_end"


class AgentEvent(BaseModel):
    type: AgentEventType
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    session_id: Optional[str] = None


# ───────────────────────────────
# Agent 全局状态快照（参照 pi-mono 设计）
# ───────────────────────────────

class ResilienceConfig(BaseModel):
    """韧性配置：重试、兜底与工具输入校验。"""
    # Provider 层
    provider_fallback_chain: list[str] = Field(default_factory=list)
    provider_retry_max_attempts: int = 3
    provider_retry_backoff_base: float = 2.0
    provider_retry_max_delay: float = 30.0
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_window_seconds: int = 60
    circuit_breaker_recovery_timeout: int = 30

    # Agent Loop 层
    turn_retry_max_attempts: int = 2
    auto_compress_on_context_overflow: bool = True
    react_turns_before_chat_fallback: int = 2

    # Tool 层
    tool_validation_enabled: bool = True
    tool_max_validation_history_groups: int = 3   # 同一工具 error 保留组数
    tool_failure_window_turns: int = 5            # 滑动窗口 turn 数
    tool_failure_threshold: int = 3               # 窗口内失败次数阈值
    tool_default_timeout: int = 60
    tool_parallel_execution: bool = True          # 同一轮多个 tool_call 是否并行执行(asyncio.gather)

    # 【新增】工具执行重试配置
    max_tool_retries: int = 1                     # 工具执行失败后的重试次数（0 = 不重试）
    tool_retry_base_delay: float = 1.0            # 工具重试基础延迟（秒），指数退避
    tool_retry_max_delay: float = 5.0             # 工具重试最大延迟（秒）

    # 【新增】Chat-Only 自动恢复
    chat_only_recovery_turns: int = 3             # 连续多少个无工具调用的成功 turn 后退出 chat-only 模式（0 = 永不自动恢复）


class AgentStateSnapshot(BaseModel):
    """Agent 在某一时刻的完整状态快照，用于持久化和回放。"""
    snapshot_id: str = Field(default_factory=lambda: f"snap-{uuid.uuid4().hex[:8]}")
    session_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    system_prompt: str = ""
    messages: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    turn_count: int = 0
    pending_tool_calls: list[str] = Field(default_factory=list)
    error_message: Optional[str] = None


# ───────────────────────────────
# 韧性层错误分类
# ───────────────────────────────

class ResilienceError(Exception):
    """韧性层错误基类。"""
    pass


class RetryableError(ResilienceError):
    """可重试：网络抖动、限流、超时、服务端不可用。"""
    pass


class ContextLengthError(RetryableError):
    """上下文过长：可重试 + 需触发上下文压缩。"""
    pass


class ValidationError(ResilienceError):
    """参数校验失败：不重试，回流给 LLM 自纠正。"""
    pass


class ToolBannedError(ResilienceError):
    """工具被临时禁用：不重试，回流给 LLM 决策。"""
    pass


class AuthError(ResilienceError):
    """认证/授权错误：不可重试。"""
    pass


class InvalidRequestError(ResilienceError):
    """请求格式错误：不可重试。"""
    pass


class ServiceUnavailable(ResilienceError):
    """服务端不可用：可重试 + 可触发熔断。"""
    pass
