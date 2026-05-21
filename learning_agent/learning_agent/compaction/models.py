from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class CompactMode(str, Enum):
    AUTO_PREFIX = "auto_prefix"
    SLACT_FULL = "slact_full"
    SLACT_FROM = "slact_from"
    SLACT_UP_TO = "slact_up_to"


class CompactScope(str, Enum):
    FULL = "full"
    INCREMENTAL = "incremental"
    REBASE = "rebase"


@dataclass
class JsonlCursor:
    session_id: str
    seq: int | None = None
    event_id: str | None = None
    line_no: int | None = None
    byte_offset: int | None = None
    last_entry_id: str | None = None
    last_delta_ts: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JsonlCursor":
        allowed = cls.__dataclass_fields__.keys()
        if "after_seq" in data and "seq" not in data:
            data = {**data, "seq": data["after_seq"]}
        return cls(**{key: value for key, value in data.items() if key in allowed})


@dataclass
class SessionMemoryState:
    session_id: str
    objective: str = ""
    mode: str = "chat"
    current_subtask: str = ""
    confirmed_facts: list[str] = field(default_factory=list)
    key_decisions: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    key_files: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionMemoryState":
        return cls(**data)


@dataclass
class CompactMetadata:
    session_id: str
    last_compact_event_id: Optional[str] = None
    previous_compact_event_id: Optional[str] = None
    compact_anchor_entry_id: Optional[str] = None
    compact_anchor_event_seq: Optional[int] = None
    last_cut_point_entry_id: Optional[str] = None
    last_compact_mode: Optional[str] = None
    last_compact_scope: Optional[str] = None
    source_event_start_seq: Optional[int] = None
    source_event_end_seq: Optional[int] = None
    last_source_snapshot_seq: Optional[int] = None
    source_event_ids: list[str] = field(default_factory=list)
    retained_event_ids: list[str] = field(default_factory=list)
    last_source_entry_ids: list[str] = field(default_factory=list)
    last_retained_entry_ids: list[str] = field(default_factory=list)
    last_source_jsonl_cursor: Optional[str] = None
    next_jsonl_cursor: Optional[int | str] = None
    template_version: str = "compact-summary-v1"
    schema_version: str = "v1"
    incremental_count_since_rebase: int = 0
    consecutive_failures: int = 0
    last_summary_file: Optional[str] = None
    last_summary_artifact_ref: Optional[str] = None
    last_summary_hash: Optional[str] = None
    last_recent_token_budget: int = 0
    last_jsonl_path: Optional[str] = None
    last_prompt_template: Optional[str] = None
    last_compacted_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompactMetadata":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value for key, value in data.items() if key in allowed})


@dataclass
class CompactSourceUnit:
    unit_id: str
    unit_type: str = "message_round"
    entry_ids: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    source_event_start_seq: Optional[int] = None
    source_event_end_seq: Optional[int] = None
    transcript: str = ""
    estimated_tokens: int = 0
    started_at: Optional[str] = None
    ended_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CompactTraceSummary:
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    searches: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FullCompactInput:
    session_id: str
    compact_mode: str
    scope: str
    compact_anchor_entry_id: Optional[str]
    cut_point_entry_id: str
    recent_token_budget: int
    summary_position: str = "before_retained"
    source_event_start_seq: Optional[int] = None
    source_event_end_seq: Optional[int] = None
    source_snapshot_seq: Optional[int] = None
    source_event_ids: list[str] = field(default_factory=list)
    source_entry_ids: list[str] = field(default_factory=list)
    retained_entry_ids: list[str] = field(default_factory=list)
    source_units: list[CompactSourceUnit] = field(default_factory=list)
    existing_summary: Optional[str] = None
    sm_state: Optional[SessionMemoryState] = None
    previous_compact_event_id: Optional[str] = None
    current_user_event_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "compact_mode": self.compact_mode,
            "scope": self.scope,
            "compact_anchor_entry_id": self.compact_anchor_entry_id,
            "cut_point_entry_id": self.cut_point_entry_id,
            "recent_token_budget": self.recent_token_budget,
            "summary_position": self.summary_position,
            "source_event_start_seq": self.source_event_start_seq,
            "source_event_end_seq": self.source_event_end_seq,
            "source_snapshot_seq": self.source_snapshot_seq,
            "source_event_ids": list(self.source_event_ids),
            "source_entry_ids": list(self.source_entry_ids),
            "retained_entry_ids": list(self.retained_entry_ids),
            "source_units": [unit.to_dict() for unit in self.source_units],
            "existing_summary": self.existing_summary,
            "sm_state": self.sm_state.to_dict() if self.sm_state else None,
            "previous_compact_event_id": self.previous_compact_event_id,
            "current_user_event_id": self.current_user_event_id,
        }


@dataclass
class FullCompactResult:
    session_id: str
    compact_mode: str
    scope: str
    summary_text: str
    compact_anchor_entry_id: Optional[str]
    cut_point_entry_id: str
    next_anchor_entry_id: Optional[str]
    summary_hash: Optional[str] = None
    compact_event_id: Optional[str] = None
    compact_event_seq: Optional[int] = None
    previous_compact_event_id: Optional[str] = None
    current_user_event_id: Optional[str] = None
    source_event_start_seq: Optional[int] = None
    source_event_end_seq: Optional[int] = None
    source_snapshot_seq: Optional[int] = None
    source_event_ids: list[str] = field(default_factory=list)
    source_entry_ids: list[str] = field(default_factory=list)
    retained_event_ids: list[str] = field(default_factory=list)
    retained_entry_ids: list[str] = field(default_factory=list)
    template_version: str = "compact-summary-v1"
    validation_status: dict[str, Any] = field(default_factory=dict)
    preserved_entry_ids: list[str] = field(default_factory=list)
    trace_summary: CompactTraceSummary = field(default_factory=CompactTraceSummary)
    estimated_tokens_after: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "compact_mode": self.compact_mode,
            "scope": self.scope,
            "summary_text": self.summary_text,
            "compact_anchor_entry_id": self.compact_anchor_entry_id,
            "cut_point_entry_id": self.cut_point_entry_id,
            "next_anchor_entry_id": self.next_anchor_entry_id,
            "summary_hash": self.summary_hash,
            "compact_event_id": self.compact_event_id,
            "compact_event_seq": self.compact_event_seq,
            "previous_compact_event_id": self.previous_compact_event_id,
            "current_user_event_id": self.current_user_event_id,
            "source_event_start_seq": self.source_event_start_seq,
            "source_event_end_seq": self.source_event_end_seq,
            "source_snapshot_seq": self.source_snapshot_seq,
            "source_event_ids": list(self.source_event_ids),
            "source_entry_ids": list(self.source_entry_ids),
            "retained_event_ids": list(self.retained_event_ids),
            "retained_entry_ids": list(self.retained_entry_ids),
            "template_version": self.template_version,
            "validation_status": dict(self.validation_status),
            "preserved_entry_ids": list(self.preserved_entry_ids),
            "trace_summary": self.trace_summary.to_dict(),
            "estimated_tokens_after": self.estimated_tokens_after,
        }


@dataclass
class CompactionPlan:
    use_micro_compact: bool = False
    use_full_compact: bool = False
    compact_mode: Optional[str] = None
    compact_scope: Optional[str] = None
    full_compact_scope: Optional[str] = None
    compact_event_id: Optional[str] = None
    compact_event_seq: Optional[int] = None
    previous_compact_event_id: Optional[str] = None
    current_user_event_id: Optional[str] = None
    turn_id: Optional[str] = None
    run_id: Optional[str] = None
    source_snapshot_seq: Optional[int] = None
    compact_request_event_id: Optional[str] = None
    compact_anchor_entry_id: Optional[str] = None
    cut_point_entry_id: Optional[str] = None
    source_event_range: tuple[int, int] | None = None
    source_entry_ids: list[str] = field(default_factory=list)
    retained_entry_ids: list[str] = field(default_factory=list)
    retained_position: str = "after_summary"
    pivot_entry_id: Optional[str] = None
    source_jsonl_cursor: Optional[str] = None
    next_jsonl_cursor: Optional[int | str] = None
    recent_token_budget: int = 0
    summary_block: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CompactionPlan | None":
        if data is None:
            return None
        return cls(**data)
