from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai import (
    AgentMode,
    ChatChunk,
    ConceptItem,
    LearningSession,
    LearningUnit,
    TeachQuestion,
    TeachSession,
    UnitObjective,
)
from learning_agent.learning_agent.main import LearningAgentSystem
from learning_agent.learning_agent.mode_service import (
    FEYNMAN_PERSONA,
    NEUTRAL_PERSONA,
    SOCRATES_PERSONA,
    ZHU_XI_PERSONA,
    TEACH_MODE_PROMPT,
    TurnExecutionKind,
    build_system_prompt,
    build_turn_profile,
    resolve_persona,
)
from learning_agent.learning_agent.session_manager import SessionManager


def _build_system_stub() -> LearningAgentSystem:
    system = LearningAgentSystem.__new__(LearningAgentSystem)
    system.agent_loop = MagicMock()
    system.save_session = MagicMock(return_value=True)
    system.update_session_mode = MagicMock()
    system.get_session = MagicMock()
    learning_unit_store = MagicMock()
    learning_unit_store.get = MagicMock(return_value=None)
    system.learning_unit_store = learning_unit_store
    return system


def _attach_unit(system: LearningAgentSystem, unit: LearningUnit) -> None:
    """Monkey-patch get_learning_unit so the stub system returns the given unit."""
    system.get_learning_unit = lambda uid: unit if uid == unit.id else None


def _make_unit(
    *,
    phase: str = "aligning",
    teach_session: TeachSession | None = None,
) -> LearningUnit:
    unit = LearningUnit(
        session_id="sess-lu",
        objective=UnitObjective(text="理解 Pydantic v2 的 BaseModel"),
        concept_list=[
            ConceptItem(name="BaseModel", summary="数据模型基类", relevance=0.95),
        ],
        teach_session=teach_session,
    )
    # bypass the state-machine guard for setup — tests cover transitions separately
    unit.phase = phase  # type: ignore[assignment]
    return unit


def _make_session_with_unit(unit: LearningUnit) -> LearningSession:
    return LearningSession(
        id=unit.session_id,
        mode=AgentMode.CHAT,
        learning_unit_id=unit.id,
    )


class TestModeLayering:
    def test_build_turn_profile_defaults_to_neutral_persona(self):
        profile = build_turn_profile(
            AgentMode.ASK,
            assistant_message_metadata={"alignment": True},
        )

        assert profile.turn_kind == TurnExecutionKind.SINGLE_PASS
        assert profile.assistant_message_metadata["mode"] == "ask"
        assert profile.assistant_message_metadata["alignment"] is True
        assert profile.assistant_message_metadata["persona_key"] == NEUTRAL_PERSONA.key
        assert profile.assistant_message_metadata["persona_name"] == NEUTRAL_PERSONA.display_name
        assert "对齐" in profile.system_prompt

    def test_build_turn_profile_applies_explicit_persona_overlay(self):
        profile = build_turn_profile(
            AgentMode.CHAT,
            persona_key=SOCRATES_PERSONA.key,
        )

        assert profile.turn_kind == TurnExecutionKind.REACT
        assert profile.assistant_message_metadata["mode"] == "chat"
        assert profile.assistant_message_metadata["persona_key"] == SOCRATES_PERSONA.key
        assert profile.assistant_message_metadata["persona_name"] == SOCRATES_PERSONA.display_name
        # the persona's tone overlay must reach the composed system prompt
        assert "苏格拉底" in profile.system_prompt

    def test_build_turn_profile_unknown_persona_falls_back_to_neutral(self):
        # legacy persisted keys (e.g. removed harem personas) must not raise
        profile = build_turn_profile(
            AgentMode.CHAT,
            persona_key="shu_consort_lu_zhiwei",
        )

        assert profile.assistant_message_metadata["persona_key"] == NEUTRAL_PERSONA.key
        assert profile.assistant_message_metadata["persona_name"] == NEUTRAL_PERSONA.display_name

    @pytest.mark.asyncio
    async def test_prepare_session_turn_uses_neutral_persona_by_default(self):
        system = _build_system_stub()
        session = LearningSession(id="sess-chat", mode=AgentMode.CHAT)

        _, first_turn = await system._prepare_session_turn(session, "第一句", AgentMode.CHAT)
        _, second_turn = await system._prepare_session_turn(session, "第二句", AgentMode.CHAT)

        # no auto-picking — session metadata stays clean until the user opts in
        assert "chat_persona_key" not in session.mode_metadata
        assert first_turn.profile.assistant_message_metadata["persona_key"] == NEUTRAL_PERSONA.key
        assert second_turn.profile.assistant_message_metadata["persona_key"] == NEUTRAL_PERSONA.key

    @pytest.mark.asyncio
    async def test_prepare_session_turn_honors_explicit_persona_in_session_metadata(self):
        system = _build_system_stub()
        session = LearningSession(
            id="sess-chat-persona",
            mode=AgentMode.CHAT,
            mode_metadata={"chat_persona_key": FEYNMAN_PERSONA.key},
        )

        _, turn = await system._prepare_session_turn(session, "讲讲", AgentMode.CHAT)

        assert turn.profile.assistant_message_metadata["persona_key"] == FEYNMAN_PERSONA.key
        assert turn.profile.assistant_message_metadata["persona_name"] == FEYNMAN_PERSONA.display_name

    @pytest.mark.asyncio
    async def test_prepare_session_turn_carries_persona_across_mode_switch(self):
        system = _build_system_stub()
        session = LearningSession(
            id="sess-chat-reuse",
            mode=AgentMode.ASK,
            mode_metadata={"chat_persona_key": ZHU_XI_PERSONA.key},
        )
        session.ask_state.status = "aligning"
        session.ask_state.confirmed_input = "继续回答我刚才的问题"

        switched_session = session.model_copy(deep=True)
        switched_session.mode = AgentMode.CHAT
        system.update_session_mode.return_value = switched_session

        prepared_session, prepared_turn = await system._prepare_session_turn(
            session,
            "确认，开始吧",
            AgentMode.ASK,
        )

        assert prepared_session.mode == AgentMode.CHAT
        assert prepared_session.mode_metadata["chat_persona_key"] == ZHU_XI_PERSONA.key
        assert prepared_turn.profile.assistant_message_metadata["persona_key"] == ZHU_XI_PERSONA.key
        assert prepared_turn.profile.assistant_message_metadata["persona_name"] == ZHU_XI_PERSONA.display_name

    @pytest.mark.asyncio
    async def test_prepare_session_turn_keeps_ask_confirmation_in_product_layer(self):
        system = _build_system_stub()

        session = LearningSession(id="sess-ask", mode=AgentMode.ASK)
        session.ask_state.status = "aligning"
        session.ask_state.confirmed_input = "请先审查 runtime 与 product 的分层边界"

        switched_session = session.model_copy(deep=True)
        switched_session.mode = AgentMode.CHAT
        system.update_session_mode.return_value = switched_session

        prepared_session, prepared_turn = await system._prepare_session_turn(
            session,
            "好的，开始吧",
            AgentMode.ASK,
        )

        assert prepared_session.mode == AgentMode.CHAT
        assert prepared_turn.effective_mode == AgentMode.CHAT
        assert prepared_turn.runtime_input == "请先审查 runtime 与 product 的分层边界"
        assert prepared_turn.profile.turn_kind == TurnExecutionKind.REACT
        assert prepared_turn.compaction_plan is not None
        assert prepared_turn.compaction_plan.use_micro_compact is False
        assert prepared_turn.compaction_plan.use_full_compact is False
        assert session.ask_state.status == "idle"
        assert session.ask_state.confirmed_input == ""
        system.update_session_mode.assert_called_once_with(
            "sess-ask",
            AgentMode.CHAT,
            clear_ask_state=False,
        )

    @pytest.mark.asyncio
    async def test_stream_session_chat_captures_alignment_output_in_product_layer(self):
        system = _build_system_stub()

        session = LearningSession(id="sess-align", mode=AgentMode.ASK)
        system.get_session.return_value = session

        async def _runtime_chunks():
            yield ChatChunk(content="我理解你的目标是先完成")
            yield ChatChunk(content="分层审查，再决定是否实现。")

        system.agent_loop.run.return_value = _runtime_chunks()

        chunks = []
        async for chunk in system.stream_session_chat("sess-align", "帮我先对齐目标", mode=AgentMode.ASK):
            chunks.append(chunk)

        assert "".join(chunk.content for chunk in chunks) == "我理解你的目标是先完成分层审查，再决定是否实现。"
        assert session.ask_state.status == "aligning"
        assert session.ask_state.confirmed_input == "我理解你的目标是先完成分层审查，再决定是否实现。"
        assert chunks[0].metadata["mode"] == "ask"
        assert chunks[0].metadata["alignment"] is True
        # default ASK now uses NEUTRAL persona, not a fixed harem persona
        assert chunks[0].metadata["persona_key"] == NEUTRAL_PERSONA.key
        assert chunks[0].metadata["persona_name"] == NEUTRAL_PERSONA.display_name

        _, run_kwargs = system.agent_loop.run.call_args
        assert run_kwargs["profile"].turn_kind == TurnExecutionKind.SINGLE_PASS
        assert run_kwargs["compaction_plan"].use_micro_compact is False
        assert run_kwargs["compaction_plan"].use_full_compact is False
        system.save_session.assert_not_called()


class TestTeachProtocol:
    def test_build_turn_profile_for_teach_uses_single_pass(self):
        profile = build_turn_profile(AgentMode.TEACH)

        assert profile.mode == AgentMode.TEACH
        assert profile.turn_kind == TurnExecutionKind.SINGLE_PASS
        assert profile.visible_tools == []
        assert profile.memory_read is False
        assert profile.memory_write is False
        assert profile.micro_compact_enabled is False
        assert profile.full_compact_enabled is False
        assert TEACH_MODE_PROMPT in profile.system_prompt
        assert profile.assistant_message_metadata["mode"] == "teach"

    def test_learning_unit_phase_transitions_are_validated(self):
        unit = LearningUnit(
            session_id="sess-x",
            objective=UnitObjective(text="t"),
        )
        # aligning → absorbing OK
        assert unit.can_transition_to("absorbing")
        unit.transition_to("absorbing")
        assert unit.phase == "absorbing"

        # absorbing → outputting OK
        unit.transition_to("outputting")
        assert unit.phase == "outputting"

        # outputting → absorbing OK (TEACH 失败回退)
        unit.transition_to("absorbing")
        assert unit.phase == "absorbing"

        # absorbing → consolidated 必须经过 outputting
        with pytest.raises(ValueError):
            unit.transition_to("consolidated")

        # 走完正路径到达 consolidated
        unit.transition_to("outputting")
        unit.transition_to("consolidated")
        assert unit.is_terminal()

        # consolidated 是终态，任何过渡都拒绝
        with pytest.raises(ValueError):
            unit.transition_to("absorbing")

    @pytest.mark.asyncio
    async def test_prepare_session_turn_aligning_phase_yields_ask_mode(self):
        system = _build_system_stub()
        unit = _make_unit(phase="aligning")
        _attach_unit(system, unit)
        session = _make_session_with_unit(unit)

        _, turn = await system._prepare_session_turn(
            session, "我想学 Pydantic", AgentMode.CHAT
        )

        assert turn.effective_mode == AgentMode.ASK
        assert turn.profile.turn_kind == TurnExecutionKind.SINGLE_PASS
        assert turn.runtime_input == "我想学 Pydantic"
        meta = turn.profile.assistant_message_metadata
        assert meta["mode"] == "ask"
        assert meta["learning_unit_id"] == unit.id
        assert meta["learning_unit_phase"] == "aligning"
        assert meta["alignment"] is True
        assert meta["aligning_round"] == unit.aligning_round

    @pytest.mark.asyncio
    async def test_prepare_session_turn_absorbing_phase_yields_chat_mode(self):
        system = _build_system_stub()
        unit = _make_unit(phase="absorbing")
        _attach_unit(system, unit)
        session = _make_session_with_unit(unit)

        _, turn = await system._prepare_session_turn(
            session, "讲讲 BaseModel", AgentMode.CHAT
        )

        assert turn.effective_mode == AgentMode.CHAT
        assert turn.profile.turn_kind == TurnExecutionKind.REACT
        meta = turn.profile.assistant_message_metadata
        assert meta["mode"] == "chat"
        assert meta["learning_unit_phase"] == "absorbing"
        assert meta["learning_unit_id"] == unit.id
        # absorbing 阶段不带 alignment / teach_session_id
        assert "alignment" not in meta
        assert "teach_session_id" not in meta

    @pytest.mark.asyncio
    async def test_prepare_session_turn_outputting_phase_yields_teach_mode(self):
        system = _build_system_stub()
        teach = TeachSession(
            questions=[
                TeachQuestion(
                    concept_id="cpt-1",
                    kind="sa",
                    stem="请解释 BaseModel 的作用。",
                    rubric="提到验证与序列化即算通过。",
                ),
            ],
            state="prompted",
        )
        unit = _make_unit(phase="outputting", teach_session=teach)
        _attach_unit(system, unit)
        session = _make_session_with_unit(unit)

        _, turn = await system._prepare_session_turn(
            session, "我准备好答题了", AgentMode.CHAT
        )

        assert turn.effective_mode == AgentMode.TEACH
        assert turn.profile.turn_kind == TurnExecutionKind.SINGLE_PASS
        assert turn.profile.visible_tools == []
        meta = turn.profile.assistant_message_metadata
        assert meta["mode"] == "teach"
        assert meta["learning_unit_phase"] == "outputting"
        assert meta["teach_session_id"] == teach.id
        assert meta["teach_state"] == "prompted"

    @pytest.mark.asyncio
    async def test_prepare_session_turn_consolidated_phase_raises(self):
        system = _build_system_stub()
        unit = _make_unit(phase="consolidated")
        _attach_unit(system, unit)
        session = _make_session_with_unit(unit)

        with pytest.raises(ValueError):
            await system._prepare_session_turn(
                session, "再问一个问题", AgentMode.CHAT
            )

    @pytest.mark.asyncio
    async def test_prepare_session_turn_without_unit_falls_back_to_legacy_path(self):
        # learning_unit_id 已设但 get_learning_unit 返回 None（持久化尚未接管），
        # 应回退到原有的非学习卷调度路径，不影响普通 CHAT 会话。
        system = _build_system_stub()
        unit = _make_unit(phase="absorbing")
        session = LearningSession(
            id="sess-stale",
            mode=AgentMode.CHAT,
            learning_unit_id=unit.id,  # 但 get_learning_unit 未 patch
        )

        _, turn = await system._prepare_session_turn(
            session, "随便聊聊", AgentMode.CHAT
        )

        # 命中 legacy CHAT 分支：没有 learning_unit_phase 元数据
        meta = turn.profile.assistant_message_metadata
        assert "learning_unit_phase" not in meta
        assert turn.effective_mode == AgentMode.CHAT

    @pytest.mark.asyncio
    async def test_full_lifecycle_happy_path(self):
        """驱动一个学习卷走完 aligning → absorbing → outputting → consolidated。"""
        system = _build_system_stub()
        unit = _make_unit(phase="aligning")
        _attach_unit(system, unit)
        session = _make_session_with_unit(unit)

        # 1) aligning: ASK
        _, turn1 = await system._prepare_session_turn(session, "我想学这个", AgentMode.CHAT)
        assert turn1.effective_mode == AgentMode.ASK

        # 用户确认目标后，进入 absorbing
        unit.transition_to("absorbing")

        # 2) absorbing: CHAT
        _, turn2 = await system._prepare_session_turn(session, "讲讲", AgentMode.CHAT)
        assert turn2.effective_mode == AgentMode.CHAT

        # 用户主动结束吸收，进入 outputting
        unit.transition_to("outputting")
        unit.teach_session = TeachSession(
            questions=[
                TeachQuestion(concept_id="cpt-1", kind="sa", stem="?"),
            ],
            state="prompted",
        )

        # 3) outputting: TEACH
        _, turn3 = await system._prepare_session_turn(session, "答题", AgentMode.CHAT)
        assert turn3.effective_mode == AgentMode.TEACH

        # TEACH 通过后归档
        unit.transition_to("consolidated")
        assert unit.is_terminal()

        # 4) consolidated: 拒绝进一步对话
        with pytest.raises(ValueError):
            await system._prepare_session_turn(session, "再问一个", AgentMode.CHAT)


class TestSystemPromptToolGuardrails:
    """system prompt 的工具段必须与 visible_tools 一致，否则模型会幻觉式输出代码块。"""

    def test_ask_mode_omits_tool_usage_priority_section(self):
        prompt = build_system_prompt(AgentMode.ASK, resolve_persona(AgentMode.ASK))

        assert "工具使用优先级" not in prompt
        assert "工具限制" in prompt

    def test_chat_mode_keeps_tool_usage_priority_section(self):
        prompt = build_system_prompt(AgentMode.CHAT, resolve_persona(AgentMode.CHAT))

        assert "工具使用优先级" in prompt
        assert "grep" in prompt
        assert "read_file" in prompt

    def test_teach_mode_omits_tool_usage_priority_section(self):
        prompt = build_system_prompt(AgentMode.TEACH, resolve_persona(AgentMode.TEACH))

        assert "工具使用优先级" not in prompt
        assert "工具限制" in prompt


class TestPostAskTargetMetadata:
    """切入 Ask 时必须记录来源 mode，否则确认后会被错误地踢回 CHAT。"""

    def test_entering_ask_records_previous_mode_as_post_ask_target(self):
        sm = SessionManager()
        session = sm.create_session()
        sm.switch_session_mode(session.id, AgentMode.STUDY)

        switched = sm.switch_session_mode(session.id, AgentMode.ASK)

        assert switched.mode == AgentMode.ASK
        assert switched.mode_metadata["post_ask_target"] == AgentMode.STUDY.value

    def test_leaving_ask_clears_post_ask_target(self):
        sm = SessionManager()
        session = sm.create_session()
        sm.switch_session_mode(session.id, AgentMode.STUDY)
        sm.switch_session_mode(session.id, AgentMode.ASK)

        switched = sm.switch_session_mode(session.id, AgentMode.CHAT)

        assert switched.mode == AgentMode.CHAT
        assert "post_ask_target" not in switched.mode_metadata

    def test_entering_ask_from_chat_records_chat(self):
        sm = SessionManager()
        session = sm.create_session()  # default mode is CHAT

        switched = sm.switch_session_mode(session.id, AgentMode.ASK)

        assert switched.mode_metadata["post_ask_target"] == AgentMode.CHAT.value
