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
    MessageRole,
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
    build_turn_profile,
)


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
    phase: str = "absorbing",
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
        # 新主链：默认 absorbing 起步（adaptive alignment §8.1，aligning 已移除）
        assert unit.phase == "absorbing"

        # absorbing → outputting OK
        assert unit.can_transition_to("outputting")
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
    async def test_prepare_session_turn_absorbing_with_vague_input_yields_ask(self):
        """absorbing + 模糊输入：策略判 active 时 Product 层临时覆写为 ASK。"""
        system = _build_system_stub()
        unit = _make_unit(phase="absorbing")
        _attach_unit(system, unit)
        session = _make_session_with_unit(unit)

        # "讲讲这个" 是代词指代，即便 concept_list 非空也不豁免
        _, turn = await system._prepare_session_turn(session, "讲讲这个", AgentMode.CHAT)

        assert turn.effective_mode == AgentMode.ASK
        meta = turn.profile.assistant_message_metadata
        assert meta["mode"] == "ask"
        assert meta["alignment"] is True
        assert meta["alignment_state"] == "active"
        assert meta["alignment_reason"] == "missing_learnable_target"
        # unit 状态写回 + 限流计数 +1
        assert unit.alignment_state == "active"
        assert unit.clarification_count == 1

    @pytest.mark.asyncio
    async def test_prepare_session_turn_clarification_rate_limited_to_once(self):
        """§9.3 第 1 条护栏：单卷启动期阻塞澄清最多 1 次，第二次模糊输入强制走 CHAT。"""
        system = _build_system_stub()
        unit = _make_unit(phase="absorbing")
        _attach_unit(system, unit)
        session = _make_session_with_unit(unit)

        # 第一轮：触发澄清
        _, turn1 = await system._prepare_session_turn(session, "讲讲这个", AgentMode.CHAT)
        assert turn1.effective_mode == AgentMode.ASK
        assert unit.clarification_count == 1

        # 第二轮：同样模糊但限流命中 → 强制 CHAT，不再递增
        _, turn2 = await system._prepare_session_turn(session, "再讲讲", AgentMode.CHAT)
        assert turn2.effective_mode == AgentMode.CHAT
        assert unit.clarification_count == 1
        meta2 = turn2.profile.assistant_message_metadata
        assert meta2["alignment_state"] == "idle"

    @pytest.mark.asyncio
    async def test_prepare_session_turn_suggestion_count_bumps_and_caps(self):
        """§9.3 第 2 条护栏：suggested 写入时 suggestion_count +1，超过 2 后降级为 none。"""
        system = _build_system_stub()
        unit = _make_unit(phase="absorbing")
        _attach_unit(system, unit)
        session = _make_session_with_unit(unit)

        # 第 1 次 B 档输入：策略判 suggested，counter 0 → 1
        _, t1 = await system._prepare_session_turn(session, "教我整个项目", AgentMode.CHAT)
        assert t1.profile.assistant_message_metadata["alignment_state"] == "suggested"
        assert unit.suggestion_count == 1

        # 第 2 次：counter 1 → 2，仍 suggested
        _, t2 = await system._prepare_session_turn(session, "教我整个项目", AgentMode.CHAT)
        assert t2.profile.assistant_message_metadata["alignment_state"] == "suggested"
        assert unit.suggestion_count == 2

        # 第 3 次：counter 已达上限，策略降级为 none → alignment_state 回到 idle
        _, t3 = await system._prepare_session_turn(session, "教我整个项目", AgentMode.CHAT)
        meta3 = t3.profile.assistant_message_metadata
        assert meta3["alignment_state"] == "idle"
        assert "alignment_reason" not in meta3  # none 不应暴露原因
        # counter 不再继续涨
        assert unit.suggestion_count == 2

    @pytest.mark.asyncio
    async def test_prepare_session_turn_cooldown_ticks_down_each_absorbing_turn(self):
        """§9.3 第 3 条护栏：nag_cooldown_remaining 每个 absorbing turn 减 1，期间 B 档降级。"""
        system = _build_system_stub()
        unit = _make_unit(phase="absorbing")
        # 模拟用户已 accept_assumption，进入 3 轮冷静期
        unit.nag_cooldown_remaining = 3
        _attach_unit(system, unit)
        session = _make_session_with_unit(unit)

        # 第 1 轮：B 档输入应被降级；冷静期 3 → 2
        _, t1 = await system._prepare_session_turn(session, "教我整个项目", AgentMode.CHAT)
        assert t1.profile.assistant_message_metadata["alignment_state"] == "idle"
        assert unit.nag_cooldown_remaining == 2

        # 第 2 轮：仍降级；2 → 1
        _, t2 = await system._prepare_session_turn(session, "教我整个项目", AgentMode.CHAT)
        assert t2.profile.assistant_message_metadata["alignment_state"] == "idle"
        assert unit.nag_cooldown_remaining == 1

        # 第 3 轮：仍降级；1 → 0
        _, t3 = await system._prepare_session_turn(session, "教我整个项目", AgentMode.CHAT)
        assert t3.profile.assistant_message_metadata["alignment_state"] == "idle"
        assert unit.nag_cooldown_remaining == 0

        # 第 4 轮：冷静期结束，B 档恢复
        _, t4 = await system._prepare_session_turn(session, "教我整个项目", AgentMode.CHAT)
        assert t4.profile.assistant_message_metadata["alignment_state"] == "suggested"
        assert unit.nag_cooldown_remaining == 0  # 已经触底不再减

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
        """驱动一个学习卷走完 absorbing → outputting → consolidated。

        B1 之后 aligning 已从主链移除（adaptive alignment §8.1）；
        absorbing 阶段是否临时覆写为 ASK 由 alignment_state 决定，B2 实现。
        """
        system = _build_system_stub()
        unit = _make_unit(phase="absorbing")
        _attach_unit(system, unit)
        session = _make_session_with_unit(unit)

        # 1) absorbing: CHAT — 输入命中 concept_list (BaseModel) 走 A 档
        _, turn1 = await system._prepare_session_turn(session, "讲讲 BaseModel", AgentMode.CHAT)
        assert turn1.effective_mode == AgentMode.CHAT

        # 用户主动结束吸收，进入 outputting
        unit.transition_to("outputting")
        unit.teach_session = TeachSession(
            questions=[
                TeachQuestion(concept_id="cpt-1", kind="sa", stem="?"),
            ],
            state="prompted",
        )

        # 2) outputting: TEACH
        _, turn2 = await system._prepare_session_turn(session, "答题", AgentMode.CHAT)
        assert turn2.effective_mode == AgentMode.TEACH

        # TEACH 通过后归档
        unit.transition_to("consolidated")
        assert unit.is_terminal()

        # 4) consolidated: 拒绝进一步对话
        with pytest.raises(ValueError):
            await system._prepare_session_turn(session, "再问一个", AgentMode.CHAT)


class TestAbsorbingOpeningTemplate:
    """B3: absorbing 首轮的 system prompt addendum 注入（adaptive alignment §6.1 / §6.2）。"""

    @pytest.mark.asyncio
    async def test_first_absorbing_turn_injects_opening_template(self):
        system = _build_system_stub()
        unit = _make_unit(phase="absorbing")
        _attach_unit(system, unit)
        session = _make_session_with_unit(unit)
        # session.entries 为空，命中"首轮"

        _, turn = await system._prepare_session_turn(
            session, "讲讲 BaseModel", AgentMode.CHAT
        )

        prompt = turn.profile.system_prompt
        assert "工作目标卡片" in prompt
        assert "学习地图" in prompt
        assert "第一段实质讲解" in prompt
        # 学习目标原文必须被嵌入，供 LLM 复述
        assert unit.objective.text in prompt
        # A 档默认不带 suggestion 段
        assert "收窄建议" not in prompt

    @pytest.mark.asyncio
    async def test_first_absorbing_turn_with_suggested_appends_narrowing_block(self):
        """B 档 alignment_state=suggested 时附加收窄建议段。"""
        system = _build_system_stub()
        unit = _make_unit(phase="absorbing")
        _attach_unit(system, unit)
        session = _make_session_with_unit(unit)

        # "教我整个项目" 命中 too_broad → B 档
        _, turn = await system._prepare_session_turn(
            session, "教我整个项目", AgentMode.CHAT
        )

        assert turn.effective_mode == AgentMode.CHAT
        assert unit.alignment_state == "suggested"
        prompt = turn.profile.system_prompt
        assert "工作目标卡片" in prompt
        assert "收窄建议" in prompt

    @pytest.mark.asyncio
    async def test_non_first_absorbing_turn_skips_opening_template(self):
        """已有 assistant 消息时不再注入开场模板。"""
        system = _build_system_stub()
        unit = _make_unit(phase="absorbing")
        _attach_unit(system, unit)
        session = _make_session_with_unit(unit)
        # 模拟首轮已经发生过一次回答
        from learning_agent.ai.models import SessionEntry

        session.entries.append(
            SessionEntry(role=MessageRole.ASSISTANT, content="先前的回答。")
        )

        _, turn = await system._prepare_session_turn(
            session, "再讲讲 BaseModel", AgentMode.CHAT
        )

        assert "工作目标卡片" not in turn.profile.system_prompt

    @pytest.mark.asyncio
    async def test_ask_overlay_does_not_get_opening_template(self):
        """C 档（active）走 ASK，开场模板不应注入到 ASK turn 上。"""
        system = _build_system_stub()
        unit = _make_unit(phase="absorbing")
        _attach_unit(system, unit)
        session = _make_session_with_unit(unit)

        # "讲讲这个" → C 档 → ASK 覆写
        _, turn = await system._prepare_session_turn(
            session, "讲讲这个", AgentMode.CHAT
        )

        assert turn.effective_mode == AgentMode.ASK
        assert "工作目标卡片" not in turn.profile.system_prompt

    @pytest.mark.asyncio
    async def test_outputting_turn_has_no_opening_template(self):
        system = _build_system_stub()
        teach = TeachSession(
            questions=[TeachQuestion(concept_id="cpt-1", kind="sa", stem="?")],
            state="prompted",
        )
        unit = _make_unit(phase="outputting", teach_session=teach)
        _attach_unit(system, unit)
        session = _make_session_with_unit(unit)

        _, turn = await system._prepare_session_turn(
            session, "我准备好答题了", AgentMode.CHAT
        )

        assert turn.effective_mode == AgentMode.TEACH
        assert "工作目标卡片" not in turn.profile.system_prompt


class TestNarrowingActions:
    """B4 D3: 三个收窄动作端点 (request_alignment / accept_assumption / refine_objective)。

    系统层方法的纯逻辑测试 —— 不验 HTTP 层，只验 unit 状态变更与持锁/落盘。
    """

    def _wire_store_for_unit(self, system: LearningAgentSystem, unit: LearningUnit):
        """让 store.get(unit.id) 返回真实 unit，save 收 unit 引用。"""
        system.learning_unit_store.get = MagicMock(
            side_effect=lambda uid: unit if uid == unit.id else None
        )
        system.learning_unit_store.save = MagicMock()

    @pytest.mark.asyncio
    async def test_request_alignment_pulls_to_active_user_request(self):
        system = _build_system_stub()
        unit = _make_unit(phase="absorbing")
        self._wire_store_for_unit(system, unit)

        result = await system.request_alignment(unit.id)

        assert result is unit
        assert unit.alignment_state == "active"
        assert unit.alignment_reason == "user_request"
        assert unit.assumption_note == ""
        assert unit.last_alignment_at is not None
        system.learning_unit_store.save.assert_called_once_with(unit)

    @pytest.mark.asyncio
    async def test_request_alignment_unknown_unit_raises_keyerror(self):
        system = _build_system_stub()
        system.learning_unit_store.get = MagicMock(return_value=None)
        with pytest.raises(KeyError):
            await system.request_alignment("lu-missing")

    @pytest.mark.asyncio
    async def test_request_alignment_rejected_in_outputting_phase(self):
        system = _build_system_stub()
        unit = _make_unit(phase="outputting")
        self._wire_store_for_unit(system, unit)
        with pytest.raises(ValueError, match="absorbing"):
            await system.request_alignment(unit.id)

    @pytest.mark.asyncio
    async def test_accept_assumption_sets_skipped_with_cooldown(self):
        from learning_agent.learning_agent.alignment_policy import (
            COOLDOWN_AFTER_ACCEPT_ASSUMPTION,
        )

        system = _build_system_stub()
        unit = _make_unit(phase="absorbing")
        unit.alignment_state = "suggested"  # 模拟 UI 上正显示建议条
        self._wire_store_for_unit(system, unit)

        result = await system.accept_assumption(unit.id)

        assert result is unit
        assert unit.alignment_state == "skipped"
        assert unit.nag_cooldown_remaining == COOLDOWN_AFTER_ACCEPT_ASSUMPTION
        system.learning_unit_store.save.assert_called_once_with(unit)

    @pytest.mark.asyncio
    async def test_refine_objective_replaces_text_and_marks_refined(self):
        system = _build_system_stub()
        unit = _make_unit(phase="absorbing")
        self._wire_store_for_unit(system, unit)

        result = await system.refine_objective(unit.id, "  专注 BaseModel 校验链  ")

        assert result is unit
        assert unit.objective.text == "专注 BaseModel 校验链"  # strip 过
        assert unit.objective_status == "refined"
        assert unit.alignment_state == "resolved"
        assert unit.alignment_reason == "user_request"
        assert unit.assumption_note == ""
        system.learning_unit_store.save.assert_called_once_with(unit)

    @pytest.mark.asyncio
    async def test_refine_objective_empty_text_raises_valueerror(self):
        system = _build_system_stub()
        unit = _make_unit(phase="absorbing")
        self._wire_store_for_unit(system, unit)
        with pytest.raises(ValueError, match="empty"):
            await system.refine_objective(unit.id, "   ")
        # 不应触碰 unit
        system.learning_unit_store.save.assert_not_called()


class TestUserRequestedAlignmentFlow:
    """B4 D3: /align 之后的下一轮调度 —— 必须走 ASK，本轮结束后转 resolved。"""

    @pytest.mark.asyncio
    async def test_next_turn_after_request_alignment_yields_ask(self):
        system = _build_system_stub()
        unit = _make_unit(phase="absorbing")
        # 模拟 /align 端点写入的状态
        unit.alignment_state = "active"
        unit.alignment_reason = "user_request"
        _attach_unit(system, unit)
        session = _make_session_with_unit(unit)

        # 下一轮即便是清晰输入，也应被强制 ASK
        _, turn = await system._prepare_session_turn(
            session, "讲讲 BaseModel", AgentMode.CHAT
        )

        assert turn.effective_mode == AgentMode.ASK
        meta = turn.profile.assistant_message_metadata
        assert meta["alignment"] is True
        assert meta["alignment_reason"] == "user_request"
        # 一次性消费：apply 后 state → resolved，clarification_count 未递增
        assert unit.alignment_state == "resolved"
        assert unit.clarification_count == 0

    @pytest.mark.asyncio
    async def test_third_turn_after_request_alignment_returns_to_heuristic(self):
        """user_request 是一次性的：下一轮立即消费，再下一轮走 heuristic。"""
        system = _build_system_stub()
        unit = _make_unit(phase="absorbing")
        unit.alignment_state = "active"
        unit.alignment_reason = "user_request"
        _attach_unit(system, unit)
        session = _make_session_with_unit(unit)

        # 第 1 轮：消费 user_request → ASK，state 转 resolved
        _, t1 = await system._prepare_session_turn(
            session, "讲讲 BaseModel", AgentMode.CHAT
        )
        assert t1.effective_mode == AgentMode.ASK

        # 第 2 轮：state 已 resolved，正常走 heuristic；清晰输入 → CHAT
        _, t2 = await system._prepare_session_turn(
            session, "讲讲 BaseModel", AgentMode.CHAT
        )
        assert t2.effective_mode == AgentMode.CHAT
        assert unit.alignment_state == "idle"

    @pytest.mark.asyncio
    async def test_user_request_bypasses_clarification_rate_limit(self):
        """即便启动期已经用掉 1 次澄清额度，/align 仍应触发 ASK。"""
        system = _build_system_stub()
        unit = _make_unit(phase="absorbing")
        unit.clarification_count = 1  # 已经被系统打断过一次
        unit.alignment_state = "active"
        unit.alignment_reason = "user_request"
        _attach_unit(system, unit)
        session = _make_session_with_unit(unit)

        _, turn = await system._prepare_session_turn(
            session, "讲讲 BaseModel", AgentMode.CHAT
        )

        assert turn.effective_mode == AgentMode.ASK
        # 不计入限流额度
        assert unit.clarification_count == 1
