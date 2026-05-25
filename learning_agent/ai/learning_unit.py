"""学习卷（LearningUnit）数据模型。

对应设计文档 [design-modes-refactor-chat-vs-learning-unit.md](../../docs/design/design-modes-refactor-chat-vs-learning-unit.md) §3.8。

命名说明：模块内的卷内目标模型叫 `UnitObjective`，与
`learning_agent/ai/models.py` 中独立的 `LearningObjective`（用于 /objectives
端点的顶层目标实体）刻意区分；MVP 阶段两者不互相引用。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


LearningUnitPhase = Literal[
    "aligning",
    "absorbing",
    "outputting",
    "consolidated",
]


ConceptStatus = Literal["new", "covered", "needs_review"]


ObjectiveSource = Literal[
    "ai_distilled",
    "user_written",
    "material_imported",
    "promoted_from_chat",
]


VerificationStatus = Literal["passed", "skipped"]


TeachSessionState = Literal[
    "generating",
    "queued",
    "prompted",
    "evaluating",
    "passed",
    "needs_review",
]


QuestionKind = Literal["mc", "sa"]


QuestionVerdict = Literal["passed", "needs_review"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UnitObjective(BaseModel):
    """学习卷内部的一句话目标（与独立 LearningObjective 实体不冲突）。"""

    text: str
    source: ObjectiveSource = "ai_distilled"
    source_ref: Optional[str] = None
    confirmed: bool = False


class ConceptItem(BaseModel):
    id: str = Field(default_factory=lambda: f"cpt-{uuid.uuid4().hex[:8]}")
    name: str
    summary: str
    examples: list[str] = Field(default_factory=list)
    relevance: float = 1.0
    status: ConceptStatus = "new"


class TangentNote(BaseModel):
    id: str = Field(default_factory=lambda: f"tng-{uuid.uuid4().hex[:8]}")
    name: str
    summary: str
    relevance: float = 0.0
    detected_at: datetime = Field(default_factory=_now)


class TeachQuestion(BaseModel):
    id: str = Field(default_factory=lambda: f"tq-{uuid.uuid4().hex[:8]}")
    concept_id: str
    kind: QuestionKind
    stem: str

    choices: Optional[list[str]] = None
    correct_index: Optional[int] = None

    rubric: Optional[str] = None

    user_answer: Optional[str] = None
    verdict: Optional[QuestionVerdict] = None
    judge_reason: Optional[str] = None


class TeachSession(BaseModel):
    id: str = Field(default_factory=lambda: f"ts-{uuid.uuid4().hex[:8]}")
    questions: list[TeachQuestion] = Field(default_factory=list)
    current_index: int = 0
    state: TeachSessionState = "generating"
    aggregate_passed: Optional[bool] = None
    created_at: datetime = Field(default_factory=_now)
    completed_at: Optional[datetime] = None


_PHASE_ORDER: tuple[LearningUnitPhase, ...] = (
    "aligning",
    "absorbing",
    "outputting",
    "consolidated",
)


_ALLOWED_TRANSITIONS: dict[LearningUnitPhase, frozenset[LearningUnitPhase]] = {
    "aligning": frozenset({"absorbing"}),
    # outputting 是必经的，但允许 TEACH 失败时回退到 absorbing（C5/§3.4）
    "absorbing": frozenset({"outputting"}),
    "outputting": frozenset({"absorbing", "consolidated"}),
    "consolidated": frozenset(),  # 终态
}


class LearningUnit(BaseModel):
    """学习卷顶层容器。所有运行时状态必须落库以满足 C9。"""

    id: str = Field(default_factory=lambda: f"lu-{uuid.uuid4().hex[:8]}")
    session_id: str
    objective: UnitObjective
    phase: LearningUnitPhase = "aligning"
    concept_list: list[ConceptItem] = Field(default_factory=list)
    tangent_notes: list[TangentNote] = Field(default_factory=list)
    aligning_round: int = 1
    teach_session: Optional[TeachSession] = None
    verification_status: Optional[VerificationStatus] = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    def can_transition_to(self, target: LearningUnitPhase) -> bool:
        return target in _ALLOWED_TRANSITIONS[self.phase]

    def transition_to(self, target: LearningUnitPhase) -> None:
        if not self.can_transition_to(target):
            raise ValueError(
                f"Illegal phase transition: {self.phase} -> {target}"
            )
        self.phase = target
        self.updated_at = _now()

    def effective_mode(self) -> str:
        """phase 到 AgentMode 字符串值的映射；consolidated 无效返回 None 由调用方拒绝。"""
        if self.phase == "aligning":
            return "ask"
        if self.phase == "absorbing":
            return "chat"
        if self.phase == "outputting":
            return "teach"
        return ""

    def is_terminal(self) -> bool:
        return self.phase == "consolidated"


__all__ = [
    "LearningUnit",
    "LearningUnitPhase",
    "UnitObjective",
    "ConceptItem",
    "ConceptStatus",
    "ObjectiveSource",
    "TangentNote",
    "TeachQuestion",
    "TeachSession",
    "TeachSessionState",
    "QuestionKind",
    "QuestionVerdict",
    "VerificationStatus",
]
