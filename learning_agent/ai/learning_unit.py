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
    "absorbing",
    "outputting",
    "consolidated",
]


AlignmentState = Literal[
    "idle",
    "suggested",
    "active",
    "resolved",
    "skipped",
]


ObjectiveStatus = Literal[
    "working",
    "refined",
    "confirmed",
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
    "absorbing",
    "outputting",
    "consolidated",
)


_ALLOWED_TRANSITIONS: dict[LearningUnitPhase, frozenset[LearningUnitPhase]] = {
    # 主链：absorbing -> outputting -> consolidated；TEACH 失败时可回退到 absorbing。
    # aligning 已从主链移除（adaptive alignment §8.1）；对齐降级为 alignment_state 旁路。
    "absorbing": frozenset({"outputting"}),
    "outputting": frozenset({"absorbing", "consolidated"}),
    "consolidated": frozenset(),  # 终态
}


class LearningUnit(BaseModel):
    """学习卷顶层容器。所有运行时状态必须落库以满足 C9。"""

    id: str = Field(default_factory=lambda: f"lu-{uuid.uuid4().hex[:8]}")
    session_id: str
    objective: UnitObjective
    phase: LearningUnitPhase = "absorbing"
    concept_list: list[ConceptItem] = Field(default_factory=list)
    tangent_notes: list[TangentNote] = Field(default_factory=list)

    # ── adaptive alignment 旁路状态（§8.2）─────────────────────────
    # 对齐不再是主阶段，而是横切能力；以下字段刻画当前轮是否需要触发 ASK
    # 协议，以及目标的成熟度。Product 层在 _prepare_learning_unit_turn 中
    # 读写，Runtime 不感知。
    alignment_state: AlignmentState = "idle"
    objective_status: ObjectiveStatus = "working"
    assumption_note: str = ""
    alignment_reason: str = ""
    clarification_count: int = 0
    # 已经向用户暴露过的"非阻塞建议"次数。§9.3 #2 限流：每个 unit
    # 启动期最多 2 条 suggested 建议，超过即降级为 none，避免反复唠叨。
    suggestion_count: int = 0
    # "先按这个学"冷静期剩余轮数。§9.3 #3 限流：用户主动 accept_assumption
    # 后，N 轮内不再弹建议条；每个 absorbing 轮进入时减 1，归零后恢复。
    nag_cooldown_remaining: int = 0
    last_alignment_at: Optional[datetime] = None

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
        """phase 到 AgentMode 字符串值的映射；consolidated 无效返回空串由调用方拒绝。

        注意：是否在 absorbing 中临时覆写为 ASK，由 Product 层根据
        ``alignment_state`` 决定（adaptive alignment §9.1），不在本方法范围。
        """
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
    "AlignmentState",
    "ObjectiveStatus",
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
