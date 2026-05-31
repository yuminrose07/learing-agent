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

from unittest.mock import MagicMock

from learning_agent.ai.learning_unit import (
    LearningUnit,
    OrientationContext,
    UnitObjective,
)
from learning_agent.learning_agent.forge_policy import (
    ForgeStagePlan,
    maybe_advance_forge_stage,
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


def _orientation() -> OrientationContext:
    return OrientationContext(
        prompt_text="如果换成你要解释 attention，你会先抓哪条线索？",
        hook_kind="scenario",
        source="llm",
        source_seed_ref="objective:理解 attention",
        orientation_digest="digest123",
    )


class TestPrepareForgeStageMetadata:
    def test_entry_maps_to_orient(self):
        plan = prepare_forge_stage_metadata(_make_unit(forge_stage="entry"), "任意输入")
        assert isinstance(plan, ForgeStagePlan)
        assert plan.forge_stage == "entry"
        assert plan.learning_action == "orient"

    def test_entry_with_orientation_awaits_response(self):
        unit = _make_unit(forge_stage="entry")
        unit.orientation_context = _orientation()
        plan = prepare_forge_stage_metadata(unit, "任意输入")
        assert plan.learning_action == "await_orientation_response"
        assert plan.hook_kind == "scenario"

    def test_collision_maps_to_prepare_to_guess(self):
        plan = prepare_forge_stage_metadata(
            _make_unit(forge_stage="collision"), "任意输入"
        )
        assert plan.forge_stage == "collision"
        assert plan.learning_action == "prepare_to_guess"

    def test_collision_with_orientation_awaits_response(self):
        unit = _make_unit(forge_stage="collision")
        unit.orientation_context = _orientation()
        plan = prepare_forge_stage_metadata(unit, "任意输入")
        assert plan.learning_action == "await_orientation_response"
        assert plan.hook_kind == "scenario"

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


class TestMaybeAdvanceForgeStage:
    def _store(self):
        store = MagicMock()
        store.save = MagicMock()
        return store

    def test_entry_requires_orientation_context(self):
        unit = _make_unit(forge_stage="entry")
        store = self._store()
        emit = MagicMock()

        maybe_advance_forge_stage(
            unit=unit,
            store=store,
            emit_unit_event=emit,
            response_text="首条实质回答",
            effective_mode="study",
            user_input="讲讲 attention",
        )

        assert unit.forge_stage == "entry"
        store.save.assert_not_called()
        emit.assert_not_called()

    def test_entry_advances_after_orientation_attempt(self):
        unit = _make_unit(forge_stage="entry")
        unit.orientation_context = _orientation()
        store = self._store()
        emit = MagicMock()

        maybe_advance_forge_stage(
            unit=unit,
            store=store,
            emit_unit_event=emit,
            response_text="首条实质回答",
            effective_mode="study",
            user_input="讲讲 attention",
        )

        assert unit.forge_stage == "collision"
        store.save.assert_called_once_with(unit)
        assert emit.call_args.args[1] == "learning_unit.forge_stage_changed"
        assert emit.call_args.kwargs["extra"]["from"] == "entry"
        assert emit.call_args.kwargs["extra"]["to"] == "collision"

    def test_collision_does_not_advance_in_phase_1b(self):
        unit = _make_unit(forge_stage="collision")
        unit.orientation_context = _orientation()
        store = self._store()
        emit = MagicMock()

        maybe_advance_forge_stage(
            unit=unit,
            store=store,
            emit_unit_event=emit,
            response_text="继续",
            effective_mode="study",
            user_input="我猜是因为 A 会影响 B 的判断",
        )

        assert unit.forge_stage == "collision"
        store.save.assert_not_called()
        emit.assert_not_called()

    def test_collision_without_orientation_also_does_not_advance(self):
        unit = _make_unit(forge_stage="collision")
        store = self._store()
        emit = MagicMock()

        maybe_advance_forge_stage(
            unit=unit,
            store=store,
            emit_unit_event=emit,
            response_text="继续",
            effective_mode="study",
            user_input="我猜是因为 A 会影响 B 的判断",
        )

        assert unit.forge_stage == "collision"
        store.save.assert_not_called()
        emit.assert_not_called()
