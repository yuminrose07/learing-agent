from __future__ import annotations

import asyncio
import re
import sys

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai import MessageRole
from learning_agent.ai.file_store import FileStore
from learning_agent.learning_agent.compaction import CompactionCoordinator
from learning_agent.learning_agent.mode_service import build_turn_profile
from learning_agent.learning_agent.session_events import SessionEventType
from learning_agent.learning_agent.session_manager import SessionManager


def _append_round(
    session_manager: SessionManager,
    session_id: str,
    *,
    user_text: str,
    assistant_text: str,
    repeat: int = 18,
) -> None:
    session_manager.append_message(session_id, MessageRole.USER, user_text)
    padded = f"{assistant_text}\n" + ("细节展开，用于拉长上下文并触发压缩。 " * repeat)
    session_manager.append_message(session_id, MessageRole.ASSISTANT, padded)


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


async def _long_task_summary_executor(prompt_text: str) -> str:
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
    filtered_pending = []
    for item in pending:
        pending_norm = _normalize_task_text(item)
        if any(pending_norm in done or done in pending_norm for done in completed_norm):
            continue
        if item not in filtered_pending:
            filtered_pending.append(item)

    section_2_lines = []
    for item in [*constraints, *decisions]:
        if item not in section_2_lines:
            section_2_lines.append(item)

    section_6_lines = []
    for item in [*goals, *constraints, *decisions, *corrections, *completed, *pending]:
        if item not in section_6_lines:
            section_6_lines.append(item)

    return (
        "<analysis>\n"
        "Coverage:\n- Long-task canonical summary composed from existing summary and new source transcript.\n\n"
        "Anchors:\n"
        f"- {user_anchor}\n\n"
        "Contradictions:\n"
        f"- {corrections[-1] if corrections else 'No explicit correction.'}\n\n"
        "Omitted:\n- Recent retained messages stay outside the summarized source range.\n\n"
        "Validation Notes:\n- Test executor keeps stable task markers to detect semantic drift.\n"
        "</analysis>\n"
        "<summary>\n"
        "1. Primary Learning Request and Intent:\n"
        f"{goals[-1] if goals else user_anchor}\n\n"
        "2. Learning Context and Goals:\n"
        f"{chr(10).join(section_2_lines) if section_2_lines else '保持当前长任务目标与约束。'}\n\n"
        "3. Key Concepts, Explanations, and Examples:\n"
        "- 当前测试关注长任务压缩后是否还能保留目标、约束、纠正与待办。\n"
        "- canonical summary 需要覆盖旧摘要与新增 source transcript，而不是只保留 delta。\n\n"
        "4. Materials, Files, and External Artifacts:\n"
        "- 无外部 artifact，本测试只验证会话语义保持。\n\n"
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


def _build_long_task_profile(session_mode):
    return build_turn_profile(session_mode).model_copy(
        update={"full_compact_threshold": 0.0001, "recent_token_budget": 80}
    )


def test_long_task_compaction_preserves_goal_constraints_and_latest_correction(tmp_path):
    session_manager = SessionManager(file_store=FileStore(str(tmp_path / "data")))
    session = session_manager.create_session()
    coordinator = CompactionCoordinator(
        session_manager,
        max_context_tokens=1400,
        summary_executor=_long_task_summary_executor,
    )
    profile = _build_long_task_profile(session.mode)

    _append_round(
        session_manager,
        session.id,
        user_text="目标: 将 payment webhook 迁移到 event schema v2。",
        assistant_text="先确认迁移边界和原始目标。",
    )
    _append_round(
        session_manager,
        session.id,
        user_text="约束: 不能修改公开 API 合约。",
        assistant_text="保持 API contract 不变，只允许内部适配。",
    )
    _append_round(
        session_manager,
        session.id,
        user_text="决定: 先只改 backend parser 和 replay pipeline。",
        assistant_text="把工作面先收口到 parser 与 replay。",
    )
    _append_round(
        session_manager,
        session.id,
        user_text="待办: webhook schema fixtures",
        assistant_text="这项待办稍后补齐。",
    )

    first_plan = asyncio.run(coordinator.evaluate_turn(session, "继续长任务", profile, allow_full_compact=True))
    assert first_plan.use_full_compact is True

    _append_round(
        session_manager,
        session.id,
        user_text="纠正: frontend checkout 绝对不能改，只能动 backend parser。",
        assistant_text="以这条纠正为新的最高优先级边界。",
    )
    _append_round(
        session_manager,
        session.id,
        user_text="完成: webhook schema fixtures",
        assistant_text="fixtures 已经补完，可以关闭这一项待办。",
    )
    _append_round(
        session_manager,
        session.id,
        user_text="待办: 校验 replay 对旧事件的兼容性",
        assistant_text="保留为当前阶段最新的未完成事项。",
    )
    _append_round(
        session_manager,
        session.id,
        user_text="继续推进: 先整理 replay fallback 的实现细节。",
        assistant_text="这轮只是把最新待办往前推，避免它仍停留在 retained 区。",
    )
    _append_round(
        session_manager,
        session.id,
        user_text="继续推进: 记录 schema v2 兼容点。",
        assistant_text="继续补充细节，确保上一轮待办进入新的 compact source。",
    )

    second_plan = asyncio.run(coordinator.evaluate_turn(session, "继续长任务", profile, allow_full_compact=True))

    assert second_plan.use_full_compact is True
    assert second_plan.full_compact_scope == "incremental"
    latest_summary = session_manager.load_compact_summary(session.id)
    assert latest_summary is not None
    assert "目标: 将 payment webhook 迁移到 event schema v2。" in latest_summary
    assert "约束: 不能修改公开 API 合约。" in latest_summary
    assert "纠正: frontend checkout 绝对不能改，只能动 backend parser。" in latest_summary
    assert "完成: webhook schema fixtures" in latest_summary
    assert "待办: 校验 replay 对旧事件的兼容性" in _summary_section(
        latest_summary,
        "7. Pending Learning Tasks",
    )
    assert "待办: webhook schema fixtures" not in _summary_section(
        latest_summary,
        "7. Pending Learning Tasks",
    )


def test_long_task_compaction_view_keeps_canonical_summary_and_recent_raw_messages(tmp_path):
    session_manager = SessionManager(file_store=FileStore(str(tmp_path / "data")))
    session = session_manager.create_session()
    coordinator = CompactionCoordinator(
        session_manager,
        max_context_tokens=1400,
        summary_executor=_long_task_summary_executor,
    )
    profile = _build_long_task_profile(session.mode)

    for index, user_text in enumerate(
        [
            "目标: 统一 webhook 回放链路。",
            "约束: 不能修改事件 JSONL 事实源。",
            "决定: 先补 replay 兼容层。",
            "待办: 写旧事件兼容测试。",
            "纠正: 不要碰 frontend，只能动 backend。",
        ]
    ):
        _append_round(
            session_manager,
            session.id,
            user_text=user_text,
            assistant_text=f"第 {index} 轮长任务上下文。",
        )

    plan = asyncio.run(coordinator.evaluate_turn(session, "继续长任务", profile, allow_full_compact=True))
    assert plan.use_full_compact is True

    session_manager.append_message(session.id, MessageRole.USER, "最新请求: 先补兼容性测试，不要现在改 runtime。")
    view = session_manager.build_llm_input_view(session.id, profile)
    system_blocks = [message.content for message in view.messages if message.role == MessageRole.SYSTEM]
    non_system_contents = [message.content for message in view.messages if message.role != MessageRole.SYSTEM]
    source_entries = [
        entry for entry in session_manager.get_message_history(session.id) if entry.id in plan.source_entry_ids
    ]

    assert any("目标: 统一 webhook 回放链路。" in block for block in system_blocks)
    assert "纠正: 不要碰 frontend，只能动 backend。" in non_system_contents
    assert "最新请求: 先补兼容性测试，不要现在改 runtime。" in non_system_contents
    assert all(entry.content not in non_system_contents for entry in source_entries if entry.content.strip())


def test_invalid_incremental_summary_does_not_replace_previous_long_task_summary(tmp_path):
    session_manager = SessionManager(file_store=FileStore(str(tmp_path / "data")))
    session = session_manager.create_session()
    profile = _build_long_task_profile(session.mode)

    good_coordinator = CompactionCoordinator(
        session_manager,
        max_context_tokens=1400,
        summary_executor=_long_task_summary_executor,
    )

    _append_round(
        session_manager,
        session.id,
        user_text="目标: 迁移长任务的 compaction 验证。",
        assistant_text="先建立一份可持续的 canonical summary。",
    )
    _append_round(
        session_manager,
        session.id,
        user_text="约束: 不能破坏 append-only JSONL。",
        assistant_text="事实源约束要一直保留。",
    )
    _append_round(
        session_manager,
        session.id,
        user_text="待办: 写回归测试",
        assistant_text="这项待办会在后续继续推进。",
    )

    first_plan = asyncio.run(good_coordinator.evaluate_turn(session, "继续长任务", profile, allow_full_compact=True))
    assert first_plan.use_full_compact is True
    first_summary = session_manager.load_compact_summary(session.id)
    assert first_summary is not None

    _append_round(
        session_manager,
        session.id,
        user_text="纠正: 回归测试必须覆盖语义偏移。",
        assistant_text="新增一条更严格的纠正要求。",
    )
    _append_round(
        session_manager,
        session.id,
        user_text="待办: 补增量压缩用例",
        assistant_text="准备触发第二次 compact。",
    )

    async def invalid_executor(prompt_text: str) -> str:
        del prompt_text
        return "<summary>只有一段坏摘要，没有九个 section。</summary>"

    bad_coordinator = CompactionCoordinator(
        session_manager,
        max_context_tokens=1400,
        summary_executor=invalid_executor,
    )

    second_plan = asyncio.run(bad_coordinator.evaluate_turn(session, "继续长任务", profile, allow_full_compact=True))
    latest_summary = session_manager.load_compact_summary(session.id)
    summary_events = [
        event
        for event in session_manager._file_store.read_session_events(session.id)
        if event["type"] == SessionEventType.COMPACTION_SUMMARY_ADDED
    ]

    assert second_plan.use_full_compact is False
    assert second_plan.summary_block is not None
    assert latest_summary == first_summary
    assert len(summary_events) == 1
