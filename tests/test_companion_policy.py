from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai import AgentMode, LearningSession, LearningUnit, UnitObjective
from learning_agent.learning_agent.companion_policy import (
    CompanionIntent,
    CompanionStyle,
    classify_companion_intent,
    prepare_companion_turn,
)
from learning_agent.learning_agent.main import LearningAgentSystem
from learning_agent.learning_agent.session_events import SessionEventType


def _build_system_stub() -> LearningAgentSystem:
    system = LearningAgentSystem.__new__(LearningAgentSystem)
    system.agent_loop = MagicMock()
    system.save_session = MagicMock(return_value=True)
    system.get_session = MagicMock()
    system.session_event_store = MagicMock()
    learning_unit_store = MagicMock()
    learning_unit_store.get = MagicMock(return_value=None)
    system.learning_unit_store = learning_unit_store
    companion_intent_classifier = MagicMock()
    companion_intent_classifier.classify = AsyncMock(return_value=None)
    system.companion_intent_classifier = companion_intent_classifier
    return system


class TestCompanionPolicy:
    def test_tired_signal_builds_transient_warm_companion_plan(self):
        session = LearningSession(id="sess-companion", mode=AgentMode.CHAT)

        plan = prepare_companion_turn(
            session=session,
            user_input="我今天真的好累，哄我一下",
            requested_mode=AgentMode.CHAT,
        )

        assert plan.enabled is True
        assert plan.style == CompanionStyle.WARM_GIRLFRIEND
        assert plan.intent == CompanionIntent.TIRED
        assert plan.disable_tools is True
        assert plan.signal_detected is True
        assert plan.profile_changed is False
        assert plan.metadata_updates == {"companion_last_intent": "tired"}
        assert "亲密陪伴型闲聊" in (plan.prompt_addendum or "")

    def test_text_based_style_switch_no_longer_takes_effect(self):
        # picker 是唯一的风格控制入口；文本里的"切到女友模式"不再触发任何变化。
        session = LearningSession(id="sess-companion", mode=AgentMode.CHAT)

        plan = prepare_companion_turn(
            session=session,
            user_input="切到女友模式，陪我聊会儿",
            requested_mode=AgentMode.CHAT,
        )

        # "陪我" 命中 _TURN_TRIGGER_KEYWORDS，会以单轮 WARM_GIRLFRIEND 兜底，
        # 但绝不会把 profile_changed 置 True、也不会回写 session metadata。
        assert plan.profile_changed is False
        assert "chat_profile" not in plan.metadata_updates
        assert "companion_style" not in plan.metadata_updates

    def test_classifies_return_to_study_before_general_tiredness(self):
        assert classify_companion_intent("好了，我们继续学吧") == CompanionIntent.RETURN_TO_STUDY

    def test_intent_override_replaces_none_intent(self):
        session = LearningSession(id="sess-companion", mode=AgentMode.CHAT)

        plan = prepare_companion_turn(
            session=session,
            user_input="今天 PR 又被驳回了，真没意思",
            requested_mode=AgentMode.CHAT,
            intent_override=CompanionIntent.VENTING,
        )

        assert plan.enabled is True
        assert plan.style == CompanionStyle.WARM_GIRLFRIEND
        assert plan.intent == CompanionIntent.VENTING
        assert plan.disable_tools is True
        assert plan.signal_detected is True
        assert plan.message_metadata["companion_intent_source"] == "llm"

    def test_intent_override_ignored_when_keyword_hits(self):
        session = LearningSession(id="sess-companion", mode=AgentMode.CHAT)

        plan = prepare_companion_turn(
            session=session,
            user_input="我今天真的好累",
            requested_mode=AgentMode.CHAT,
            intent_override=CompanionIntent.VENTING,
        )

        assert plan.intent == CompanionIntent.TIRED
        assert plan.message_metadata["companion_intent_source"] == "keyword"

    def test_intent_override_none_keeps_keyword_result(self):
        session = LearningSession(id="sess-companion", mode=AgentMode.CHAT)

        plan = prepare_companion_turn(
            session=session,
            user_input="我今天真的好累",
            requested_mode=AgentMode.CHAT,
            intent_override=None,
        )

        assert plan.intent == CompanionIntent.TIRED
        assert plan.message_metadata["companion_intent_source"] == "keyword"

    def test_intent_override_none_value_does_not_force_companion(self):
        session = LearningSession(id="sess-companion", mode=AgentMode.CHAT)

        plan = prepare_companion_turn(
            session=session,
            user_input="帮我看看这个函数",
            requested_mode=AgentMode.CHAT,
            intent_override=CompanionIntent.NONE,
        )

        assert plan.enabled is False


class TestCompanionTurnPreparation:
    @pytest.mark.asyncio
    async def test_companion_turn_disables_tools_and_marks_metadata(self):
        system = _build_system_stub()
        session = LearningSession(id="sess-chat", mode=AgentMode.CHAT)

        _, turn = await system._prepare_session_turn(
            session,
            "我好累，哄我一下",
            AgentMode.CHAT,
        )

        assert turn.effective_mode == AgentMode.CHAT
        assert turn.profile.visible_tools == []
        assert turn.profile.assistant_message_metadata["chat_profile"] == "companion"
        assert turn.profile.assistant_message_metadata["companion_intent"] == "tired"
        assert "亲密陪伴型闲聊" in turn.profile.system_prompt
        system.session_event_store.append_event.assert_any_call(
            session.id,
            SessionEventType.COMPANION_SIGNAL_DETECTED,
            payload={
                "enabled": True,
                "style": "warm_girlfriend",
                "intent": "tired",
                "advice_level": "low",
                "profile_changed": False,
                "source": "keyword",
            },
        )

    @pytest.mark.asyncio
    async def test_text_based_style_switch_does_not_persist_session_metadata(self):
        # 用户即使在聊天中打"切到女友模式"，session metadata 也不能被回写——
        # 唯一的写入路径是 PUT /sessions/{id}/companion。
        system = _build_system_stub()
        session = LearningSession(id="sess-chat", mode=AgentMode.CHAT)

        _, turn = await system._prepare_session_turn(
            session,
            "切到女友模式",
            AgentMode.CHAT,
        )

        assert "chat_profile" not in session.mode_metadata
        assert "companion_style" not in session.mode_metadata
        # COMPANION_PROFILE_CHANGED 事件不应被发：profile_changed=False。
        for call in system.session_event_store.append_event.call_args_list:
            args, kwargs = call
            event_type = args[1] if len(args) > 1 else kwargs.get("event_type")
            assert event_type != SessionEventType.COMPANION_PROFILE_CHANGED

    @pytest.mark.asyncio
    async def test_text_based_disable_ignored_when_session_already_enabled(self):
        # 用户已在 picker 里开了陪伴；聊天里打"关闭女友模式"不能把 profile 清空——
        # 关掉陪伴只能走 picker / PUT API。
        system = _build_system_stub()
        session = LearningSession(
            id="sess-chat",
            mode=AgentMode.CHAT,
            mode_metadata={
                "chat_profile": "companion",
                "companion_style": "warm_girlfriend",
                "companion_last_intent": "tired",
            },
        )

        _, turn = await system._prepare_session_turn(
            session,
            "关闭女友模式",
            AgentMode.CHAT,
        )

        # session metadata 保持原样
        assert session.mode_metadata["chat_profile"] == "companion"
        assert session.mode_metadata["companion_style"] == "warm_girlfriend"
        # 本轮仍按陪伴跑
        assert turn.profile.assistant_message_metadata["companion_enabled"] is True

    def test_learning_unit_terminal_event_suggests_companion_recovery(self):
        system = _build_system_stub()
        unit = LearningUnit(
            session_id="sess-lu",
            objective=UnitObjective(text="理解 async"),
        )
        unit.phase = "stopped"  # type: ignore[assignment]

        system._emit_unit_event(unit, SessionEventType.LEARNING_UNIT_STOPPED)

        system.session_event_store.append_event.assert_any_call(
            unit.session_id,
            SessionEventType.COMPANION_RECOVERY_SUGGESTED,
            payload={
                "learning_unit_id": unit.id,
                "source_event": SessionEventType.LEARNING_UNIT_STOPPED,
                "suggested_style": "warm_girlfriend",
                "reason": "study_recovery",
            },
        )


class TestCompanionIntentLLMIntegration:
    @pytest.mark.asyncio
    async def test_llm_intent_classifier_invoked_when_keyword_misses(self):
        system = _build_system_stub()
        system.companion_intent_classifier.classify = AsyncMock(
            return_value=CompanionIntent.VENTING
        )
        session = LearningSession(id="sess-chat", mode=AgentMode.CHAT)

        _, turn = await system._prepare_session_turn(
            session,
            "今天 PR 又被驳回了，真没意思",
            AgentMode.CHAT,
        )

        system.companion_intent_classifier.classify.assert_awaited_once()
        assert turn.profile.assistant_message_metadata["companion_intent"] == "venting"
        assert (
            turn.profile.assistant_message_metadata["companion_intent_source"] == "llm"
        )
        system.session_event_store.append_event.assert_any_call(
            session.id,
            SessionEventType.COMPANION_SIGNAL_DETECTED,
            payload={
                "enabled": True,
                "style": "warm_girlfriend",
                "intent": "venting",
                "advice_level": "low",
                "profile_changed": False,
                "source": "llm",
            },
        )

    @pytest.mark.asyncio
    async def test_llm_classifier_skipped_for_short_input(self):
        system = _build_system_stub()
        session = LearningSession(id="sess-chat", mode=AgentMode.CHAT)

        _, turn = await system._prepare_session_turn(session, "嗯。", AgentMode.CHAT)

        system.companion_intent_classifier.classify.assert_not_awaited()
        # 普通短回复，不触发陪伴
        assert turn.profile.assistant_message_metadata.get("companion_enabled") is not True

    @pytest.mark.asyncio
    async def test_llm_classifier_skipped_when_keyword_hits(self):
        system = _build_system_stub()
        session = LearningSession(id="sess-chat", mode=AgentMode.CHAT)

        _, turn = await system._prepare_session_turn(
            session,
            "我今天真的好累",
            AgentMode.CHAT,
        )

        system.companion_intent_classifier.classify.assert_not_awaited()
        assert turn.profile.assistant_message_metadata["companion_intent"] == "tired"
        assert (
            turn.profile.assistant_message_metadata["companion_intent_source"]
            == "keyword"
        )

    @pytest.mark.asyncio
    async def test_llm_classifier_returning_none_keeps_normal_chat(self):
        system = _build_system_stub()
        system.companion_intent_classifier.classify = AsyncMock(return_value=None)
        session = LearningSession(id="sess-chat", mode=AgentMode.CHAT)

        _, turn = await system._prepare_session_turn(
            session,
            "帮我把这段重构成 generator",
            AgentMode.CHAT,
        )

        system.companion_intent_classifier.classify.assert_awaited_once()
        assert turn.profile.assistant_message_metadata.get("companion_enabled") is not True
