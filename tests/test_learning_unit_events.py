"""M1：LearningUnit 产品事件发射测试。

覆盖 ``LEARNING_UNIT_*`` 事件在以下入口处的最小调用契约（session_event_store
被 mock，断言 ``append_event(session_id, type, payload=...)`` 调用形状）：

- ``create_learning_unit`` → CREATED
- ``advance_learning_unit``：
    * absorbing → outputting：PHASE_CHANGED + TEACH_ENTERED
    * outputting → consolidated：PHASE_CHANGED + CONSOLIDATED
    * 0-题 skip 路径：PHASE_CHANGED + TEACH_ENTERED(skipped) +
      PHASE_CHANGED + CONSOLIDATED
- ``request_alignment`` → ALIGNMENT_STARTED(trigger=user_request)
- ``accept_assumption`` → ASSUMPTION_ACCEPTED + ALIGNMENT_SKIPPED
- ``refine_objective`` → OBJECTIVE_REFINED
- ``_apply_alignment_decision``：active → STARTED；suggested → SUGGESTED；
  user_request 消费 → RESOLVED；none → 静默
- ``_maybe_emit_first_value``：absorbing CHAT 首条非空响应 → FIRST_VALUE_DELIVERED；
  二次调用幂等（once-only 由 ``first_value_delivered_at`` 守卫）

每条事件 payload 必带最小集 ``{learning_unit_id, phase, alignment_state,
objective_status, alignment_reason, clarification_count}``，由共享断言守门。
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai import (
    AgentMode,
    ConceptItem,
    LearningSession,
    LearningUnit,
    TeachQuestion,
    TeachSession,
    UnitObjective,
)
from learning_agent.learning_agent.alignment_policy import (
    AlignmentDecision,
    COOLDOWN_AFTER_ACCEPT_ASSUMPTION,
)
from learning_agent.learning_agent.main import LearningAgentSystem
from learning_agent.learning_agent.mode_service import (
    PreparedSessionTurn,
    build_turn_profile,
)
from learning_agent.learning_agent.session_events import SessionEventType


_PAYLOAD_REQUIRED_KEYS = {
    "learning_unit_id",
    "phase",
    "alignment_state",
    "objective_status",
    "alignment_reason",
    "clarification_count",
}


def _build_event_capturing_system() -> LearningAgentSystem:
    """构造一个仅装配 event_store + learning_unit_store 的最小桩。

    全部依赖 ``__new__`` 跳过 ``__init__``，方便测试只关心事件副作用。
    """
    system = LearningAgentSystem.__new__(LearningAgentSystem)
    store = MagicMock()
    store.append_event = MagicMock()
    system.session_event_store = store
    learning_unit_store = MagicMock()
    learning_unit_store.get = MagicMock(return_value=None)
    learning_unit_store.save = MagicMock()
    system.learning_unit_store = learning_unit_store
    # session_manager 仅在 create/promote 中用到，本测试集不覆盖那条
    system.session_manager = MagicMock()
    return system


def _wire_store(system: LearningAgentSystem, unit: LearningUnit) -> None:
    system.learning_unit_store.get = MagicMock(
        side_effect=lambda uid: unit if uid == unit.id else None
    )
    system.learning_unit_store.save = MagicMock()
    system.learning_unit_store.lock = MagicMock()
    system.learning_unit_store.lock.return_value.__aenter__ = AsyncMock()
    system.learning_unit_store.lock.return_value.__aexit__ = AsyncMock()


def _make_unit(
    *,
    phase: str = "absorbing",
    concepts: bool = True,
    teach_session: TeachSession | None = None,
) -> LearningUnit:
    unit = LearningUnit(
        session_id="sess-evt",
        objective=UnitObjective(text="理解 attention"),
        concept_list=(
            [ConceptItem(id="cpt-attn", name="attention", summary="...", relevance=0.9)]
            if concepts else []
        ),
        teach_session=teach_session,
    )
    unit.phase = phase  # type: ignore[assignment]
    return unit


def _calls_for(store_mock, event_type: str) -> list[dict]:
    """从 append_event mock 中筛出指定事件类型的所有 payload。"""
    out = []
    for call in store_mock.append_event.call_args_list:
        args, kwargs = call
        if len(args) >= 2 and args[1] == event_type:
            out.append(kwargs.get("payload", {}))
        elif kwargs.get("type") == event_type:
            out.append(kwargs.get("payload", {}))
    return out


def _assert_payload_complete(payload: dict, unit_id: str) -> None:
    missing = _PAYLOAD_REQUIRED_KEYS - set(payload.keys())
    assert not missing, f"payload missing keys: {missing}"
    assert payload["learning_unit_id"] == unit_id


class TestCreateLearningUnitEvent:
    def test_create_emits_created(self):
        system = _build_event_capturing_system()
        # 构造一个由 store.create 返回的 unit；create_learning_unit 内部会调
        # session_manager.create_session + store.create。
        unit = _make_unit()
        session = LearningSession(id=unit.session_id)
        system.session_manager.create_session = MagicMock(return_value=session)
        system.session_manager.delete_session = MagicMock()
        system.learning_unit_store.create = MagicMock(return_value=unit)

        out_session, out_unit = system.create_learning_unit(seed_text="理解 attention")

        assert out_unit is unit
        assert out_session.learning_unit_id == unit.id
        created = _calls_for(system.session_event_store, "learning_unit.created")
        assert len(created) == 1
        _assert_payload_complete(created[0], unit.id)
        assert created[0]["source"] == "ai_distilled"
        assert created[0]["seed_text"] == "理解 attention"


class TestAdvanceLearningUnitEvents:
    @pytest.mark.asyncio
    async def test_normal_outputting_emits_phase_changed_and_teach_entered(self):
        system = _build_event_capturing_system()
        unit = _make_unit(phase="absorbing")
        # 模拟 teach_generator 注入题目 → 不走 skipped 分支
        async def _start(u):
            u.teach_session = TeachSession(
                questions=[
                    TeachQuestion(concept_id="cpt-attn", stem="？", kind="sa")
                ],
                current_index=0,
                state="prompted",
            )
        system._start_teach_session = _start  # type: ignore[assignment]
        _wire_store(system, unit)

        await system.advance_learning_unit(unit.id, "outputting")

        phase = _calls_for(system.session_event_store, "learning_unit.phase_changed")
        assert any(p["from"] == "absorbing" and p["to"] == "outputting" for p in phase)
        teach_entered = _calls_for(
            system.session_event_store, "learning_unit.teach_entered"
        )
        assert len(teach_entered) == 1
        _assert_payload_complete(teach_entered[0], unit.id)
        assert teach_entered[0]["question_total"] == 1
        # 不应触发 CONSOLIDATED
        assert _calls_for(
            system.session_event_store, "learning_unit.consolidated"
        ) == []

    @pytest.mark.asyncio
    async def test_skip_path_emits_full_sequence(self):
        system = _build_event_capturing_system()
        unit = _make_unit(phase="absorbing")
        # 模拟 teach_generator 返回 0 题 → skipped 分支
        async def _start(u):
            u.verification_status = "skipped"
        system._start_teach_session = _start  # type: ignore[assignment]
        _wire_store(system, unit)

        await system.advance_learning_unit(unit.id, "outputting")

        phase = _calls_for(system.session_event_store, "learning_unit.phase_changed")
        # 应该有 2 条：absorbing→outputting 和 outputting→consolidated
        transitions = [(p["from"], p["to"]) for p in phase]
        assert ("absorbing", "outputting") in transitions
        assert ("outputting", "consolidated") in transitions
        teach_entered = _calls_for(
            system.session_event_store, "learning_unit.teach_entered"
        )
        assert len(teach_entered) == 1
        assert teach_entered[0]["skipped"] is True
        assert teach_entered[0]["question_total"] == 0
        consolidated = _calls_for(
            system.session_event_store, "learning_unit.consolidated"
        )
        assert len(consolidated) == 1
        _assert_payload_complete(consolidated[0], unit.id)
        assert consolidated[0]["verification_status"] == "skipped"

    @pytest.mark.asyncio
    async def test_outputting_to_consolidated_emits_consolidated(self):
        system = _build_event_capturing_system()
        unit = _make_unit(phase="outputting")
        _wire_store(system, unit)

        await system.advance_learning_unit(unit.id, "consolidated")

        phase = _calls_for(system.session_event_store, "learning_unit.phase_changed")
        assert any(p["from"] == "outputting" and p["to"] == "consolidated" for p in phase)
        consolidated = _calls_for(
            system.session_event_store, "learning_unit.consolidated"
        )
        assert len(consolidated) == 1


class TestStopLearningUnitEvents:
    def test_absorbing_to_stopped_emits_phase_changed_and_stopped(self):
        system = _build_event_capturing_system()
        unit = _make_unit(phase="absorbing")
        _wire_store(system, unit)

        result = system.stop_learning_unit(unit.id, reason="user_stopped")

        assert result.phase == "stopped"
        phase = _calls_for(system.session_event_store, "learning_unit.phase_changed")
        assert any(p["from"] == "absorbing" and p["to"] == "stopped" for p in phase)
        stopped = _calls_for(system.session_event_store, "learning_unit.stopped")
        assert len(stopped) == 1
        _assert_payload_complete(stopped[0], unit.id)
        assert stopped[0]["stop_reason"] == "user_stopped"
        assert stopped[0]["stopped_at"]


class TestAlignmentEvents:
    @pytest.mark.asyncio
    async def test_request_alignment_emits_started(self):
        system = _build_event_capturing_system()
        unit = _make_unit(phase="absorbing")
        _wire_store(system, unit)

        await system.request_alignment(unit.id)

        started = _calls_for(
            system.session_event_store, "learning_unit.alignment_started"
        )
        assert len(started) == 1
        _assert_payload_complete(started[0], unit.id)
        assert started[0]["trigger"] == "user_request"
        # 状态已写入：active + user_request 反应在 payload
        assert started[0]["alignment_state"] == "active"
        assert started[0]["alignment_reason"] == "user_request"

    @pytest.mark.asyncio
    async def test_accept_assumption_emits_both_events(self):
        system = _build_event_capturing_system()
        unit = _make_unit(phase="absorbing")
        unit.alignment_state = "suggested"
        _wire_store(system, unit)

        await system.accept_assumption(unit.id)

        accepted = _calls_for(
            system.session_event_store, "learning_unit.assumption_accepted"
        )
        skipped = _calls_for(
            system.session_event_store, "learning_unit.alignment_skipped"
        )
        assert len(accepted) == 1
        assert len(skipped) == 1
        _assert_payload_complete(accepted[0], unit.id)
        _assert_payload_complete(skipped[0], unit.id)
        assert accepted[0]["cooldown"] == COOLDOWN_AFTER_ACCEPT_ASSUMPTION

    @pytest.mark.asyncio
    async def test_refine_objective_emits_refined(self):
        system = _build_event_capturing_system()
        unit = _make_unit(phase="absorbing")
        _wire_store(system, unit)

        await system.refine_objective(unit.id, "新目标文本")

        refined = _calls_for(
            system.session_event_store, "learning_unit.objective_refined"
        )
        assert len(refined) == 1
        _assert_payload_complete(refined[0], unit.id)
        assert refined[0]["new_text"] == "新目标文本"
        assert refined[0]["old_text"] == "理解 attention"

    @pytest.mark.asyncio
    async def test_apply_decision_active_non_user_request_emits_started(self):
        system = _build_event_capturing_system()
        unit = _make_unit(phase="absorbing")
        _wire_store(system, unit)

        await system._apply_alignment_decision(
            unit, AlignmentDecision(mode="active", reason="too_broad")
        )

        started = _calls_for(
            system.session_event_store, "learning_unit.alignment_started"
        )
        assert len(started) == 1
        assert started[0]["trigger"] == "too_broad"
        # clarification 计数应已 +1
        assert started[0]["clarification_count"] == 1

    @pytest.mark.asyncio
    async def test_apply_decision_user_request_emits_resolved(self):
        system = _build_event_capturing_system()
        unit = _make_unit(phase="absorbing")
        _wire_store(system, unit)

        await system._apply_alignment_decision(
            unit, AlignmentDecision(mode="active", reason="user_request")
        )

        resolved = _calls_for(
            system.session_event_store, "learning_unit.alignment_resolved"
        )
        started = _calls_for(
            system.session_event_store, "learning_unit.alignment_started"
        )
        assert len(resolved) == 1
        assert started == []
        # user_request 不计入 clarification_count
        assert resolved[0]["clarification_count"] == 0

    @pytest.mark.asyncio
    async def test_apply_decision_suggested_emits_suggested(self):
        system = _build_event_capturing_system()
        unit = _make_unit(phase="absorbing")
        _wire_store(system, unit)

        await system._apply_alignment_decision(
            unit,
            AlignmentDecision(
                mode="suggested",
                reason="too_broad",
                suggested_objective="收窄到 multi-head",
            ),
        )

        suggested = _calls_for(
            system.session_event_store, "learning_unit.alignment_suggested"
        )
        assert len(suggested) == 1
        assert suggested[0]["suggested_objective"] == "收窄到 multi-head"
        assert suggested[0]["suggestion_count"] == 1

    @pytest.mark.asyncio
    async def test_apply_decision_none_is_silent(self):
        system = _build_event_capturing_system()
        unit = _make_unit(phase="absorbing")
        _wire_store(system, unit)

        await system._apply_alignment_decision(
            unit, AlignmentDecision(mode="none", reason="clear_enough")
        )

        # 所有 alignment_* 类事件都不应发射
        for t in (
            "learning_unit.alignment_started",
            "learning_unit.alignment_suggested",
            "learning_unit.alignment_resolved",
            "learning_unit.alignment_skipped",
        ):
            assert _calls_for(system.session_event_store, t) == []


class TestFirstValueDeliveredEvent:
    def _make_prepared_turn(self, mode: AgentMode = AgentMode.CHAT) -> PreparedSessionTurn:
        profile = build_turn_profile(mode)
        return PreparedSessionTurn(
            effective_mode=mode,
            runtime_input="x",
            profile=profile,
            stream_metadata={},
        )

    def test_first_absorbing_chat_response_emits_once(self):
        system = _build_event_capturing_system()
        unit = _make_unit(phase="absorbing")
        _wire_store(system, unit)
        session = LearningSession(id=unit.session_id, learning_unit_id=unit.id)

        prepared = self._make_prepared_turn(AgentMode.CHAT)
        system._maybe_emit_first_value(session, prepared, "首条实质回答")
        # 二次调用：first_value_delivered_at 已置位，应静默
        system._maybe_emit_first_value(session, prepared, "再来一条")

        first = _calls_for(
            system.session_event_store, "learning_unit.first_value_delivered"
        )
        assert len(first) == 1
        _assert_payload_complete(first[0], unit.id)
        assert "delivered_at" in first[0]
        assert first[0]["response_chars"] == len("首条实质回答")
        # 卷字段已落
        assert unit.first_value_delivered_at is not None

    def test_empty_response_does_not_emit(self):
        system = _build_event_capturing_system()
        unit = _make_unit(phase="absorbing")
        _wire_store(system, unit)
        session = LearningSession(id=unit.session_id, learning_unit_id=unit.id)

        system._maybe_emit_first_value(
            session, self._make_prepared_turn(AgentMode.CHAT), "   "
        )

        assert _calls_for(
            system.session_event_store, "learning_unit.first_value_delivered"
        ) == []
        assert unit.first_value_delivered_at is None

    def test_ask_mode_does_not_count_as_first_value(self):
        system = _build_event_capturing_system()
        unit = _make_unit(phase="absorbing")
        _wire_store(system, unit)
        session = LearningSession(id=unit.session_id, learning_unit_id=unit.id)

        system._maybe_emit_first_value(
            session, self._make_prepared_turn(AgentMode.ASK), "对齐反问的开场"
        )

        assert _calls_for(
            system.session_event_store, "learning_unit.first_value_delivered"
        ) == []
        assert unit.first_value_delivered_at is None

    def test_no_unit_attached_is_silent(self):
        system = _build_event_capturing_system()
        session = LearningSession(id="s-plain", learning_unit_id=None)

        system._maybe_emit_first_value(
            session, self._make_prepared_turn(AgentMode.CHAT), "回答"
        )

        assert system.session_event_store.append_event.call_count == 0


class TestForgeStageEvents:
    """Phase 1A：_maybe_advance_forge_stage 推进 entry→collision 并发 FORGE_STAGE_CHANGED。"""

    def _make_prepared_turn(self, mode: AgentMode = AgentMode.STUDY) -> PreparedSessionTurn:
        profile = build_turn_profile(mode)
        return PreparedSessionTurn(
            effective_mode=mode,
            runtime_input="x",
            profile=profile,
            stream_metadata={},
        )

    def test_first_study_response_advances_and_emits_once(self):
        system = _build_event_capturing_system()
        unit = _make_unit(phase="absorbing")
        _wire_store(system, unit)
        session = LearningSession(id=unit.session_id, learning_unit_id=unit.id)

        prepared = self._make_prepared_turn(AgentMode.STUDY)
        system._maybe_advance_forge_stage(session, prepared, "首条实质回答")
        # 二次调用：forge_stage 已是 collision，应静默（once-only 守卫）
        system._maybe_advance_forge_stage(session, prepared, "再来一条")

        assert unit.forge_stage == "collision"
        changed = _calls_for(
            system.session_event_store, "learning_unit.forge_stage_changed"
        )
        assert len(changed) == 1
        _assert_payload_complete(changed[0], unit.id)
        assert changed[0]["from"] == "entry"
        assert changed[0]["to"] == "collision"
        assert changed[0]["temperature_state"] == "steady"
        assert changed[0]["reason"] == "first_value_delivered"

    def test_empty_response_does_not_advance(self):
        system = _build_event_capturing_system()
        unit = _make_unit(phase="absorbing")
        _wire_store(system, unit)
        session = LearningSession(id=unit.session_id, learning_unit_id=unit.id)

        system._maybe_advance_forge_stage(
            session, self._make_prepared_turn(AgentMode.STUDY), "   "
        )

        assert unit.forge_stage == "entry"
        assert _calls_for(
            system.session_event_store, "learning_unit.forge_stage_changed"
        ) == []

    def test_ask_mode_does_not_advance(self):
        system = _build_event_capturing_system()
        unit = _make_unit(phase="absorbing")
        _wire_store(system, unit)
        session = LearningSession(id=unit.session_id, learning_unit_id=unit.id)

        system._maybe_advance_forge_stage(
            session, self._make_prepared_turn(AgentMode.ASK), "对齐反问的开场"
        )

        assert unit.forge_stage == "entry"
        assert _calls_for(
            system.session_event_store, "learning_unit.forge_stage_changed"
        ) == []

    def test_no_unit_attached_is_silent(self):
        system = _build_event_capturing_system()
        session = LearningSession(id="s-plain", learning_unit_id=None)

        system._maybe_advance_forge_stage(
            session, self._make_prepared_turn(AgentMode.STUDY), "回答"
        )

        assert system.session_event_store.append_event.call_count == 0


class TestEventEmissionFaultTolerance:
    """append_event 抛异常时主流不应受影响（仅记日志）。"""

    @pytest.mark.asyncio
    async def test_emit_swallows_event_store_exception(self):
        system = _build_event_capturing_system()
        unit = _make_unit(phase="absorbing")
        _wire_store(system, unit)
        system.session_event_store.append_event = MagicMock(
            side_effect=RuntimeError("disk full")
        )

        # 不应抛
        await system.request_alignment(unit.id)
        assert unit.alignment_state == "active"
