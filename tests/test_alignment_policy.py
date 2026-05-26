"""``alignment_policy.should_run_alignment`` 三档判定单测。

覆盖 adaptive alignment §5.3 各档位的代表性输入：清楚直答 (A) / 非阻塞建议 (B) /
短暂阻塞澄清 (C)。每档 ≥ 2 例，含中英文混合。
"""

from __future__ import annotations

import pytest

from learning_agent.ai.learning_unit import ConceptItem, LearningUnit, UnitObjective
from learning_agent.learning_agent.alignment_policy import (
    AlignmentDecision,
    should_run_alignment,
)


def _unit(text: str = "理解 attention") -> LearningUnit:
    return LearningUnit(
        session_id="sess-x",
        objective=UnitObjective(text=text),
    )


class TestClearEnough:
    """A 档：直接进 absorbing，不打扰。"""

    def test_concrete_module_with_intent_is_clear(self):
        decision = should_run_alignment(
            _unit(),
            "带我理解这个项目里 compaction 的完整流程。",
        )
        assert decision.mode == "none"
        assert decision.reason == "clear_enough"

    def test_concept_with_named_target_is_clear(self):
        decision = should_run_alignment(
            _unit(),
            "我想学 session_projection 是怎么靠 JSONL replay 重建会话状态的。",
        )
        assert decision.mode == "none"
        assert decision.reason == "clear_enough"

    def test_english_clear_input(self):
        decision = should_run_alignment(
            _unit(),
            "Explain how the LearningUnit phase machine works under product layer.",
        )
        assert decision.mode == "none"
        assert decision.reason == "clear_enough"


class TestTooBroad:
    """B 档：进 absorbing，但 UI 给非阻塞建议条。"""

    def test_teach_me_the_whole_project_is_too_broad(self):
        decision = should_run_alignment(_unit(), "教我整个项目")
        assert decision.mode == "suggested"
        assert decision.reason == "too_broad"
        assert decision.assumption_note  # 必须给文案，UI 要拿来显示

    def test_learn_everything_in_english_is_too_broad(self):
        decision = should_run_alignment(
            _unit(),
            "I want to learn everything about this codebase.",
        )
        assert decision.mode == "suggested"
        assert decision.reason == "too_broad"

    def test_all_modules_with_study_verb_is_too_broad(self):
        decision = should_run_alignment(
            _unit(),
            "我想学习所有跟 compaction 有关的模块",
        )
        assert decision.mode == "suggested"
        assert decision.reason == "too_broad"


class TestMissingLearnableTarget:
    """C 档：本轮临时覆写为 ASK 做一次短澄清。"""

    def test_too_short_input_is_active(self):
        decision = should_run_alignment(_unit(), "教")
        assert decision.mode == "active"
        assert decision.reason == "missing_learnable_target"

    def test_pronoun_only_input_is_active(self):
        decision = should_run_alignment(_unit(), "讲讲这个")
        assert decision.mode == "active"
        assert decision.reason == "missing_learnable_target"

    def test_vague_anything_is_active(self):
        decision = should_run_alignment(_unit(), "随便讲讲什么都行")
        assert decision.mode == "active"
        assert decision.reason == "missing_learnable_target"


class TestConflictingScope:
    """C 档另一种情形：一次列了多个互相竞争的主目标。"""

    def test_three_distinct_objects_with_intent_is_conflicting(self):
        decision = should_run_alignment(
            _unit(),
            "顺便也讲下 memory、session、web UI 怎么串",
        )
        assert decision.mode == "active"
        assert decision.reason == "conflicting_scope"

    def test_two_objects_with_intent_still_passes(self):
        # 边界用例：只有 2 个对象 + 学习意图，不算 conflicting
        decision = should_run_alignment(
            _unit(),
            "我想理解 LearningUnit 和 alignment_policy 的关系",
        )
        assert decision.mode == "none"
        assert decision.reason == "clear_enough"


class TestDecisionContract:
    """``AlignmentDecision`` 的字段契约。"""

    def test_decision_is_pydantic_model(self):
        d = AlignmentDecision(mode="none", reason="clear_enough")
        assert d.model_dump()["mode"] == "none"
        assert d.suggested_objective == ""
        assert d.assumption_note == ""

    def test_active_decisions_carry_assumption_note(self):
        # C 档必须给 UI 文案解释为什么打断，否则用户体验是"莫名其妙弹一个对话框"
        for text in ["教", "讲讲这个", "随便讲讲什么都行"]:
            d = should_run_alignment(_unit(), text)
            assert d.mode == "active"
            assert d.assumption_note, f"missing assumption_note for {text!r}"

    def test_unused_recent_messages_arg_is_accepted(self):
        # 一阶段 recent_messages 不参与判定，但接口必须留口子
        d = should_run_alignment(
            _unit(),
            "带我理解 compaction 流程",
            recent_messages=[{"role": "user", "content": "x"}],
        )
        assert d.mode == "none"


class TestConceptListAwareness:
    """已知 concept_list 的豁免：用户在已展开的学习地图里追问短问题不应被打断。"""

    def _unit_with_concepts(self, *concepts: str) -> LearningUnit:
        return LearningUnit(
            session_id="sess-x",
            objective=UnitObjective(text="理解 Pydantic v2"),
            concept_list=[
                ConceptItem(name=name, summary=f"{name} summary", relevance=0.9)
                for name in concepts
            ],
        )

    def test_short_input_naming_known_concept_is_clear(self):
        unit = self._unit_with_concepts("BaseModel")
        decision = should_run_alignment(unit, "讲讲 BaseModel")
        # 没 concept_list 会被判 active；命中已知 concept 应豁免
        assert decision.mode == "none"

    def test_short_input_without_concept_match_still_active(self):
        unit = self._unit_with_concepts("BaseModel")
        decision = should_run_alignment(unit, "讲讲")
        assert decision.mode == "active"
        assert decision.reason == "missing_learnable_target"

    def test_vague_pronoun_not_rescued_by_concept_list(self):
        # "讲讲这个"是代词指代，即便 concept_list 非空也不应豁免
        unit = self._unit_with_concepts("BaseModel")
        decision = should_run_alignment(unit, "讲讲这个")
        assert decision.mode == "active"


class TestRateLimitDowngrade:
    """§9.3 #2 / #3：suggested 命中限流时静默降为 none。"""

    def test_suggested_downgraded_after_suggestion_cap(self):
        unit = _unit()
        unit.suggestion_count = 2  # 已经弹过 2 条
        decision = should_run_alignment(unit, "教我整个项目")
        assert decision.mode == "none"
        assert decision.reason == "clear_enough"

    def test_suggested_passes_below_suggestion_cap(self):
        unit = _unit()
        unit.suggestion_count = 1  # 还有一条额度
        decision = should_run_alignment(unit, "教我整个项目")
        assert decision.mode == "suggested"

    def test_suggested_downgraded_during_cooldown(self):
        unit = _unit()
        unit.nag_cooldown_remaining = 2
        decision = should_run_alignment(unit, "教我整个项目")
        assert decision.mode == "none"

    def test_active_not_affected_by_suggestion_cap(self):
        # active 由 clarification_count 在调度器侧限流，不在 policy 内降级
        unit = _unit()
        unit.suggestion_count = 99
        unit.nag_cooldown_remaining = 99
        decision = should_run_alignment(unit, "教")
        assert decision.mode == "active"

    def test_none_unaffected_by_counters(self):
        unit = _unit()
        unit.suggestion_count = 99
        unit.nag_cooldown_remaining = 99
        decision = should_run_alignment(unit, "带我理解 compaction 完整流程")
        assert decision.mode == "none"


@pytest.mark.parametrize(
    "text,expected_mode",
    [
        ("带我理解 compaction 完整流程", "none"),
        ("教我整个项目", "suggested"),
        ("教", "active"),
        ("讲讲这个", "active"),
        ("Explain LearningUnit phase machine", "none"),
        ("I want to learn everything", "suggested"),
    ],
)
def test_dispatch_table(text: str, expected_mode: str):
    assert should_run_alignment(_unit(), text).mode == expected_mode
