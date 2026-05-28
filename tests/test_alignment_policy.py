"""``alignment_policy`` schema + 限流单元测试。

判定主体已迁移到 ``alignment_classifier.AlignmentClassifier``（LLM 驱动），
本模块只保留：
- ``AlignmentDecision`` / ``Candidate`` 的 schema 契约
- ``_apply_rate_limits`` 的 §9.3 #2/#3 静默降级行为
- ``apply_alignment_decision`` 副作用编排相关测试在 test_apply_alignment_decision.py
  / test_user_request_alignment.py 内已经覆盖，这里不重复
"""

from __future__ import annotations

import sys

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai.learning_unit import Candidate, LearningUnit, UnitObjective
from learning_agent.learning_agent.alignment_policy import (
    AlignmentDecision,
    MAX_SUGGESTIONS_PER_UNIT,
    _apply_rate_limits,
)


def _unit(text: str = "理解 attention") -> LearningUnit:
    return LearningUnit(
        session_id="sess-x",
        objective=UnitObjective(text=text),
    )


class TestDecisionContract:
    """``AlignmentDecision`` 的字段契约。"""

    def test_decision_minimal_fields(self):
        d = AlignmentDecision(mode="none", reason="clear_enough")
        dumped = d.model_dump()
        assert dumped["mode"] == "none"
        assert dumped["reason"] == "clear_enough"
        # 新增字段必须有默认值，旧调用方不需要传
        assert d.suggested_objective == ""
        assert d.assumption_note == ""
        assert d.candidates == []
        assert d.divergence_cost is None

    def test_decision_with_candidates(self):
        d = AlignmentDecision(
            mode="active",
            reason="conflicting_scope",
            candidates=[
                Candidate(objective="学 A", first_step="从 a1 开始"),
                Candidate(objective="学 B", first_step="从 b1 开始"),
            ],
            divergence_cost="high",
            assumption_note="一次列了多个目标，先挑一个。",
        )
        assert len(d.candidates) == 2
        assert d.candidates[0].objective == "学 A"
        assert d.divergence_cost == "high"
        # model_dump 必须能递归序列化嵌套的 Candidate
        dumped = d.model_dump()
        assert dumped["candidates"][0]["first_step"] == "从 a1 开始"

    def test_candidate_requires_both_fields(self):
        # objective 和 first_step 都是必填，缺一不可
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Candidate(objective="只有目标")  # type: ignore[call-arg]

    def test_divergence_cost_literal(self):
        # divergence_cost 只接受 "low" / "high" / None
        import pytest
        from pydantic import ValidationError

        AlignmentDecision(mode="none", reason="clear_enough", divergence_cost="low")
        AlignmentDecision(mode="none", reason="clear_enough", divergence_cost="high")
        AlignmentDecision(mode="none", reason="clear_enough", divergence_cost=None)
        with pytest.raises(ValidationError):
            AlignmentDecision(
                mode="none",
                reason="clear_enough",
                divergence_cost="medium",  # type: ignore[arg-type]
            )


class TestRateLimitDowngrade:
    """§9.3 #2 / #3：suggested 命中限流时静默降为 none。

    LLM 判定迁出后，这条规则仍然适用 —— ``AlignmentClassifier`` 在产出
    suggested 之后会主动调用 ``_apply_rate_limits``，确保单卷不会反复唠叨。
    """

    def _suggested(self) -> AlignmentDecision:
        return AlignmentDecision(
            mode="suggested",
            reason="too_broad",
            candidates=[
                Candidate(objective="学 A", first_step="切口 1"),
                Candidate(objective="学 B", first_step="切口 2"),
            ],
            divergence_cost="low",
            suggested_objective="学 A",
            assumption_note="先按 A 学。",
        )

    def test_suggested_passes_below_cap(self):
        unit = _unit()
        unit.suggestion_count = MAX_SUGGESTIONS_PER_UNIT - 1
        result = _apply_rate_limits(unit, self._suggested())
        assert result.mode == "suggested"
        assert len(result.candidates) == 2

    def test_suggested_downgraded_at_cap(self):
        unit = _unit()
        unit.suggestion_count = MAX_SUGGESTIONS_PER_UNIT
        result = _apply_rate_limits(unit, self._suggested())
        assert result.mode == "none"
        assert result.reason == "clear_enough"

    def test_suggested_downgraded_during_cooldown(self):
        unit = _unit()
        unit.nag_cooldown_remaining = 2
        result = _apply_rate_limits(unit, self._suggested())
        assert result.mode == "none"
        assert result.reason == "clear_enough"

    def test_active_unaffected_by_suggestion_cap(self):
        unit = _unit()
        unit.suggestion_count = 99
        unit.nag_cooldown_remaining = 99
        active = AlignmentDecision(
            mode="active",
            reason="missing_learnable_target",
            candidates=[
                Candidate(objective="学 A", first_step="切口 1"),
                Candidate(objective="学 B", first_step="切口 2"),
            ],
            divergence_cost="high",
        )
        result = _apply_rate_limits(unit, active)
        assert result.mode == "active"
        assert len(result.candidates) == 2

    def test_none_unaffected_by_counters(self):
        unit = _unit()
        unit.suggestion_count = 99
        unit.nag_cooldown_remaining = 99
        none = AlignmentDecision(mode="none", reason="clear_enough")
        result = _apply_rate_limits(unit, none)
        assert result.mode == "none"
