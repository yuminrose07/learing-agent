"""
大文件处理相关测试。
"""

from __future__ import annotations

import shlex
import sys
from typing import AsyncIterable

import pytest

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.agent.agent_loop import AgentLoop
from learning_agent.agent.event_bus import EventBus
from learning_agent.agent.hook_system import HookSystem
from learning_agent.ai import ChatChunk, ChatParams, ContextLengthError, MessageRole, ResilienceConfig
from learning_agent.ai.base_provider import BaseProvider
from learning_agent.learning_agent.extensions.code_tools import _tool_bash, _tool_read_file
from learning_agent.learning_agent.extensions.grep_tools import _tool_grep
from learning_agent.learning_agent.session_manager import SessionManager


class MinimalToolExecutionService:
    async def execute_tool_call(self, tool_call, timeout=None):
        raise NotImplementedError("Test stub")

    def get_tool_definition(self, tool_id):
        return None

    def list_tools(self):
        return []


class OverflowThenRecoverProvider(BaseProvider):
    _default_model = "gpt-4o"

    def __init__(self):
        self.calls = 0

    @property
    def default_model(self) -> str:
        return self._default_model

    async def stream_chat(self, params: ChatParams) -> AsyncIterable[ChatChunk]:
        self.calls += 1
        if self.calls == 1:
            raise ContextLengthError("context too large")
        yield ChatChunk(content="recovered")

    async def chat(self, params: ChatParams) -> ChatChunk:
        return ChatChunk(content="recovered")

    def supports_tool_calling(self) -> bool:
        return True

    def supports_vision(self) -> bool:
        return False

    def get_max_context_length(self) -> int:
        return 128000


@pytest.mark.asyncio
async def test_read_file_truncates_large_file_and_returns_next_offset(tmp_path, monkeypatch):
    file_path = tmp_path / "big.txt"
    file_path.write_text("".join(f"line {i}\n" for i in range(1, 601)), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = await _tool_read_file("big.txt")

    assert result["truncated"] is True
    assert result["truncated_by"] == "lines"
    assert result["shown_lines"] == 500
    assert result["next_offset"] == 501
    assert "[Showing lines 1-500 of 600. Use offset=501 to continue.]" in result["content"]


@pytest.mark.asyncio
async def test_read_file_with_user_limit_reports_remaining_lines(tmp_path, monkeypatch):
    file_path = tmp_path / "notes.txt"
    file_path.write_text("".join(f"note {i}\n" for i in range(1, 21)), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = await _tool_read_file("notes.txt", offset=11, limit=5)

    assert result["truncated"] is False
    assert result["shown_lines"] == 5
    assert result["next_offset"] == 16
    assert "[5 more lines. Use offset=16 to continue.]" in result["content"]


@pytest.mark.asyncio
async def test_grep_returns_line_numbers_and_limit_notice(tmp_path, monkeypatch):
    file_path = tmp_path / "materials.md"
    file_path.write_text(
        "\n".join(
            [
                "chapter 1",
                "overview",
                "entropy introduction",
                "details",
                "entropy advanced",
                "summary",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = await _tool_grep(
        "entropy",
        path=".",
        glob="*.md",
        literal=True,
        context=1,
        limit=1,
    )

    assert result["matches"] == 1
    assert result["limit_reached"] is True
    assert "materials.md:2- overview" in result["output"]
    assert "materials.md:3: entropy introduction" in result["output"]
    assert "materials.md:4- details" in result["output"]
    assert "1 matches limit reached." in result["output"]


@pytest.mark.asyncio
async def test_bash_truncates_tail_output():
    command = f"{shlex.quote(sys.executable)} -c \"for i in range(600): print(i)\""

    result = await _tool_bash(command)

    assert result["returncode"] == 0
    assert result["truncated"] is True
    assert result["truncated_by"] == "lines"
    assert "599" in result["stdout"]
    assert "[Showing lines 101-600 of 600. Use file operations to inspect full output.]" in result["stdout"]


@pytest.mark.asyncio
async def test_agent_loop_emergency_truncates_tool_message_and_retries():
    provider = OverflowThenRecoverProvider()
    session_manager = SessionManager()
    event_bus = EventBus()
    loop = AgentLoop(
        provider=provider,
        memory_service=None,
        session_store=session_manager,
        hook_system=HookSystem(),
        event_bus=event_bus,
        tool_execution_service=MinimalToolExecutionService(),
        observability=None,
        resilience_config=ResilienceConfig(turn_retry_max_attempts=1),
    )
    session = session_manager.create_session()
    entry = session_manager.append_message(
        session.id,
        MessageRole.TOOL,
        "x" * 5000,
        metadata={"tool_id": "read_file", "tool_call_id": "call-test"},
    )
    runtime = loop._get_or_create_runtime(session.id)

    chunks = [
        chunk
        async for chunk in runtime._stream_chat_with_retry(
            session,
            ChatParams(model="gpt-4o", messages=[]),
        )
    ]

    assert provider.calls == 2
    assert [chunk.content for chunk in chunks] == ["recovered"]
    assert entry is not None
    assert entry.metadata["emergency_truncated"] is True
    assert len(entry.content) <= 1000
    assert entry.content.endswith("[Content truncated due to context limit]")
    history = event_bus.get_history("agent.contextEmergencyTruncation")
    assert len(history) == 1
