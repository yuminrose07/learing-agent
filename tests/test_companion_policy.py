from __future__ import annotations

import sys
from unittest.mock import MagicMock

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
    system.update_session_mode = MagicMock()
    system.get_session = MagicMock()
    system.session_event_store = MagicMock()
    learning_unit_store = MagicMock()
    learning_unit_store.get = MagicMock(return_value=None)
    system.learning_unit_store = learning_unit_store
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

    def test_explicit_girlfriend_mode_persists_companion_profile(self):
        session = LearningSession(id="sess-companion", mode=AgentMode.CHAT)

        plan = prepare_companion_turn(
            session=session,
            user_input="切到女友模式，陪我聊会儿",
            requested_mode=AgentMode.CHAT,
        )

        assert plan.profile_changed is True
        assert plan.metadata_updates["chat_profile"] == "companion"
        assert plan.metadata_updates["companion_style"] == "warm_girlfriend"

    def test_classifies_return_to_study_before_general_tiredness(self):
        assert classify_companion_intent("好了，我们继续学吧") == CompanionIntent.RETURN_TO_STUDY


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
            },
        )

    @pytest.mark.asyncio
    async def test_explicit_companion_style_updates_session_metadata(self):
        system = _build_system_stub()
        session = LearningSession(id="sess-chat", mode=AgentMode.CHAT)

        _, turn = await system._prepare_session_turn(
            session,
            "切到女友模式",
            AgentMode.CHAT,
        )

        assert session.mode_metadata["chat_profile"] == "companion"
        assert session.mode_metadata["companion_style"] == "warm_girlfriend"
        assert turn.profile.assistant_message_metadata["companion_style"] == "warm_girlfriend"
        system.session_event_store.append_event.assert_any_call(
            session.id,
            SessionEventType.COMPANION_PROFILE_CHANGED,
            payload={
                "enabled": True,
                "style": "warm_girlfriend",
                "intent": "none",
                "advice_level": "low",
                "metadata_changed": {
                    "chat_profile": "companion",
                    "companion_style": "warm_girlfriend",
                    "companion_advice_level": "low",
                    "companion_last_intent": "none",
                },
            },
        )

    @pytest.mark.asyncio
    async def test_disable_companion_profile_clears_metadata(self):
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

        assert "chat_profile" not in session.mode_metadata
        assert "companion_style" not in session.mode_metadata
        assert turn.profile.visible_tools != []
        assert turn.profile.assistant_message_metadata["companion_enabled"] is False

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
