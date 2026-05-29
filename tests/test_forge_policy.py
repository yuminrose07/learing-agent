"""Phase 1A：forge_policy.prepare_forge_stage_metadata 映射测试。

只验证 Product 层从 unit 当前 forge_stage / temperature_state 派生
ForgeStagePlan 的最小契约：
- entry  → learning_action=orient
- collision → learning_action=prepare_to_guess
- 未知/后续阶段（forge/fixed/cooling）→ 兜底 orient
- forge_stage / temperature_state 原样透传
- user_input 在 Phase 1A 不影响结果（占位参数）
"""

from __future__ import annotations

import sys

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai.learning_unit import LearningUnit, UnitObjective
from learning_agent.learning_agent.forge_policy import (
    ForgeStagePlan,
    prepare_forge_stage_metadata,
)


def _make_unit(*, forge_stage: str = "entry", temperature_state: str = "steady") -> LearningUnit:
    unit = LearningUnit(
        session_id="sess-forge",
        objective=UnitObjective(text="理解 attention"),
    )
    unit.forge_stage = forge_stage  # type: ignore[assignment]
    unit.temperature_state = temperature_state  # type: ignore[assignment]
    return unit


class TestPrepareForgeStageMetadata:
    def test_entry_maps_to_orient(self):
        plan = prepare_forge_stage_metadata(_make_unit(forge_stage="entry"), "任意输入")
        assert isinstance(plan, ForgeStagePlan)
        assert plan.forge_stage == "entry"
        assert plan.learning_action == "orient"

    def test_collision_maps_to_prepare_to_guess(self):
        plan = prepare_forge_stage_metadata(
            _make_unit(forge_stage="collision"), "任意输入"
        )
        assert plan.forge_stage == "collision"
        assert plan.learning_action == "prepare_to_guess"

    def test_temperature_state_passthrough(self):
        plan = prepare_forge_stage_metadata(
            _make_unit(forge_stage="entry", temperature_state="needs_example"), ""
        )
        assert plan.temperature_state == "needs_example"

    def test_unknown_stage_falls_back_to_orient(self):
        # Phase 1A 尚未为 forge/fixed/cooling 定义动作 → 兜底 orient，不抛错
        for stage in ("forge", "fixed", "cooling"):
            plan = prepare_forge_stage_metadata(_make_unit(forge_stage=stage), "")
            assert plan.forge_stage == stage
            assert plan.learning_action == "orient"

    def test_user_input_does_not_change_result(self):
        unit = _make_unit(forge_stage="entry")
        a = prepare_forge_stage_metadata(unit, "")
        b = prepare_forge_stage_metadata(unit, "完全不同的一长串用户输入文本")
        assert a.model_dump() == b.model_dump()
