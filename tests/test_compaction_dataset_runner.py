from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterable

import pytest

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.agent.agent_loop import AgentLoop
from learning_agent.agent.event_bus import EventBus
from learning_agent.agent.hook_system import HookSystem
from learning_agent.ai import ChatChunk, ChatParams, MessageRole, ResilienceConfig
from learning_agent.ai.base_provider import BaseProvider
from learning_agent.ai.file_store import FileStore
from learning_agent.learning_agent.compaction import CompactionCoordinator
from learning_agent.learning_agent.mode_service import build_turn_profile
from learning_agent.learning_agent.session_events import SessionEventType
from learning_agent.learning_agent.session_manager import SessionManager
from learning_agent.learning_agent.tool_registry import ToolRegistry
from learning_agent.memory.memory_manager import MemoryManager


FIXTURE_DIR = Path("/Users/roseannk/my-agent/tests/fixtures/compaction_long_task_cases")
ARTIFACT_ROOT = Path("/Users/roseannk/my-agent/.test_artifacts/compaction_dataset_runs/latest")


class ScriptedProvider(BaseProvider):
    _default_model = "gpt-4o"

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self._cursor = 0

    @property
    def default_model(self) -> str:
        return self._default_model

    async def stream_chat(self, params: ChatParams) -> AsyncIterable[ChatChunk]:
        del params
        if self._cursor >= len(self._responses):
            raise AssertionError("scripted provider ran out of responses")
        response = self._responses[self._cursor]
        self._cursor += 1
        yield ChatChunk(content=response, finish_reason="stop")

    async def chat(self, params: ChatParams) -> ChatChunk:
        del params
        return ChatChunk(content="")

    def supports_tool_calling(self) -> bool:
        return True

    def supports_vision(self) -> bool:
        return False

    def get_max_context_length(self) -> int:
        return 128000


@dataclass
class DatasetRunResult:
    case_name: str
    responses: list[str]
    latest_summary: str | None
    summary_events: list[dict[str, Any]]
    system_blocks: list[str]
    non_system_contents: list[str]
    pending_section: str
    artifact_dir: str
    session_id: str
    turn_reports: list[dict[str, Any]]


def _extract_tag(text: str, tag: str) -> str:
    match = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, flags=re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _summary_section(summary_text: str, title: str) -> str:
    pattern = rf"(?ms)^{re.escape(title)}:\n(.*?)(?=^\d+\.\s.+?:|\Z)"
    match = re.search(pattern, summary_text.strip())
    return match.group(1).strip() if match else ""


def _normalize_task_text(value: str) -> str:
    return (
        value.replace("待办:", "")
        .replace("完成:", "")
        .replace("已", "")
        .replace("。", "")
        .replace("：", ":")
        .strip()
    )


async def _dataset_summary_executor(prompt_text: str) -> str:
    existing_summary = _extract_tag(prompt_text, "existing_summary")
    source_transcript = _extract_tag(prompt_text, "source_transcript")
    corpus = "\n".join(part for part in (existing_summary, source_transcript) if part).strip()

    goals = re.findall(r"(目标[:：][^\n]+)", corpus)
    constraints = re.findall(r"(约束[:：][^\n]+)", corpus)
    decisions = re.findall(r"(决定[:：][^\n]+)", corpus)
    corrections = re.findall(r"(纠正[:：][^\n]+)", corpus)
    completed = re.findall(r"(完成[:：][^\n]+)", corpus)
    pending = re.findall(r"(待办[:：][^\n]+)", corpus)
    user_lines = re.findall(r"USER:\s*(.+)", source_transcript)
    user_anchor = user_lines[-1].strip() if user_lines else (goals[-1] if goals else "继续当前长任务。")

    completed_norm = [_normalize_task_text(item) for item in completed]
    filtered_pending: list[str] = []
    for item in pending:
        pending_norm = _normalize_task_text(item)
        if any(pending_norm in done or done in pending_norm for done in completed_norm):
            continue
        if item not in filtered_pending:
            filtered_pending.append(item)

    section_2_lines: list[str] = []
    for item in [*constraints, *decisions]:
        if item not in section_2_lines:
            section_2_lines.append(item)

    section_6_lines: list[str] = []
    for item in [*goals, *constraints, *decisions, *corrections, *completed, *pending]:
        if item not in section_6_lines:
            section_6_lines.append(item)

    return (
        "<analysis>\n"
        "Coverage:\n- Dataset replay summary built from existing summary and selected source transcript.\n\n"
        "Anchors:\n"
        f"- {user_anchor}\n\n"
        "Contradictions:\n"
        f"- {corrections[-1] if corrections else 'No explicit correction.'}\n\n"
        "Omitted:\n- Recent retained messages remain outside the compact source.\n\n"
        "Validation Notes:\n- Dataset evaluator preserves goals, constraints, corrections, completed work and pending tasks.\n"
        "</analysis>\n"
        "<summary>\n"
        "1. Primary Learning Request and Intent:\n"
        f"{goals[-1] if goals else user_anchor}\n\n"
        "2. Learning Context and Goals:\n"
        f"{chr(10).join(section_2_lines) if section_2_lines else '保持当前长任务目标与约束。'}\n\n"
        "3. Key Concepts, Explanations, and Examples:\n"
        "- 该数据集通过真实对话脚本验证长任务压缩后的语义保持能力。\n"
        "- canonical summary 必须覆盖旧摘要与新增 source transcript，而不是只保留 delta。\n\n"
        "4. Materials, Files, and External Artifacts:\n"
        "- 无外部 artifact，本测试只验证会话事件流和压缩语义。\n\n"
        "5. Errors, Misunderstandings, and Corrections:\n"
        f"{corrections[-1] if corrections else '无新的纠正。'}\n\n"
        "6. All User Messages and Feedback:\n"
        f"{chr(10).join(f'- {item}' for item in section_6_lines) if section_6_lines else f'- {user_anchor}'}\n\n"
        "7. Pending Learning Tasks:\n"
        f"{chr(10).join(f'- {item}' for item in filtered_pending) if filtered_pending else '- 暂无未完成待办。'}\n\n"
        "8. Work Completed in Summarized Portion:\n"
        f"{chr(10).join(f'- {item}' for item in completed) if completed else '- 暂无已完成项。'}\n\n"
        "9. Context for Continuing Recent Messages:\n"
        f"继续时以后续 retained messages 和最新用户请求为准。关键原文锚点：“{user_anchor}”\n"
        "</summary>"
    )


def _make_agent_loop(provider: BaseProvider, session_manager: SessionManager) -> AgentLoop:
    from learning_agent.learning_agent.tool_execution_service import ToolExecutionServiceImpl

    event_bus = EventBus()
    hook_system = HookSystem()
    tool_registry = ToolRegistry()
    tool_execution_service = ToolExecutionServiceImpl(tool_registry)
    return AgentLoop(
        provider=provider,
        memory_service=MemoryManager(),
        session_store=session_manager,
        hook_system=hook_system,
        event_bus=event_bus,
        tool_execution_service=tool_execution_service,
        observability=None,
        max_react_turns=5,
        resilience_config=ResilienceConfig(),
    )


def _pad_response(text: str, repeat: int) -> str:
    return f"{text}\n" + ("细节展开，用于拉长上下文并触发压缩。 " * repeat)


async def _run_case(tmp_path: Path, case: dict[str, Any]) -> DatasetRunResult:
    del tmp_path
    data_dir = ARTIFACT_ROOT / case["name"]
    if data_dir.exists():
        shutil.rmtree(data_dir)
    file_store = FileStore(str(data_dir))
    session_manager = SessionManager(file_store=file_store)
    provider = ScriptedProvider(
        [
            _pad_response(turn["assistant"], turn.get("assistant_repeat", case["compaction"].get("assistant_repeat", 18)))
            for turn in case["turns"]
        ]
    )
    agent_loop = _make_agent_loop(provider, session_manager)
    session = session_manager.create_session(title=case["name"])
    profile = build_turn_profile(session.mode).model_copy(
        update={
            "full_compact_threshold": case["compaction"]["full_compact_threshold"],
            "recent_token_budget": case["compaction"]["recent_token_budget"],
        }
    )
    coordinator = CompactionCoordinator(
        session_manager,
        max_context_tokens=case["compaction"]["max_context_tokens"],
        summary_executor=_dataset_summary_executor,
    )

    responses: list[str] = []
    turn_reports: list[dict[str, Any]] = []
    for turn in case["turns"]:
        plan = await coordinator.evaluate_turn(session, turn["user"], profile, allow_full_compact=True)
        response_parts: list[str] = []
        async for chunk in agent_loop.run(session, turn["user"], profile, compaction_plan=plan):
            if chunk.content:
                response_parts.append(chunk.content)
        response_text = "".join(response_parts)
        responses.append(response_text)
        turn_summary = session_manager.load_compact_summary(session.id)
        turn_events = file_store.read_session_events(session.id)
        turn_summary_events = [
            event for event in turn_events if event["type"] == SessionEventType.COMPACTION_SUMMARY_ADDED
        ]
        turn_view = session_manager.build_llm_input_view(session.id, profile)
        turn_reports.append(
            {
                "user": turn["user"],
                "assistant": response_text,
                "compaction_plan": {
                    "use_full_compact": plan.use_full_compact,
                    "compact_scope": plan.full_compact_scope,
                    "summary_block_present": bool(plan.summary_block),
                    "source_entry_ids": list(plan.source_entry_ids),
                    "retained_entry_ids": list(plan.retained_entry_ids),
                },
                "summary_event_count": len(turn_summary_events),
                "latest_summary": turn_summary,
                "view": {
                    "system_blocks": [
                        message.content
                        for message in turn_view.messages
                        if message.role == MessageRole.SYSTEM
                    ],
                    "non_system_contents": [
                        message.content
                        for message in turn_view.messages
                        if message.role != MessageRole.SYSTEM
                    ],
                },
            }
        )

    latest_summary = session_manager.load_compact_summary(session.id)
    events = file_store.read_session_events(session.id)
    summary_events = [event for event in events if event["type"] == SessionEventType.COMPACTION_SUMMARY_ADDED]
    view = session_manager.build_llm_input_view(session.id, profile)
    system_blocks = [message.content for message in view.messages if message.role == MessageRole.SYSTEM]
    non_system_contents = [message.content for message in view.messages if message.role != MessageRole.SYSTEM]
    pending_section = _summary_section(latest_summary or "", "7. Pending Learning Tasks")
    report = {
        "case_name": case["name"],
        "description": case.get("description", ""),
        "session_id": session.id,
        "event_log_path": str(data_dir / "sessions" / f"{session.id}.events.jsonl"),
        "summary_event_count": len(summary_events),
        "latest_summary": latest_summary,
        "pending_section": pending_section,
        "turn_reports": turn_reports,
    }
    (data_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return DatasetRunResult(
        case_name=case["name"],
        responses=responses,
        latest_summary=latest_summary,
        summary_events=summary_events,
        system_blocks=system_blocks,
        non_system_contents=non_system_contents,
        pending_section=pending_section,
        artifact_dir=str(data_dir),
        session_id=session.id,
        turn_reports=turn_reports,
    )


def _assert_expectations(case: dict[str, Any], result: DatasetRunResult) -> None:
    expectations = case["expectations"]
    joined_system = "\n".join(result.system_blocks)
    joined_non_system = "\n".join(result.non_system_contents)
    latest_summary = result.latest_summary or ""
    artifact_dir = Path(result.artifact_dir)
    report_path = artifact_dir / "report.json"
    events_path = artifact_dir / "sessions" / f"{result.session_id}.events.jsonl"

    assert artifact_dir.exists()
    assert report_path.exists()
    assert events_path.exists()
    assert len(result.summary_events) >= expectations.get("min_summary_events", 0)
    if expectations.get("event_scopes_prefix"):
        actual_scopes = [event["payload"]["scope"] for event in result.summary_events]
        assert actual_scopes[: len(expectations["event_scopes_prefix"])] == expectations["event_scopes_prefix"]

    for text in expectations.get("summary_contains", []):
        assert text in latest_summary
    for text in expectations.get("summary_pending_contains", []):
        assert text in result.pending_section
    for text in expectations.get("summary_pending_excludes", []):
        assert text not in result.pending_section
    for text in expectations.get("view_system_contains", []):
        assert text in joined_system
    for text in expectations.get("view_recent_contains", []):
        assert text in joined_non_system
    for text in expectations.get("view_recent_excludes", []):
        assert text not in joined_non_system
    for index, expected in enumerate(expectations.get("response_contains", [])):
        assert expected in result.responses[index]


def _load_case(case_path: Path) -> dict[str, Any]:
    return json.loads(case_path.read_text(encoding="utf-8"))


CASE_PATHS = sorted(FIXTURE_DIR.glob("*.json"))


@pytest.mark.parametrize("case_path", CASE_PATHS, ids=lambda path: path.stem)
def test_compaction_dataset_replay(case_path: Path, tmp_path: Path):
    case = _load_case(case_path)
    result = asyncio.run(_run_case(tmp_path, case))
    _assert_expectations(case, result)
