"""TEACH outputting 阶段问答流。

从 ``LearningAgentSystem`` 抽出的"判题 + 渲染下一题 / 反馈卡"子系统：

- 模块级纯函数：``finalize_consolidation`` / ``render_feedback_card`` /
  ``teach_metadata``，零 ``self`` 依赖、可独立单测。
- ``TeachFlow``：协作者类，构造时注入 ``learning_unit_store`` /
  ``session_manager`` / ``teach_judge`` 与 ``emit_unit_event`` 回调，提供
  ``stream_teach_answer_flow`` / ``record_teach_turn`` 两个入口。

``LearningAgentSystem`` 仍持有 ``advance_learning_unit`` 与
``_start_teach_session`` 两个边界——前者是公共 API，后者依赖
``self.teach_generator`` 并被多处测试 monkeypatch，下沉到本模块会破坏穿透。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable, Optional

from learning_agent.ai import AgentMode, ChatChunk, MessageRole
from learning_agent.ai.learning_unit import TeachFeedbackCard
from learning_agent.learning_agent.session_events import SessionEventType

if TYPE_CHECKING:
    from learning_agent.ai import LearningSession
    from learning_agent.ai.learning_unit import LearningUnit
    from learning_agent.learning_agent.learning_unit_store import LearningUnitStore
    from learning_agent.learning_agent.session_manager import SessionManager
    from learning_agent.learning_agent.teach_judge import TeachJudge


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 纯函数：渲染与聚合
# ---------------------------------------------------------------------------


def finalize_consolidation(unit: "LearningUnit") -> None:
    """从已答题的 TeachSession 聚合 mastered/gaps 反馈卡，并落进 consolidated。

    - 若 ``unit.teach_session`` 不存在或全空：mastered/gaps 都为空，
      ``next_topic_suggestion`` 走兜底文案。
    - 已经判过的题：``verdict="passed"`` → 概念 name 进 mastered；
      其余进 gaps。
    - 调用方负责后续 ``transition_to("consolidated")`` 与 save。
    """
    concept_name_by_id = {c.id: c.name for c in unit.concept_list}
    mastered: list[str] = []
    gaps: list[str] = []
    seen: set[str] = set()
    if unit.teach_session is not None:
        for q in unit.teach_session.questions:
            name = concept_name_by_id.get(q.concept_id, "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            if q.verdict == "passed":
                mastered.append(name)
            elif q.verdict == "needs_review":
                gaps.append(name)
            # 没判过的（None）忽略，避免误打掌握或空缺标签

    if gaps:
        next_suggestion = (
            f"建议下一卷优先补强：{gaps[0]}。"
        )
    elif mastered:
        next_suggestion = "本卷掌握度良好，可以挑选相邻方向继续深入。"
    else:
        next_suggestion = "本卷未进入评估环节；下次可在 absorbing 中多沉淀几个概念再讲讲看。"

    unit.feedback_card = TeachFeedbackCard(
        mastered=mastered,
        gaps=gaps,
        next_topic_suggestion=next_suggestion,
    )
    # MVP 取舍：verification_status 仅区分"完成评估" vs "跳过评估"两态，
    # 是否全过由 feedback_card.gaps 表达。已为 skipped 的不覆盖。
    if unit.verification_status != "skipped":
        unit.verification_status = "passed"
    if unit.teach_session is not None:
        unit.teach_session.state = (
            "needs_review" if gaps else "passed"
        )
        unit.teach_session.aggregate_passed = not gaps
        unit.teach_session.completed_at = datetime.now(timezone.utc)


def render_feedback_card(unit: "LearningUnit") -> str:
    """把 unit.feedback_card 渲染成一段纯文本，供 chat stream 直接返回。"""
    card = unit.feedback_card
    if card is None:
        return "本卷已收束。"
    lines = ["**本卷已完成评估。**"]
    if card.mastered:
        lines.append("✓ 掌握：" + "、".join(card.mastered))
    if card.gaps:
        lines.append("⚠️ 待补强：" + "、".join(card.gaps))
    if card.next_topic_suggestion:
        lines.append("→ " + card.next_topic_suggestion)
    return "\n".join(lines)


def teach_metadata(
    unit: "LearningUnit",
    *,
    verdict: Optional[str],
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "mode": AgentMode.TEACH.value,
        "learning_unit_id": unit.id,
        "learning_unit_phase": unit.phase,
    }
    if unit.teach_session is not None:
        meta["teach_session_id"] = unit.teach_session.id
        meta["teach_state"] = unit.teach_session.state
        meta["question_index"] = unit.teach_session.current_index
        meta["question_total"] = len(unit.teach_session.questions)
    if verdict is not None:
        meta["verdict"] = verdict
    if unit.feedback_card is not None:
        meta["feedback_card"] = unit.feedback_card.model_dump()
    return meta


# ---------------------------------------------------------------------------
# 协作者类：流式答题 + 写盘
# ---------------------------------------------------------------------------


class TeachFlow:
    """outputting 阶段的判题流编排。

    依赖通过构造注入，便于上层在不暴露内部状态的前提下复用：
    - ``learning_unit_store``：读 / 写 LearningUnit，提供 ``lock`` 上下文。
    - ``session_manager``：写 session.entries（user + assistant 两条）。
    - ``teach_judge``：可空；空时按 ``needs_review`` 兜底。
    - ``emit_unit_event``：注入 ``LearningAgentSystem._emit_unit_event``
      bound method，让 companion recovery 钩子等副作用照常触发。
    """

    def __init__(
        self,
        *,
        learning_unit_store: "LearningUnitStore",
        session_manager: "SessionManager",
        teach_judge: Optional["TeachJudge"],
        emit_unit_event: Callable[..., None],
    ) -> None:
        self.learning_unit_store = learning_unit_store
        self.session_manager = session_manager
        self.teach_judge = teach_judge
        self.emit_unit_event = emit_unit_event

    async def stream_teach_answer_flow(
        self,
        session: "LearningSession",
        unit: "LearningUnit",
        user_input: str,
    ) -> AsyncGenerator[ChatChunk, None]:
        """outputting 阶段：user_input = 当前题答案，judge + 渲染下一题或反馈卡。

        与 ``stream_session_chat`` 默认 agent_loop 路径不同，此函数：
        - 不走 provider.chat / agent_loop，节省一次主模型调用；
        - 手动把 user/assistant 两条消息写进 session.entries（与 runtime 行为对齐）；
        - 单次产出一条 ChatChunk（不流式），content 即"verdict + 下一步"。

        所有写盘动作在持锁后完成；judge 失败会被 TeachJudge 内吞掉，外层
        看到的就是 verdict=needs_review。
        """
        ts = unit.teach_session
        if ts is None or not ts.questions:
            # 异常：outputting 状态但没有题目。返回一段诊断 chunk，并直接收束。
            finalize_consolidation(unit)
            unit.transition_to("consolidated")
            self.learning_unit_store.save(unit)
            self.emit_unit_event(
                unit,
                SessionEventType.LEARNING_UNIT_PHASE_CHANGED,
                extra={"from": "outputting", "to": "consolidated", "reason": "no_questions"},
            )
            self.emit_unit_event(
                unit,
                SessionEventType.LEARNING_UNIT_CONSOLIDATED,
                extra={
                    "verification_status": unit.verification_status,
                    "mastered_count": 0,
                    "gaps_count": 0,
                    "reason": "no_questions",
                },
            )
            text = "本卷未能生成可评估题目，已自动收束。"
            self.record_teach_turn(session, unit, user_input, text)
            yield ChatChunk(
                content=text,
                metadata=teach_metadata(unit, verdict=None),
            )
            return

        idx = ts.current_index
        if idx >= len(ts.questions):
            # 已经答完，但 phase 还没切（极少出现的状态）。直接收束。
            finalize_consolidation(unit)
            unit.transition_to("consolidated")
            self.learning_unit_store.save(unit)
            self.emit_unit_event(
                unit,
                SessionEventType.LEARNING_UNIT_PHASE_CHANGED,
                extra={"from": "outputting", "to": "consolidated", "reason": "already_answered"},
            )
            self.emit_unit_event(
                unit,
                SessionEventType.LEARNING_UNIT_CONSOLIDATED,
                extra={
                    "verification_status": unit.verification_status,
                    "mastered_count": (
                        len(unit.feedback_card.mastered)
                        if unit.feedback_card else 0
                    ),
                    "gaps_count": (
                        len(unit.feedback_card.gaps)
                        if unit.feedback_card else 0
                    ),
                },
            )
            text = render_feedback_card(unit)
            self.record_teach_turn(session, unit, user_input, text)
            yield ChatChunk(
                content=text,
                metadata=teach_metadata(unit, verdict=None),
            )
            return

        question = ts.questions[idx]

        if self.teach_judge is None:
            verdict, judge_reason = "needs_review", "评判服务暂不可用。"
        else:
            verdict, judge_reason = await self.teach_judge.judge(
                question=question,
                user_answer=user_input,
            )

        async with self.learning_unit_store.lock(unit.id):
            latest = self.learning_unit_store.get(unit.id) or unit
            latest_ts = latest.teach_session
            if latest_ts is None or latest_ts.current_index != idx:
                # 并发修改：让最新状态赢，本次答题作废。
                logger.warning(
                    "[System] teach_session state shifted under concurrent "
                    f"writers for unit {unit.id}; dropping this answer"
                )
                text = "状态已变更，请刷新后重试。"
                yield ChatChunk(
                    content=text,
                    metadata=teach_metadata(latest, verdict=None),
                )
                return
            q = latest_ts.questions[idx]
            q.user_answer = user_input
            q.verdict = verdict
            q.judge_reason = judge_reason
            latest_ts.current_index = idx + 1
            done = latest_ts.current_index >= len(latest_ts.questions)
            if done:
                finalize_consolidation(latest)
                latest.transition_to("consolidated")
            else:
                latest_ts.state = "prompted"
            self.learning_unit_store.save(latest)

        if done:
            self.emit_unit_event(
                latest,
                SessionEventType.LEARNING_UNIT_PHASE_CHANGED,
                extra={"from": "outputting", "to": "consolidated"},
            )
            self.emit_unit_event(
                latest,
                SessionEventType.LEARNING_UNIT_CONSOLIDATED,
                extra={
                    "verification_status": latest.verification_status,
                    "mastered_count": (
                        len(latest.feedback_card.mastered)
                        if latest.feedback_card else 0
                    ),
                    "gaps_count": (
                        len(latest.feedback_card.gaps)
                        if latest.feedback_card else 0
                    ),
                },
            )

        # 渲染响应文本
        verdict_glyph = "✓" if verdict == "passed" else "✗"
        head = f"{verdict_glyph} {judge_reason}".strip()
        if done:
            body = render_feedback_card(latest)
            text = f"{head}\n\n{body}"
        else:
            next_q = latest_ts.questions[latest_ts.current_index]
            text = (
                f"{head}\n\n"
                f"下一题（{latest_ts.current_index + 1}/{len(latest_ts.questions)}）：{next_q.stem}"
            )

        self.record_teach_turn(session, latest, user_input, text)
        yield ChatChunk(
            content=text,
            metadata=teach_metadata(latest, verdict=verdict),
        )

    def record_teach_turn(
        self,
        session: "LearningSession",
        unit: "LearningUnit",
        user_input: str,
        assistant_text: str,
    ) -> None:
        """把 outputting 一轮的 user/assistant 消息写入 session.entries。"""
        common_meta = {
            "mode": AgentMode.TEACH.value,
            "learning_unit_id": unit.id,
            "learning_unit_phase": unit.phase,
        }
        if unit.teach_session is not None:
            common_meta["teach_session_id"] = unit.teach_session.id
        self.session_manager.append_message(
            session.id,
            MessageRole.USER,
            user_input,
            metadata=dict(common_meta),
        )
        self.session_manager.append_message(
            session.id,
            MessageRole.ASSISTANT,
            assistant_text,
            metadata=dict(common_meta),
        )
