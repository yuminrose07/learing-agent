from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai import AgentMode, ChatChunk, LearningSession
from learning_agent.learning_agent.main import LearningAgentSystem
from learning_agent.learning_agent.mode_service import (
    VIRTUOUS_CONSORT_PERSONA,
    SHU_CONSORT_PERSONA,
    TurnExecutionKind,
    build_turn_profile,
)


def _build_system_stub() -> LearningAgentSystem:
    system = LearningAgentSystem.__new__(LearningAgentSystem)
    system.agent_loop = MagicMock()
    system.save_session = MagicMock(return_value=True)
    system.update_session_mode = MagicMock()
    system.get_session = MagicMock()
    return system


class TestModeLayering:
    def test_build_turn_profile_includes_fixed_ask_persona_metadata(self):
        profile = build_turn_profile(
            AgentMode.ASK,
            assistant_message_metadata={"alignment": True},
        )

        assert profile.turn_kind == TurnExecutionKind.SINGLE_PASS
        assert profile.assistant_message_metadata["mode"] == "ask"
        assert profile.assistant_message_metadata["alignment"] is True
        assert profile.assistant_message_metadata["persona_name"] == "贵妃·顾明嫣"
        assert "先行对齐" in profile.system_prompt

    def test_build_turn_profile_randomizes_chat_persona_per_turn(self):
        with patch("learning_agent.learning_agent.mode_service.choice", return_value=SHU_CONSORT_PERSONA):
            profile = build_turn_profile(AgentMode.CHAT)

        assert profile.turn_kind == TurnExecutionKind.REACT
        assert profile.assistant_message_metadata["mode"] == "chat"
        assert profile.assistant_message_metadata["persona_key"] == SHU_CONSORT_PERSONA.key
        assert profile.assistant_message_metadata["persona_name"] == SHU_CONSORT_PERSONA.display_name
        assert SHU_CONSORT_PERSONA.display_name in profile.system_prompt

    def test_prepare_session_turn_binds_chat_persona_per_session(self):
        system = _build_system_stub()
        session = LearningSession(id="sess-chat", mode=AgentMode.CHAT)

        with patch("learning_agent.learning_agent.main.resolve_persona", return_value=SHU_CONSORT_PERSONA) as mock_resolve:
            _, first_turn = system._prepare_session_turn(session, "第一句", AgentMode.CHAT)
            _, second_turn = system._prepare_session_turn(session, "第二句", AgentMode.CHAT)

        assert session.mode_metadata["chat_persona_key"] == SHU_CONSORT_PERSONA.key
        assert first_turn.profile.assistant_message_metadata["persona_key"] == SHU_CONSORT_PERSONA.key
        assert second_turn.profile.assistant_message_metadata["persona_key"] == SHU_CONSORT_PERSONA.key
        assert first_turn.profile.assistant_message_metadata["persona_name"] == SHU_CONSORT_PERSONA.display_name
        assert second_turn.profile.assistant_message_metadata["persona_name"] == SHU_CONSORT_PERSONA.display_name
        mock_resolve.assert_called_once_with(AgentMode.CHAT)

    def test_prepare_session_turn_reuses_existing_chat_persona_after_mode_switch(self):
        system = _build_system_stub()
        session = LearningSession(
            id="sess-chat-reuse",
            mode=AgentMode.ASK,
            mode_metadata={"chat_persona_key": VIRTUOUS_CONSORT_PERSONA.key},
        )
        session.ask_state.status = "aligning"
        session.ask_state.confirmed_input = "继续回答我刚才的问题"

        switched_session = session.model_copy(deep=True)
        switched_session.mode = AgentMode.CHAT
        system.update_session_mode.return_value = switched_session

        prepared_session, prepared_turn = system._prepare_session_turn(
            session,
            "确认，开始吧",
            AgentMode.ASK,
        )

        assert prepared_session.mode == AgentMode.CHAT
        assert prepared_session.mode_metadata["chat_persona_key"] == VIRTUOUS_CONSORT_PERSONA.key
        assert prepared_turn.profile.assistant_message_metadata["persona_key"] == VIRTUOUS_CONSORT_PERSONA.key
        assert prepared_turn.profile.assistant_message_metadata["persona_name"] == VIRTUOUS_CONSORT_PERSONA.display_name

    def test_prepare_session_turn_keeps_ask_confirmation_in_product_layer(self):
        system = _build_system_stub()

        session = LearningSession(id="sess-ask", mode=AgentMode.ASK)
        session.ask_state.status = "aligning"
        session.ask_state.confirmed_input = "请先审查 runtime 与 product 的分层边界"

        switched_session = session.model_copy(deep=True)
        switched_session.mode = AgentMode.CHAT
        system.update_session_mode.return_value = switched_session

        prepared_session, prepared_turn = system._prepare_session_turn(
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
        assert chunks[0].metadata["mode"] == "ask"
        assert chunks[0].metadata["alignment"] is True
        assert chunks[0].metadata["persona_name"] == "贵妃·顾明嫣"

        _, run_kwargs = system.agent_loop.run.call_args
        assert run_kwargs["profile"].turn_kind == TurnExecutionKind.SINGLE_PASS
        assert run_kwargs["compaction_plan"].use_micro_compact is False
        assert run_kwargs["compaction_plan"].use_full_compact is False
        system.save_session.assert_not_called()
