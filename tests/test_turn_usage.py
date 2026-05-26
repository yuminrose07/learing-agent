from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


from learning_agent.agent.agent_loop import AgentLoop, AgentLoopSession
from learning_agent.agent.observability import ObservabilityCollector
from learning_agent.ai import AgentMode, ChatChunk, ResilienceConfig
from learning_agent.ai.models import Event
from learning_agent.learning_agent.config import Config
from learning_agent.learning_agent.main import LearningAgentSystem
from learning_agent.learning_agent.mode_service import PreparedSessionTurn, build_turn_profile
from learning_agent.learning_agent.session_manager import SessionManager


class FakeUsageProvider:
    default_model = "gpt-4o"

    async def stream_chat(self, params):
        yield ChatChunk(content="对齐说明")
        yield ChatChunk(
            content="",
            finish_reason="stop",
            metadata={
                "provider_usage": {
                    "prompt_tokens": 128,
                    "completion_tokens": 16,
                    "total_tokens": 144,
                }
            },
        )

    async def chat(self, params):
        return ChatChunk(content="unused")

    def supports_tool_calling(self) -> bool:
        return False

    def supports_vision(self) -> bool:
        return False

    def get_max_context_length(self) -> int:
        return 128000


@pytest.mark.asyncio
async def test_single_pass_turn_persists_turn_usage_in_assistant_metadata():
    session_store = SessionManager()
    session = session_store.create_session(title="usage-test")
    provider = FakeUsageProvider()
    event_bus = MagicMock()
    event_bus.publish = AsyncMock()
    agent_loop = AgentLoop(
        provider=provider,
        memory_service=None,
        session_store=session_store,
        hook_system=MagicMock(),
        event_bus=event_bus,
        tool_execution_service=MagicMock(),
        observability=None,
        resilience_config=ResilienceConfig(),
    )
    runtime = AgentLoopSession(
        session_id=session.id,
        agent_loop=agent_loop,
        resilience_config=ResilienceConfig(),
    )
    profile = build_turn_profile(AgentMode.ASK)

    chunks = [
        chunk
        async for chunk in runtime._run_single_pass_turn(
            session,
            "帮我确认需求",
            profile,
            parent_span=None,
        )
    ]

    assert chunks[-1].metadata["turn_usage"]["actual_total_tokens"] == 144
    assert chunks[-1].metadata["turn_usage"]["is_estimated"] is False

    assistant_entry = session.entries[-1]
    persisted_usage = assistant_entry.metadata["turn_usage"]
    assert assistant_entry.role.value == "assistant"
    assert persisted_usage["estimated_prompt_tokens"] > 0
    assert persisted_usage["actual_prompt_tokens"] == 128
    assert persisted_usage["actual_completion_tokens"] == 16
    assert persisted_usage["actual_total_tokens"] == 144
    assert persisted_usage["is_estimated"] is False
    assert session.model_dump()["entries"][-1]["metadata"]["turn_usage"]["actual_total_tokens"] == 144


@pytest.mark.asyncio
async def test_stream_session_chat_merges_public_usage_metadata():
    system = LearningAgentSystem()
    system.session_manager = SessionManager()
    system.agent_loop = MagicMock()
    system.save_session = MagicMock(return_value=True)
    session = system.session_manager.create_session(title="stream-usage-test")
    system.get_session = MagicMock(return_value=session)

    profile = build_turn_profile(AgentMode.CHAT)
    prepared_turn = PreparedSessionTurn(
        effective_mode=AgentMode.CHAT,
        runtime_input="你好",
        profile=profile,
        stream_metadata=dict(profile.assistant_message_metadata),
        compaction_plan=None,
    )
    system._prepare_session_turn = AsyncMock(return_value=(session, prepared_turn))

    async def _run(*args, **kwargs):
        yield ChatChunk(
            content="你好",
            finish_reason="stop",
            metadata={
                "turn_usage": {
                    "estimated_prompt_tokens": 320,
                    "actual_total_tokens": 360,
                    "context_limit": 128000,
                    "utilization_ratio": 0.0025,
                    "is_estimated": False,
                    "compaction": {
                        "micro_compact_applied": True,
                        "full_compact_applied": False,
                        "summary_block_present": False,
                        "full_compact_scope": None,
                        "recent_token_budget": 16000,
                    },
                }
            },
        )

    system.agent_loop.run = _run

    chunks = [chunk async for chunk in system.stream_session_chat(session.id, "你好", mode=AgentMode.CHAT)]

    assert len(chunks) == 1
    metadata = chunks[0].metadata
    assert metadata["persona_key"] == profile.assistant_message_metadata["persona_key"]
    assert metadata["usage"]["actual_total_tokens"] == 360
    assert metadata["turn_usage"]["estimated_prompt_tokens"] == 320


def test_backfill_learning_unit_session_links_restores_session_projection(tmp_path: Path):
    config = Config()
    config.data_dir = str(tmp_path)
    system = LearningAgentSystem(config=config)
    system.session_manager._event_bus = None

    session = system.session_manager.create_session(title="learning-link")
    unit = system.learning_unit_store.create(
        session_id=session.id,
        objective_text="怎么学 agent",
    )

    assert session.learning_unit_id is None

    system._backfill_learning_unit_session_links()

    assert session.learning_unit_id == unit.id

    snapshot = system.session_manager.get_agent_snapshot(session.id)
    restored = snapshot.to_learning_session()
    assert snapshot.learning_unit_id == unit.id
    assert restored.learning_unit_id == unit.id


@pytest.mark.asyncio
async def test_observability_distinguishes_chunk_and_usage_metrics(tmp_path):
    collector = ObservabilityCollector()

    await collector.on_event(
        Event(
            type="agent.responseChunk",
            payload={"content": "你好"},
            source="test",
            session_id="sess-usage",
        )
    )
    await collector.on_event(
        Event(
            type="agent.turnUsage",
            payload={
                "phase": "response",
                "usage": {
                    "estimated_prompt_tokens": 320,
                    "actual_prompt_tokens": 344,
                    "actual_completion_tokens": 16,
                    "actual_total_tokens": 360,
                    "context_limit": 128000,
                    "utilization_ratio": 0.0025,
                    "is_estimated": False,
                },
            },
            source="test",
            session_id="sess-usage",
        )
    )

    summary = collector.get_metrics_summary()

    assert summary["counters"]["llm.chunk.received"] == 1
    assert "llm.token.received" not in summary["counters"]
    assert summary["counters"]["llm.usage.turns{is_estimated=false,phase=response}"] == 1
    assert summary["histograms"]["llm.usage.estimated_prompt_tokens"]["p50"] == 320
    assert summary["histograms"]["llm.usage.actual_prompt_tokens"]["p50"] == 344
    assert summary["histograms"]["llm.usage.actual_completion_tokens"]["p50"] == 16
    assert summary["histograms"]["llm.usage.actual_total_tokens"]["p50"] == 360
    assert summary["histograms"]["llm.usage.context_limit"]["p50"] == 128000
    assert summary["histograms"]["llm.usage.context_utilization_ratio"]["p50"] == 0.0025


@pytest.mark.asyncio
async def test_observability_skips_missing_actual_usage_metrics(tmp_path):
    collector = ObservabilityCollector()

    await collector.on_event(
        Event(
            type="agent.turnUsage",
            payload={
                "phase": "single_pass",
                "usage": {
                    "estimated_prompt_tokens": 128,
                    "context_limit": 128000,
                    "utilization_ratio": 0.001,
                    "is_estimated": True,
                },
            },
            source="test",
            session_id="sess-estimated",
        )
    )

    summary = collector.get_metrics_summary()

    assert summary["counters"]["llm.usage.turns{is_estimated=true,phase=single_pass}"] == 1
    assert summary["histograms"]["llm.usage.estimated_prompt_tokens"]["p50"] == 128
    assert "llm.usage.actual_total_tokens" not in summary["histograms"]
