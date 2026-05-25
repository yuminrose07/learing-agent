"""L2 可复用断言模块 —— 读 L1 events.jsonl 的测试共用 helpers.

设计目标（详见 docs/design/design-observability-l1-l4-architecture.md §3.5）：
- 纯函数，输入只接 `list[SessionEvent]`，不带 IO；测试自己负责加载
- 失败消息附上「候选事件摘要」，方便复制给 AI 排查
- 与 `tests/render_session_timeline.py` 共享同一套 visibility / type 语义
- 这些断言不属于 production 代码，只在 tests/ 使用

四个核心 helper：
- `assert_event_present` —— 在 events 里找第一条满足条件的事件
- `assert_causal_chain_resolvable` —— parent_event_id 必须在同 session 内可解析
- `assert_tool_exec_paired` —— 每个 tool.exec_started 都配 completed/failed（同 call_id）
- `assert_visibility_isolation` —— observability 事件不应泄漏到 projection messages
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from learning_agent.learning_agent.session_events import (
    EventVisibility,
    SessionEvent,
    SessionEventType,
)

__all__ = [
    "assert_event_present",
    "assert_causal_chain_resolvable",
    "assert_tool_exec_paired",
    "assert_visibility_isolation",
]


_CANDIDATE_PREVIEW_LIMIT = 5
_PAYLOAD_PREVIEW_CHARS = 120


def _payload_preview(payload: dict[str, Any]) -> str:
    """把 payload 压成单行短串，仅用于失败消息。"""
    if not payload:
        return "{}"
    try:
        items = ", ".join(f"{k}={payload[k]!r}" for k in list(payload)[:4])
    except Exception:
        items = str(payload)
    if len(items) > _PAYLOAD_PREVIEW_CHARS:
        items = items[: _PAYLOAD_PREVIEW_CHARS - 3] + "..."
    return f"{{{items}}}"


def _event_one_liner(evt: SessionEvent) -> str:
    return (
        f"seq={evt.seq} type={evt.type!r} visibility={evt.visibility!r} "
        f"event_id={evt.event_id!r} parent={evt.parent_event_id!r} "
        f"payload={_payload_preview(evt.payload)}"
    )


def _format_candidates(events: Sequence[SessionEvent], hint: str = "") -> str:
    if not events:
        return "  (no events in input)"
    head = events[:_CANDIDATE_PREVIEW_LIMIT]
    lines = [f"  - {_event_one_liner(e)}" for e in head]
    if len(events) > _CANDIDATE_PREVIEW_LIMIT:
        lines.append(f"  ... and {len(events) - _CANDIDATE_PREVIEW_LIMIT} more")
    return ("\n".join([hint, *lines]) if hint else "\n".join(lines))


def _payload_matches(payload: dict[str, Any], expected: dict[str, Any]) -> bool:
    for k, v in expected.items():
        if k not in payload:
            return False
        if payload[k] != v:
            return False
    return True


def assert_event_present(
    events: Sequence[SessionEvent],
    *,
    type: str,
    payload_contains: dict[str, Any] | None = None,
    visibility: str | None = None,
    parent_event_id: str | None = None,
) -> SessionEvent:
    """在 events 里找第一条满足条件的事件。

    匹配语义：
    - `type` 必须严格等
    - `payload_contains` 是子集匹配（候选事件 payload 必须包含所有指定 k=v）
    - `visibility` 必须严格等（None 表示不约束）
    - `parent_event_id` 必须严格等（None 表示不约束；如果想断言"无 parent"，传空串 ""）

    失败时附上「同 type 的候选事件摘要」，便于 AI 排查。
    """
    same_type = [e for e in events if e.type == type]
    matches: list[SessionEvent] = []
    for evt in same_type:
        if visibility is not None and evt.visibility != visibility:
            continue
        if parent_event_id is not None:
            if parent_event_id == "":
                if evt.parent_event_id not in (None, ""):
                    continue
            elif evt.parent_event_id != parent_event_id:
                continue
        if payload_contains is not None and not _payload_matches(evt.payload, payload_contains):
            continue
        matches.append(evt)

    if matches:
        return matches[0]

    criteria_parts = [f"type={type!r}"]
    if visibility is not None:
        criteria_parts.append(f"visibility={visibility!r}")
    if parent_event_id is not None:
        criteria_parts.append(f"parent_event_id={parent_event_id!r}")
    if payload_contains:
        criteria_parts.append(f"payload_contains={payload_contains!r}")
    criteria = ", ".join(criteria_parts)

    if same_type:
        candidates_hint = f"  closest candidates (same type, {len(same_type)} total):"
    else:
        candidates_hint = f"  no events with type={type!r}; sample of available events:"
        same_type = list(events)

    raise AssertionError(
        f"assert_event_present: no event matches {criteria}\n"
        f"{_format_candidates(same_type, candidates_hint)}"
    )


def assert_causal_chain_resolvable(events: Sequence[SessionEvent]) -> None:
    """对每条带 parent_event_id 的事件，校验 parent 在同 session 内可解析。

    具体规则：
    - parent event 必须存在（event_id 出现在 events 中）
    - parent event 必须与 child 同 session_id
    - parent event 的 seq 必须小于 child（先后顺序）

    失败时附上 orphan 列表 + 最接近的几个候选 parent。
    """
    by_event_id: dict[str, SessionEvent] = {e.event_id: e for e in events}
    orphans: list[tuple[SessionEvent, str]] = []

    for evt in events:
        pid = evt.parent_event_id
        if not pid:
            continue
        parent = by_event_id.get(pid)
        if parent is None:
            orphans.append((evt, f"parent_event_id={pid!r} not found in events"))
            continue
        if parent.session_id != evt.session_id:
            orphans.append((
                evt,
                f"parent session_id={parent.session_id!r} != child session_id={evt.session_id!r}",
            ))
            continue
        if parent.seq >= evt.seq:
            orphans.append((
                evt,
                f"parent seq={parent.seq} >= child seq={evt.seq} (must precede)",
            ))

    if not orphans:
        return

    lines = ["assert_causal_chain_resolvable: causal chain broken for the following events:"]
    for evt, reason in orphans[:_CANDIDATE_PREVIEW_LIMIT]:
        lines.append(f"  - {_event_one_liner(evt)}")
        lines.append(f"    reason: {reason}")
    if len(orphans) > _CANDIDATE_PREVIEW_LIMIT:
        lines.append(f"  ... and {len(orphans) - _CANDIDATE_PREVIEW_LIMIT} more orphans")
    raise AssertionError("\n".join(lines))


def assert_tool_exec_paired(events: Sequence[SessionEvent]) -> None:
    """每个 tool.exec_started 必须跟着一个 tool.exec_completed 或 tool.exec_failed（同 call_id）。

    配对规则：
    - 用 payload.call_id 作配对键（ToolExecutor 写入约定）
    - completed/failed 任一即可，但只允许一个对端（不允许 started → completed → failed 三联）
    - failed/completed 不能没有 started 前缀

    失败时列出不平衡的 call_id 与对应事件。
    """
    started_by_call_id: dict[str, SessionEvent] = {}
    closed_by_call_id: dict[str, SessionEvent] = {}
    duplicate_close: list[tuple[str, SessionEvent, SessionEvent]] = []
    orphan_close: list[SessionEvent] = []

    close_types = {SessionEventType.TOOL_EXEC_COMPLETED, SessionEventType.TOOL_EXEC_FAILED}

    for evt in events:
        if evt.type == SessionEventType.TOOL_EXEC_STARTED:
            call_id = evt.payload.get("call_id")
            if not call_id:
                # call_id 是 ToolExecutor 写入约定，缺失即护栏失败
                raise AssertionError(
                    f"assert_tool_exec_paired: tool.exec_started missing payload.call_id\n"
                    f"  - {_event_one_liner(evt)}"
                )
            if call_id in started_by_call_id:
                raise AssertionError(
                    f"assert_tool_exec_paired: duplicate tool.exec_started for call_id={call_id!r}\n"
                    f"  - {_event_one_liner(started_by_call_id[call_id])}\n"
                    f"  - {_event_one_liner(evt)}"
                )
            started_by_call_id[call_id] = evt
        elif evt.type in close_types:
            call_id = evt.payload.get("call_id")
            if not call_id:
                orphan_close.append(evt)
                continue
            if call_id not in started_by_call_id:
                orphan_close.append(evt)
                continue
            if call_id in closed_by_call_id:
                duplicate_close.append((call_id, closed_by_call_id[call_id], evt))
                continue
            closed_by_call_id[call_id] = evt

    unmatched_started = [
        s for cid, s in started_by_call_id.items() if cid not in closed_by_call_id
    ]

    if not (unmatched_started or orphan_close or duplicate_close):
        return

    lines = ["assert_tool_exec_paired: tool.exec_* pairing violated:"]

    if unmatched_started:
        lines.append(
            f"  unmatched tool.exec_started (no completed/failed for call_id, "
            f"{len(unmatched_started)} total):"
        )
        for s in unmatched_started[:_CANDIDATE_PREVIEW_LIMIT]:
            lines.append(f"    - {_event_one_liner(s)}")

    if orphan_close:
        lines.append(
            f"  orphan close events (completed/failed without preceding started, "
            f"{len(orphan_close)} total):"
        )
        for c in orphan_close[:_CANDIDATE_PREVIEW_LIMIT]:
            lines.append(f"    - {_event_one_liner(c)}")

    if duplicate_close:
        lines.append(f"  duplicate close for same call_id ({len(duplicate_close)} total):")
        for cid, first, second in duplicate_close[:_CANDIDATE_PREVIEW_LIMIT]:
            lines.append(f"    call_id={cid!r}")
            lines.append(f"      first : {_event_one_liner(first)}")
            lines.append(f"      second: {_event_one_liner(second)}")

    raise AssertionError("\n".join(lines))


def assert_visibility_isolation(
    events: Sequence[SessionEvent],
    projected_messages: Iterable[Any],
) -> None:
    """observability 事件不应出现在 replay 重建的 messages 里。

    分层校验：
    1. **强校验**：若 message 暴露了 `event_id` / `source_event_id` / `metadata.event_id`
       字段，且其值落在 observability 事件集合内 → 直接失败。
    2. **弱校验（防御性）**：若 message 的 `content` 字符串包含某 observability 事件
       的 type 名（如 "tool.exec_started"）→ 失败，因为业务消息不应承载诊断事件类型名。

    两层并行：强校验能精确定位违规，弱校验抓"复制粘贴式"泄漏。
    """
    obs_event_ids = {
        e.event_id for e in events if e.visibility == EventVisibility.OBSERVABILITY
    }
    obs_event_types = {
        e.type for e in events if e.visibility == EventVisibility.OBSERVABILITY
    }
    if not obs_event_ids:
        # 输入没有 observability 事件，无需校验
        return

    leaks: list[str] = []

    for idx, msg in enumerate(projected_messages):
        # 强校验：直查事件 id 字段
        for attr in ("event_id", "source_event_id"):
            val = getattr(msg, attr, None)
            if val and val in obs_event_ids:
                leaks.append(
                    f"  message[{idx}].{attr}={val!r} maps to an observability event"
                )

        meta = getattr(msg, "metadata", None)
        if isinstance(meta, dict):
            for key in ("event_id", "source_event_id"):
                val = meta.get(key)
                if val and val in obs_event_ids:
                    leaks.append(
                        f"  message[{idx}].metadata.{key}={val!r} maps to an observability event"
                    )

        # 弱校验：content 子串扫描
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            for t in obs_event_types:
                if t in content:
                    snippet = content[:80].replace("\n", " ")
                    leaks.append(
                        f"  message[{idx}].content contains observability type {t!r}: {snippet!r}"
                    )

    if not leaks:
        return

    lines = [
        "assert_visibility_isolation: observability event(s) leaked into projected messages:",
        *leaks[:_CANDIDATE_PREVIEW_LIMIT * 2],
    ]
    if len(leaks) > _CANDIDATE_PREVIEW_LIMIT * 2:
        lines.append(f"  ... and {len(leaks) - _CANDIDATE_PREVIEW_LIMIT * 2} more leaks")
    raise AssertionError("\n".join(lines))
