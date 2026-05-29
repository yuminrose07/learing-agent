"""自适应对齐策略（adaptive alignment §5.3 / §9.2）。

判定主体已迁移到 ``alignment_classifier.AlignmentClassifier``（LLM 驱动）。
本模块只保留：
- 共享 schema：``AlignmentDecision`` / ``AlignmentMode`` / ``AlignmentReason``
  / ``Candidate``；
- 限流工具：``_apply_rate_limits``（B 档建议 cooldown）；
- 副作用编排：``apply_alignment_decision``（unit 写回 + 事件发射）；
- absorbing 首轮 system prompt 增量：``build_absorbing_opening_addendum``。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Literal, Optional

from pydantic import BaseModel

from learning_agent.ai import AgentMode, MessageRole
from learning_agent.ai.learning_unit import Candidate, LearningUnit
from learning_agent.learning_agent.session_events import SessionEventType

if TYPE_CHECKING:
    from learning_agent.ai import LearningSession
    from learning_agent.learning_agent.learning_unit_store import LearningUnitStore


logger = logging.getLogger(__name__)

AlignmentMode = Literal["none", "suggested", "active"]
AlignmentReason = Literal[
    "clear_enough",
    "too_broad",
    "goal_drift",
    "conflicting_scope",
    "missing_learnable_target",
    # 用户主动触发的对齐（POST /align）。policy 不会自己产出这个原因，
    # 仅作为 Product 层调用 _apply_alignment_decision 时的合法值。
    "user_request",
]


class AlignmentDecision(BaseModel):
    """对齐分类器的判定结果。

    - ``mode``: 三档输出
        - ``none``: A 档，直接进 absorbing
        - ``suggested``: B 档，进 absorbing 但 UI 给非阻塞建议条
        - ``active``: C 档，前端弹出 modal 让用户选方向（不再切到 ASK 轮）
    - ``reason``: 触发档位的原因，进事件 payload 供观测
    - ``suggested_objective``: B 档时给 UI 显示的「建议收窄成 X」文本
    - ``assumption_note``: A 档「系统替你假设」或 B 档「按 X 切口」的简短说明
    - ``candidates``: LLM 列出的可选方向（active 档至少 2 个；其它档可为空）
    - ``divergence_cost``: 选错的代价；``high`` → modal，``low`` → 软建议
    """

    mode: AlignmentMode
    reason: AlignmentReason
    suggested_objective: str = ""
    assumption_note: str = ""
    candidates: list[Candidate] = []
    divergence_cost: Optional[Literal["low", "high"]] = None


# §9.3 #2：每个 unit 启动期最多弹 ``MAX_SUGGESTIONS_PER_UNIT`` 条非阻塞建议；
# 超过后即使 policy 仍判 B 档也会被静默降级为 A 档，避免反复唠叨。
MAX_SUGGESTIONS_PER_UNIT = 2

# §9.3 #3：用户点过「先按这个学」之后，本卷接下来 N 个 absorbing turn 不再
# 主动弹收窄建议。每个 absorbing turn 入口减 1，归零后恢复。
COOLDOWN_AFTER_ACCEPT_ASSUMPTION = 3


def _apply_rate_limits(
    unit: LearningUnit, decision: AlignmentDecision
) -> AlignmentDecision:
    """§9.3 #2 / #3 限流：suggested 命中上限或冷静期内静默降为 none。

    - #2 ``suggestion_count >= MAX_SUGGESTIONS_PER_UNIT``：单卷已弹过 2 条建议，
      不再让 UI 出第三条；目标已经被「宽」了两次，再弹只是噪声。
    - #3 ``nag_cooldown_remaining > 0``：用户已经按下「先按这个学」，N 轮内豁免建议。
    其它情形（active、none）保持原状 —— C 档由 ``clarification_count`` 在调度器
    侧单独限流，A 档本就不打扰。
    """
    if decision.mode != "suggested":
        return decision
    if unit.suggestion_count >= MAX_SUGGESTIONS_PER_UNIT:
        return AlignmentDecision(mode="none", reason="clear_enough")
    if unit.nag_cooldown_remaining > 0:
        return AlignmentDecision(mode="none", reason="clear_enough")
    return decision


# ---------------------------------------------------------------------------
# 注入式 advance（带副作用）
#
# 上面是「纯 plan 派生」逻辑：无 I/O、无事件、可单测。
# 下面是「副作用编排」：把 store/事件回调由调用方注入，本模块只负责
# 守卫判定与状态推进的纯逻辑组合，与 forge_policy 同 pattern。
# ---------------------------------------------------------------------------


ABSORBING_OPENING_TEMPLATE = """\
本轮是这个学习卷的首轮回答。请严格按以下结构输出，先教再建议：

1. 工作目标卡片（一句话）
   开头复述："我先按这个目标带你学：<对学习目标的简短复述>"
   学习目标原文：{objective}

2. 学习地图（3-5 个 bullet）
   列出本卷会涵盖的模块 / 概念 / 学习顺序。

3. 第一段实质讲解
   从地图的第一项切口开始，直接给一段有内容的解释——不要只是大纲。
{suggestion_block}\
不要先反问、不要先要求确认。"""

ABSORBING_OPENING_SUGGESTION_BLOCK = """
4. 收窄建议（仅本轮）
   在最末尾附一句："如果你想更聚焦，我可以帮你收窄成 <更具体的方向>"。
"""


def build_absorbing_opening_addendum(
    session: "LearningSession",
    unit: LearningUnit,
    decision: Optional[AlignmentDecision],
    effective_mode: AgentMode,
) -> Optional[str]:
    """absorbing 首轮的 system prompt 增量（adaptive alignment §6.1 / §6.2）。

    触发条件：absorbing + STUDY + 本卷此前没有过 assistant 回答。
    B 档（suggested）追加"收窄建议"段；A 档不附加。C 档走 ASK 不进这里。
    """
    if effective_mode != AgentMode.STUDY or unit.phase != "absorbing":
        return None
    if any(e.role == MessageRole.ASSISTANT for e in session.entries):
        return None
    suggestion_block = (
        ABSORBING_OPENING_SUGGESTION_BLOCK
        if decision is not None and decision.mode == "suggested"
        else ""
    )
    return ABSORBING_OPENING_TEMPLATE.format(
        objective=unit.objective.text.strip() or "（待定）",
        suggestion_block=suggestion_block,
    )


async def apply_alignment_decision(
    *,
    store: "LearningUnitStore",
    emit_unit_event: Callable[..., None],
    unit: LearningUnit,
    decision: AlignmentDecision,
) -> None:
    """把策略结果写回 unit；同时维护 §9.3 #2/#3 的持久化计数器。

    ``alignment_state`` 映射：``none → idle`` / ``suggested → suggested`` /
    ``active → active``。``user_request`` 是一次性消费：本轮以 active 表达，
    但 apply 时立刻转入 ``resolved``，不参与 §9.3 #1 限流计数。

    计数器：
    - ``active`` 且非 ``user_request``：``clarification_count += 1`` (§9.3 #1)
    - ``suggested``：``suggestion_count += 1`` (§9.3 #2)
    - 每次进入此方法（一次 absorbing turn 入口）：``nag_cooldown_remaining``
      若大于 0 则减 1 (§9.3 #3)。冷静期与策略判断结果无关，是绝对回合数。

    副作用（持久化 + 事件发射）由注入的 ``store`` 与 ``emit_unit_event`` 承担。
    """
    state_map = {"none": "idle", "suggested": "suggested", "active": "active"}
    new_state = state_map[decision.mode]
    is_user_request = decision.reason == "user_request"
    if is_user_request:
        # 用户主动触发的对齐本轮即被消费，下一轮回归 heuristic
        new_state = "resolved"
    will_bump_clarification = decision.mode == "active" and not is_user_request
    will_bump_suggestion = decision.mode == "suggested"

    async with store.lock(unit.id):
        latest = store.get(unit.id) or unit
        latest.alignment_state = new_state
        latest.alignment_reason = decision.reason
        latest.assumption_note = decision.assumption_note
        # 把分类器列出的候选解读同步进 unit，便于页面刷新后恢复 modal、
        # 以及离线观测分类器输出质量。decision.candidates 可能为空。
        latest.last_candidates = list(decision.candidates)
        if will_bump_clarification:
            latest.clarification_count += 1
            latest.last_alignment_at = datetime.now(timezone.utc)
        if will_bump_suggestion:
            latest.suggestion_count += 1
            latest.last_alignment_at = datetime.now(timezone.utc)
        if latest.nag_cooldown_remaining > 0:
            latest.nag_cooldown_remaining -= 1
        store.save(latest)
        # caller 持有的 unit 与 store 缓存指向同一对象，确保元数据立刻可见
        if latest is not unit:
            unit.alignment_state = latest.alignment_state
            unit.alignment_reason = latest.alignment_reason
            unit.assumption_note = latest.assumption_note
            unit.clarification_count = latest.clarification_count
            unit.suggestion_count = latest.suggestion_count
            unit.nag_cooldown_remaining = latest.nag_cooldown_remaining
            unit.last_alignment_at = latest.last_alignment_at
            unit.last_candidates = latest.last_candidates

    # M1：把策略结果翻译成产品事件。idle 静默；user_request 在本轮被消费
    # 为 resolved，发 RESOLVED；suggested 发 SUGGESTED；其余 active 发 STARTED。
    if is_user_request:
        emit_unit_event(
            latest,
            SessionEventType.LEARNING_UNIT_ALIGNMENT_RESOLVED,
            extra={"trigger": "user_request_consumed"},
        )
    elif decision.mode == "suggested":
        emit_unit_event(
            latest,
            SessionEventType.LEARNING_UNIT_ALIGNMENT_SUGGESTED,
            extra={
                "suggested_objective": decision.suggested_objective or "",
                "suggestion_count": latest.suggestion_count,
            },
        )
    elif decision.mode == "active":
        emit_unit_event(
            latest,
            SessionEventType.LEARNING_UNIT_ALIGNMENT_STARTED,
            extra={
                "trigger": decision.reason or "policy",
                "clarification_count": latest.clarification_count,
            },
        )


__all__ = [
    "AlignmentDecision",
    "AlignmentMode",
    "AlignmentReason",
    "Candidate",
    "MAX_SUGGESTIONS_PER_UNIT",
    "COOLDOWN_AFTER_ACCEPT_ASSUMPTION",
    "ABSORBING_OPENING_TEMPLATE",
    "ABSORBING_OPENING_SUGGESTION_BLOCK",
    "build_absorbing_opening_addendum",
    "apply_alignment_decision",
]
