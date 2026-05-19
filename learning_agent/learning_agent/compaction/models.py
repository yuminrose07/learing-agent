from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


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
    compact_anchor_entry_id: Optional[str] = None
    last_cut_point_entry_id: Optional[str] = None
    last_compact_scope: Optional[str] = None
    incremental_count_since_rebase: int = 0
    consecutive_failures: int = 0
    last_summary_file: Optional[str] = None
    last_summary_hash: Optional[str] = None
    last_recent_token_budget: int = 0
    last_compacted_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompactMetadata":
        return cls(**data)


@dataclass
class CompactSourceUnit:
    unit_id: str
    entry_ids: list[str] = field(default_factory=list)
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
    scope: str
    compact_anchor_entry_id: Optional[str]
    cut_point_entry_id: str
    recent_token_budget: int
    source_units: list[CompactSourceUnit] = field(default_factory=list)
    existing_summary: Optional[str] = None
    sm_state: Optional[SessionMemoryState] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "scope": self.scope,
            "compact_anchor_entry_id": self.compact_anchor_entry_id,
            "cut_point_entry_id": self.cut_point_entry_id,
            "recent_token_budget": self.recent_token_budget,
            "source_units": [unit.to_dict() for unit in self.source_units],
            "existing_summary": self.existing_summary,
            "sm_state": self.sm_state.to_dict() if self.sm_state else None,
        }


@dataclass
class FullCompactResult:
    session_id: str
    scope: str
    summary_text: str
    compact_anchor_entry_id: Optional[str]
    cut_point_entry_id: str
    next_anchor_entry_id: Optional[str]
    preserved_entry_ids: list[str] = field(default_factory=list)
    trace_summary: CompactTraceSummary = field(default_factory=CompactTraceSummary)
    estimated_tokens_after: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "scope": self.scope,
            "summary_text": self.summary_text,
            "compact_anchor_entry_id": self.compact_anchor_entry_id,
            "cut_point_entry_id": self.cut_point_entry_id,
            "next_anchor_entry_id": self.next_anchor_entry_id,
            "preserved_entry_ids": list(self.preserved_entry_ids),
            "trace_summary": self.trace_summary.to_dict(),
            "estimated_tokens_after": self.estimated_tokens_after,
        }


@dataclass
class CompactionPlan:
    use_micro_compact: bool = True
    use_full_compact: bool = False
    full_compact_scope: Optional[str] = None
    compact_anchor_entry_id: Optional[str] = None
    cut_point_entry_id: Optional[str] = None
    recent_token_budget: int = 0
    summary_block: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CompactionPlan | None":
        if data is None:
            return None
        return cls(**data)
