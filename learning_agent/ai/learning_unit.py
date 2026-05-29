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
    "stopped",
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


ForgeStage = Literal["entry", "collision", "forge", "fixed", "cooling"]

TemperatureState = Literal[
    "steady",
    "needs_example",
    "needs_structure",
    "resisting_output",
    "fatigued",
    "small_step_crossed",
]


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


class Candidate(BaseModel):
    """LLM 对齐分类器列出的「导向不同 first_step」的可能解读。

    ``objective`` 是一句给用户挑选用的目标复述，``first_step`` 是配套的
    可操作切入点，用于前端 modal 卡片的标题 / 副标题。

    定义在此模块（``ai`` 层）而非 ``learning_agent`` 层是为了让 ``LearningUnit``
    可以把它当作字段直接序列化；``learning_agent.alignment_policy.Candidate``
    会从这里 re-export，对外接口不变。
    """

    objective: str
    first_step: str


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


class TeachFeedbackCard(BaseModel):
    """consolidated 阶段的"掌握度"反馈卡片（task breakdown B5 §4）。

    三块固定字段供 F5 反馈卡片直接渲染：
    - ``mastered``：本卷已经掌握的概念短语（≤ concept_list.name 集合）
    - ``gaps``：仍有空缺、需要补强的概念短语
    - ``next_topic_suggestion``：基于 gaps 给出的一句话下一步建议
    所有字段都默认为空，便于"卷尚未完成评判"时的兜底渲染。
    """

    mastered: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    next_topic_suggestion: str = ""


_PHASE_ORDER: tuple[LearningUnitPhase, ...] = (
    "absorbing",
    "outputting",
    "consolidated",
    "stopped",
)


_ALLOWED_TRANSITIONS: dict[LearningUnitPhase, frozenset[LearningUnitPhase]] = {
    # 主链：absorbing -> outputting -> consolidated；TEACH 失败时可回退到 absorbing。
    # 用户也可以显式 "先学到这里"，进入 stopped 终态并释放单卷互斥。
    # aligning 已从主链移除（adaptive alignment §8.1）；对齐降级为 alignment_state 旁路。
    "absorbing": frozenset({"outputting", "stopped"}),
    "outputting": frozenset({"absorbing", "consolidated", "stopped"}),
    "consolidated": frozenset(),  # 终态
    "stopped": frozenset(),  # 用户显式中止终态，不计入完成率
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
    # LLM 对齐分类器最近一次列出的候选解读。前端在用户刷新页面时
    # 仍能恢复 modal；non-active 档也会写入，便于观测分类器输出质量。
    last_candidates: list[Candidate] = Field(default_factory=list)

    teach_session: Optional[TeachSession] = None
    verification_status: Optional[VerificationStatus] = None
    # B5: consolidated 阶段写入；UI 用作"掌握度反馈卡"。卷未完成时保持 None。
    feedback_card: Optional[TeachFeedbackCard] = None
    # 用户显式 "先学到这里" 时写入。stopped 是终态，但不代表完成验收。
    stop_reason: Optional[str] = None
    stopped_at: Optional[datetime] = None
    # M1：本卷首条 absorbing 阶段 assistant 消息成功 finalize 的时间戳，用作
    # ``learning_unit.first_value_delivered`` 事件的 once-only 守卫与
    # "首个学习价值时间 (TTFV)" 指标的 t0。None = 尚未投出首条价值。
    first_value_delivered_at: Optional[datetime] = None
    # ── Phase 1A 铸造状态骨架 ─────────────────────────────────────
    forge_stage: ForgeStage = "entry"
    temperature_state: TemperatureState = "steady"
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
        """phase 到 AgentMode 字符串值的映射；终态无效返回空串由调用方拒绝。

        注意：是否在 absorbing 中临时覆写为 ASK，由 Product 层根据
        ``alignment_state`` 决定（adaptive alignment §9.1），不在本方法范围。
        """
        if self.phase == "absorbing":
            return "study"
        if self.phase == "outputting":
            return "teach"
        return ""

    def is_terminal(self) -> bool:
        return self.phase in {"consolidated", "stopped"}


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
    "TeachFeedbackCard",
    "QuestionKind",
    "QuestionVerdict",
    "VerificationStatus",
    "ForgeStage",
    "TemperatureState",
]
