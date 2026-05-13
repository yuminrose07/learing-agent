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
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.agent.agent_loop import AgentLoop, AgentLoopSession, AgentState
from learning_agent.models import LearningSession, ResilienceConfig


@pytest.fixture
def mock_system():
    """构造一个最小可工作的 Mock LearningAgentSystem。"""
    system = MagicMock()

    # SessionManager
    system.session_manager = MagicMock()
    system.session_manager._sessions = {}

    # AgentLoop
    system.agent_loop = MagicMock(spec=AgentLoop)
    system.agent_loop._session_runtimes = {}
    system.agent_loop._session_last_accessed = {}
    system.agent_loop.clear_session_runtime = MagicMock()
    system.agent_loop.clear_all_runtimes = MagicMock()

    # FileStore / Observability
    system.file_store = MagicMock()
    system.observability = MagicMock()
    system.observability.data_dir = "/tmp/obs"

    return system


# ───────────────────────────────────────────────────────────────
# P0: 删除 Session 时清理运行时
# ───────────────────────────────────────────────────────────────

class TestDeleteSessionClearsRuntime:
    @pytest.mark.asyncio
    async def test_delete_existing_session(self, mock_system):
        from learning_agent.web_server import delete_session

        mock_system.session_manager._sessions["sess-001"] = MagicMock()
        mock_system._save_state = AsyncMock(return_value=None)
        with patch("learning_agent.web_server._get_system", return_value=mock_system):
            result = await delete_session("sess-001")

        assert result["status"] == "deleted"
        mock_system.agent_loop.clear_session_runtime.assert_called_once_with("sess-001")
        assert "sess-001" not in mock_system.session_manager._sessions

    @pytest.mark.asyncio
    async def test_delete_nonexistent_session(self, mock_system):
        from learning_agent.web_server import delete_session
        from fastapi import HTTPException

        with patch("learning_agent.web_server._get_system", return_value=mock_system):
            with pytest.raises(HTTPException) as exc_info:
                await delete_session("nonexist")
        assert exc_info.value.status_code == 404
        mock_system.agent_loop.clear_session_runtime.assert_not_called()


# ───────────────────────────────────────────────────────────────
# P2: 重置 Session 运行时
# ───────────────────────────────────────────────────────────────

class TestResetRuntimeEndpoint:
    @pytest.mark.asyncio
    async def test_reset_existing_session(self, mock_system):
        from learning_agent.web_server import reset_session_runtime

        mock_system.session_manager._sessions["sess-002"] = MagicMock()
        with patch("learning_agent.web_server._get_system", return_value=mock_system):
            result = await reset_session_runtime("sess-002")

        assert result["status"] == "runtime_reset"
        mock_system.agent_loop.clear_session_runtime.assert_called_once_with("sess-002")

    @pytest.mark.asyncio
    async def test_reset_nonexistent_session(self, mock_system):
        from learning_agent.web_server import reset_session_runtime
        from fastapi import HTTPException

        with patch("learning_agent.web_server._get_system", return_value=mock_system):
            with pytest.raises(HTTPException) as exc_info:
                await reset_session_runtime("nonexist")
        assert exc_info.value.status_code == 404
        mock_system.agent_loop.clear_session_runtime.assert_not_called()


# ───────────────────────────────────────────────────────────────
# P2: 观测运行时 API
# ───────────────────────────────────────────────────────────────

class TestObservabilityRuntimes:
    @pytest.mark.asyncio
    async def test_empty_runtimes(self, mock_system):
        from learning_agent.web_server import get_runtimes

        mock_system.agent_loop._session_runtimes = {}
        with patch("learning_agent.web_server._get_system", return_value=mock_system):
            result = await get_runtimes()

        assert result["active_runtime_count"] == 0
        assert result["runtimes"] == []

    @pytest.mark.asyncio
    async def test_with_active_runtimes(self, mock_system):
        from learning_agent.web_server import get_runtimes

        runtime = MagicMock(spec=AgentLoopSession)
        runtime.state = AgentState.IDLE
        runtime._chat_only_mode = False
        runtime._chat_only_success_turns = 2
        ft = MagicMock()
        ft._counts = {}
        ft.is_banned.return_value = False
        runtime._failure_tracker = ft

        mock_system.agent_loop._session_runtimes = {"sess-003": runtime}
        mock_system.agent_loop._session_last_accessed = {"sess-003": 1234567890.0}

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
        from learning_agent.web_server import get_runtimes

        runtime = MagicMock(spec=AgentLoopSession)
        runtime.state = AgentState.IDLE
        runtime._chat_only_mode = True
        runtime._chat_only_success_turns = 0
        ft = MagicMock()
        ft._counts = {}
        ft.is_banned.return_value = False
        runtime._failure_tracker = ft

        mock_system.agent_loop._session_runtimes = {"sess-004": runtime}
        mock_system.agent_loop._session_last_accessed = {"sess-004": 1234567890.0}

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
        from learning_agent.web_server import lifespan
        from fastapi import FastAPI
        from learning_agent.config import Config
        from learning_agent.main import LearningAgentSystem

        app = FastAPI()
        mock_system.initialize = AsyncMock(return_value=None)
        mock_system.shutdown = AsyncMock(return_value=None)
        mock_system._save_state = AsyncMock(return_value=None)
        with patch("learning_agent.web_server._system", None), \
             patch("learning_agent.web_server.Config") as mock_config_cls, \
             patch("learning_agent.web_server.LearningAgentSystem", return_value=mock_system):
            async with lifespan(app):
                # 启动阶段已完成（进入 yield 后）
                mock_system.agent_loop.clear_all_runtimes.assert_called_once()
            # 关闭阶段再次调用
            assert mock_system.agent_loop.clear_all_runtimes.call_count == 2
