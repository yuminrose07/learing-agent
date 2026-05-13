"""
Web 前后端适配方案 —— 测试套件

覆盖场景：
1. 删除 Session 时清理运行时（P0）
2. 系统启动/关闭时清理所有运行时（P1）
3. 观测 API 返回活跃运行时（P2）
4. Session 强制重置 API（P2）
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.agent.agent_loop import AgentLoop, AgentLoopSession, AgentState
from learning_agent.ai import KnowledgeNode, LearningSession, ResilienceConfig, SessionEntry


@pytest.fixture
def mock_system():
    """构造一个最小可工作的 Mock LearningAgentSystem。"""
    system = MagicMock()

    # AgentLoop
    system.agent_loop = MagicMock(spec=AgentLoop)

    # FileStore / Observability
    system.file_store = MagicMock()
    system.observability = MagicMock()
    system.observability.data_dir = "/tmp/obs"
    system.get_session = MagicMock(return_value=None)
    system.list_sessions = MagicMock(return_value=[])
    system.list_objectives = MagicMock(return_value=[])
    system.get_objective = MagicMock(return_value=None)
    system.create_session = MagicMock()
    system.stream_session_chat = MagicMock()
    system.collect_session_chat = AsyncMock(return_value="")
    system.fork_session_entry = MagicMock(return_value=None)
    system.confirm_knowledge_candidate = AsyncMock(return_value=None)
    system.save_session = MagicMock(return_value=True)
    system.save_state = AsyncMock(return_value=None)
    system.delete_session = AsyncMock(return_value=False)
    system.reset_session_runtime = MagicMock(return_value=None)
    system.clear_all_runtimes = MagicMock()
    system.get_runtime_overview = MagicMock(
        return_value={
            "active_runtime_count": 0,
            "total_session_count": 0,
            "runtimes": [],
        }
    )

    return system


class TestSessionEndpointsUseSystemApi:
    @pytest.mark.asyncio
    async def test_create_session_does_not_manually_save(self, mock_system):
        from learning_agent.web.web_server import create_session, CreateSessionRequest

        session = LearningSession(title="Web Session")
        mock_system.create_session.return_value = session

        with patch("learning_agent.web_server._get_system", return_value=mock_system):
            result = await create_session(CreateSessionRequest(title="Web Session"))

        assert result["id"] == session.id
        mock_system.create_session.assert_called_once_with(
            objective_id=None,
            title="Web Session",
        )
        mock_system.save_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_session_uses_system_side_persistence(self, mock_system):
        from learning_agent.web.web_server import update_session, UpdateSessionRequest

        session = LearningSession(id="sess-update", title="Renamed")
        mock_system.update_session_title = MagicMock(return_value=session)

        with patch("learning_agent.web_server._get_system", return_value=mock_system):
            result = await update_session("sess-update", UpdateSessionRequest(title="Renamed"))

        assert result["title"] == "Renamed"
        mock_system.update_session_title.assert_called_once_with("sess-update", "Renamed")
        mock_system.save_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_session_uses_system_lookup(self, mock_system):
        from learning_agent.web.web_server import get_session

        session = LearningSession(id="sess-query", title="Queried")
        mock_system.get_session.return_value = session

        with patch("learning_agent.web_server._get_system", return_value=mock_system):
            result = await get_session("sess-query")

        assert result["id"] == "sess-query"
        mock_system.get_session.assert_called_once_with("sess-query")

    @pytest.mark.asyncio
    async def test_fork_session_uses_system_api(self, mock_system):
        from learning_agent.web.web_server import fork_session, ForkRequest

        fork_entry = SessionEntry(id="entry-fork", content="User initiated fork via web API")
        mock_system.fork_session_entry.return_value = fork_entry

        with patch("learning_agent.web_server._get_system", return_value=mock_system):
            result = await fork_session("sess-123", ForkRequest(entry_id="entry-parent"))

        assert result["id"] == "entry-fork"
        mock_system.fork_session_entry.assert_called_once_with(
            "sess-123",
            "entry-parent",
            fork_content="User initiated fork via web API",
        )

    @pytest.mark.asyncio
    async def test_non_stream_chat_uses_system_api(self, mock_system):
        from learning_agent.web.web_server import chat, ChatRequest

        mock_system.collect_session_chat = AsyncMock(return_value="full response")

        with patch("learning_agent.web_server._get_system", return_value=mock_system):
            result = await chat("sess-chat", ChatRequest(message="hi", stream=False, ask_mode=True))

        assert result == {
            "session_id": "sess-chat",
            "content": "full response",
        }
        mock_system.collect_session_chat.assert_awaited_once_with(
            "sess-chat",
            "hi",
            ask_mode=True,
        )

    @pytest.mark.asyncio
    async def test_stream_chat_helper_uses_system_api(self, mock_system):
        from learning_agent.web.web_server import _stream_chat_chunks

        async def _chunks():
            yield MagicMock(content="part-1", tool_call=None, finish_reason=None)
            yield MagicMock(content="part-2", tool_call=None, finish_reason="stop")

        mock_system.stream_session_chat.return_value = _chunks()

        payloads = []
        async for payload in _stream_chat_chunks(mock_system, "sess-stream", "hi", ask_mode=True):
            payloads.append(payload)

        assert payloads[0].startswith("data: ")
        assert payloads[-1] == "data: [DONE]\n\n"
        mock_system.stream_session_chat.assert_called_once_with("sess-stream", "hi", ask_mode=True)

    @pytest.mark.asyncio
    async def test_confirm_knowledge_uses_system_api(self, mock_system):
        from learning_agent.web.web_server import confirm_knowledge

        mock_system.confirm_knowledge_candidate = AsyncMock(
            return_value=KnowledgeNode(id="kn-001", content="confirmed knowledge")
        )

        with patch("learning_agent.web_server._get_system", return_value=mock_system):
            result = await confirm_knowledge("kn-001")

        assert result == {"status": "confirmed", "node_id": "kn-001"}
        mock_system.confirm_knowledge_candidate.assert_awaited_once_with("kn-001", source="web")

    @pytest.mark.asyncio
    async def test_save_state_uses_system_api(self, mock_system):
        from learning_agent.web.web_server import save_state

        mock_system.save_state = AsyncMock(return_value=None)

        with patch("learning_agent.web_server._get_system", return_value=mock_system):
            result = await save_state()

        assert result == {"status": "saved"}
        mock_system.save_state.assert_awaited_once_with()


# ───────────────────────────────────────────────────────────────
# P0: 删除 Session 时清理运行时
# ───────────────────────────────────────────────────────────────

class TestDeleteSessionClearsRuntime:
    @pytest.mark.asyncio
    async def test_delete_existing_session(self, mock_system):
        from learning_agent.web.web_server import delete_session

        mock_system.delete_session = AsyncMock(return_value=True)
        with patch("learning_agent.web_server._get_system", return_value=mock_system):
            result = await delete_session("sess-001")

        assert result["status"] == "deleted"
        mock_system.delete_session.assert_awaited_once_with("sess-001")

    @pytest.mark.asyncio
    async def test_delete_nonexistent_session(self, mock_system):
        from learning_agent.web.web_server import delete_session
        from fastapi import HTTPException

        mock_system.delete_session = AsyncMock(return_value=False)
        with patch("learning_agent.web_server._get_system", return_value=mock_system):
            with pytest.raises(HTTPException) as exc_info:
                await delete_session("nonexist")
        assert exc_info.value.status_code == 404
        mock_system.delete_session.assert_awaited_once_with("nonexist")


# ───────────────────────────────────────────────────────────────
# P2: 重置 Session 运行时
# ───────────────────────────────────────────────────────────────

class TestResetRuntimeEndpoint:
    @pytest.mark.asyncio
    async def test_reset_existing_session(self, mock_system):
        from learning_agent.web.web_server import reset_session_runtime

        mock_system.reset_session_runtime = MagicMock(
            return_value={
                "session_id": "sess-002",
                "had_runtime": True,
                "cleared_runtime": {
                    "session_id": "sess-002",
                    "state": "idle",
                    "chat_only_mode": False,
                    "chat_only_success_turns": 0,
                    "turn_count": 2,
                    "failure_tracker": {
                        "tracked_tools": [],
                        "banned_tools": [],
                        "tools": {},
                    },
                    "lock_acquired": False,
                    "last_accessed": 1234567890.0,
                    "trace": None,
                },
            }
        )
        with patch("learning_agent.web_server._get_system", return_value=mock_system):
            result = await reset_session_runtime("sess-002")

        assert result["status"] == "runtime_reset"
        assert result["runtime"]["had_runtime"] is True
        assert result["runtime"]["cleared_runtime"]["session_id"] == "sess-002"
        mock_system.reset_session_runtime.assert_called_once_with("sess-002")

    @pytest.mark.asyncio
    async def test_reset_nonexistent_session(self, mock_system):
        from learning_agent.web.web_server import reset_session_runtime
        from fastapi import HTTPException

        mock_system.reset_session_runtime = MagicMock(return_value=None)
        with patch("learning_agent.web_server._get_system", return_value=mock_system):
            with pytest.raises(HTTPException) as exc_info:
                await reset_session_runtime("nonexist")
        assert exc_info.value.status_code == 404
        mock_system.reset_session_runtime.assert_called_once_with("nonexist")


# ───────────────────────────────────────────────────────────────
# P2: 观测运行时 API
# ───────────────────────────────────────────────────────────────

class TestObservabilityRuntimes:
    @pytest.mark.asyncio
    async def test_empty_runtimes(self, mock_system):
        from learning_agent.web.web_server import get_runtimes

        mock_system.get_runtime_overview.return_value = {
            "active_runtime_count": 0,
            "total_session_count": 0,
            "runtimes": [],
        }
        with patch("learning_agent.web_server._get_system", return_value=mock_system):
            result = await get_runtimes()

        assert result["active_runtime_count"] == 0
        assert result["runtimes"] == []

    @pytest.mark.asyncio
    async def test_with_active_runtimes(self, mock_system):
        from learning_agent.web.web_server import get_runtimes

        mock_system.get_runtime_overview.return_value = {
            "active_runtime_count": 1,
            "total_session_count": 1,
            "runtimes": [
                {
                    "session_id": "sess-003",
                    "state": "idle",
                    "chat_only_mode": False,
                    "chat_only_success_turns": 2,
                    "failure_tracker": {
                        "tracked_tools": [],
                        "banned_tools": [],
                    },
                    "lock_acquired": False,
                    "last_accessed": 1234567890.0,
                    "trace": None,
                }
            ],
        }

        with patch("learning_agent.web_server._get_system", return_value=mock_system):
            result = await get_runtimes()

        assert result["active_runtime_count"] == 1
        assert len(result["runtimes"]) == 1
        r = result["runtimes"][0]
        assert r["session_id"] == "sess-003"
        assert r["state"] == "idle"
        assert r["chat_only_mode"] is False
        assert r["chat_only_success_turns"] == 2
        assert r["last_accessed"] == 1234567890.0

    @pytest.mark.asyncio
    async def test_chat_only_runtime_highlighted(self, mock_system):
        from learning_agent.web.web_server import get_runtimes

        mock_system.get_runtime_overview.return_value = {
            "active_runtime_count": 1,
            "total_session_count": 1,
            "runtimes": [
                {
                    "session_id": "sess-004",
                    "state": "idle",
                    "chat_only_mode": True,
                    "chat_only_success_turns": 0,
                    "failure_tracker": {
                        "tracked_tools": [],
                        "banned_tools": [],
                    },
                    "lock_acquired": False,
                    "last_accessed": 1234567890.0,
                    "trace": None,
                }
            ],
        }

        with patch("learning_agent.web_server._get_system", return_value=mock_system):
            result = await get_runtimes()

        assert result["active_runtime_count"] == 1
        assert result["runtimes"][0]["chat_only_mode"] is True


# ───────────────────────────────────────────────────────────────
# P1: lifespan 启动/关闭清理
# ───────────────────────────────────────────────────────────────

class TestLifespanClearsRuntimes:
    @pytest.mark.asyncio
    async def test_lifespan_startup_and_shutdown(self, mock_system):
        from learning_agent.web.web_server import lifespan
        from fastapi import FastAPI
        from learning_agent.learning_agent.config import Config
        from learning_agent.learning_agent.main import LearningAgentSystem

        app = FastAPI()
        mock_system.initialize = AsyncMock(return_value=None)
        mock_system.shutdown = AsyncMock(return_value=None)
        with patch("learning_agent.web_server._system", None), \
             patch("learning_agent.web_server.Config") as mock_config_cls, \
             patch("learning_agent.web_server.LearningAgentSystem", return_value=mock_system):
            async with lifespan(app):
                # 启动阶段已完成（进入 yield 后）
                mock_system.clear_all_runtimes.assert_called_once()
            # 关闭阶段再次调用
            assert mock_system.clear_all_runtimes.call_count == 2


class TestAgentLoopRuntimeOverview:
    def test_runtime_overview_public_api(self):
        session_manager = MagicMock()
        session_manager.list_sessions.return_value = [MagicMock(), MagicMock()]
        obs = MagicMock()
        trace = MagicMock()
        trace.trace_id = "trace-001"
        trace.spans = [MagicMock()]
        trace.duration_ms = 12
        span = MagicMock()
        span.name = "llm.stream"
        obs.get_trace_by_session.return_value = trace
        obs.get_spans_by_session.return_value = [span]

        agent_loop = AgentLoop(
            provider=MagicMock(),
            memory_manager=MagicMock(),
            session_manager=session_manager,
            hook_system=MagicMock(),
            event_bus=MagicMock(),
            tool_registry=MagicMock(),
            observability=obs,
        )

        runtime = AgentLoopSession(
            session_id="sess-003",
            agent_loop=agent_loop,
            resilience_config=ResilienceConfig(),
        )
        runtime.state = AgentState.IDLE
        runtime._chat_only_mode = False
        runtime._chat_only_success_turns = 2
        runtime._last_turn_count = 1
        runtime._failure_tracker.record_failure("tool.search", turn_count=1, reason="execution_error")

        agent_loop._session_runtimes = {"sess-003": runtime}
        agent_loop._session_last_accessed = {"sess-003": time.time()}

        result = agent_loop.get_runtime_overview()

        assert result["active_runtime_count"] == 1
        assert result["total_session_count"] == 2
        assert result["runtimes"][0]["session_id"] == "sess-003"
        assert result["runtimes"][0]["failure_tracker"]["tracked_tools"] == ["tool.search"]
        assert result["runtimes"][0]["trace"]["trace_id"] == "trace-001"

    def test_clear_runtime_returns_stable_summary(self):
        session_manager = MagicMock()
        session_manager.list_sessions.return_value = []
        obs = MagicMock()

        agent_loop = AgentLoop(
            provider=MagicMock(),
            memory_manager=MagicMock(),
            session_manager=session_manager,
            hook_system=MagicMock(),
            event_bus=MagicMock(),
            tool_registry=MagicMock(),
            observability=obs,
        )

        runtime = AgentLoopSession(
            session_id="sess-004",
            agent_loop=agent_loop,
            resilience_config=ResilienceConfig(),
        )
        runtime._chat_only_mode = True
        runtime._chat_only_success_turns = 1
        runtime._last_turn_count = 3
        runtime._failure_tracker.record_failure("tool.search", turn_count=3, reason="execution_error")

        agent_loop._session_runtimes = {"sess-004": runtime}
        agent_loop._session_last_accessed = {"sess-004": 1234567890.0}

        result = agent_loop.clear_runtime("sess-004")

        assert result["session_id"] == "sess-004"
        assert result["had_runtime"] is True
        assert result["cleared_runtime"]["chat_only_mode"] is True
        assert result["cleared_runtime"]["failure_tracker"]["tracked_tools"] == ["tool.search"]
        assert "sess-004" not in agent_loop._session_runtimes
