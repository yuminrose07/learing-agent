"""Tests for the runner's tool-call aggregator (_collect_tool_calls_from_events).

回填 cases/*/response.json 的 tool_calls 关键路径:
- /chat 响应里没有 tool_calls,真实工具调用只在 events 流里(tool.exec_started + tool.exec_completed)
- 多 case 单 session 时,按 case 自报的 start_ts/end_ts 时间窗切分
- result 字段是被截断的 JSON 字符串,需要降级到 raw_head
"""
from __future__ import annotations

import json

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tests" / "e2e"))
# 直接 import 模块路径
import importlib.util
spec = importlib.util.spec_from_file_location(
    "frontend_real_runner_web_search",
    str(Path(__file__).parent / "e2e" / "frontend_real_runner_web_search.py"),
)
runner_mod = importlib.util.module_from_spec(spec)
# Python 3.14 的 @dataclass 会通过 sys.modules[cls.__module__] 反查,
# 所以必须把动态加载的模块塞回 sys.modules 再 exec
sys.modules[spec.name] = runner_mod
spec.loader.exec_module(runner_mod)

_collect_tool_calls_from_events = runner_mod._collect_tool_calls_from_events


def _make_started(ts: str, call_id: str, tool_name: str, args: dict) -> dict:
    return {
        "seq": 1,
        "ts": ts,
        "type": "tool.exec_started",
        "payload": {
            "tool_name": tool_name,
            "call_id": call_id,
            "args": args,
            "attempt": 0,
            "timeout": 60,
        },
    }


def _make_completed(ts: str, call_id: str, tool_name: str, result: dict | str, *, truncated: bool = False) -> dict:
    result_str = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return {
        "seq": 2,
        "ts": ts,
        "type": "tool.exec_completed",
        "payload": {
            "tool_name": tool_name,
            "call_id": call_id,
            "attempt": 0,
            "latency_ms": 12.3,
            "result": result_str,
            "result_size": len(result_str),
            "result_truncated": truncated,
        },
    }


def test_collect_returns_empty_when_start_ts_none():
    assert _collect_tool_calls_from_events([], None, None) == []


def test_collect_picks_up_started_completed_pair():
    events = [
        _make_started("2026-05-28T10:00:01Z", "c1", "web_fetch", {"url": "https://x.com", "query": "task group"}),
        _make_completed(
            "2026-05-28T10:00:02Z", "c1", "web_fetch",
            {"pagination_mode": "query", "content": "## Tasks\n\nTask group..." , "selected_sections": ["s2", "s3"], "url": "https://x.com", "title": "X"},
        ),
    ]
    result = _collect_tool_calls_from_events(events, "2026-05-28T10:00:00Z", "2026-05-28T10:00:05Z")
    assert len(result) == 1
    call = result[0]
    assert call["call_id"] == "c1"
    assert call["tool_name"] == "web_fetch"
    assert call["args"] == {"url": "https://x.com", "query": "task group"}
    assert call["started_at"] == "2026-05-28T10:00:01Z"
    assert call["completed_at"] == "2026-05-28T10:00:02Z"
    assert call["latency_ms"] == 12.3
    # result_summary 应解出关键字段
    summary = call["result_summary"]
    assert summary["pagination_mode"] == "query"
    assert summary["selected_sections"] == ["s2", "s3"]
    assert summary["url"] == "https://x.com"
    assert summary["content_chars"] == len("## Tasks\n\nTask group...")
    assert summary["content_head"].startswith("## Tasks")


def test_collect_filters_by_time_window():
    events = [
        # 窗外(早)
        _make_started("2026-05-28T09:59:59Z", "c0", "web_search", {"query": "earlier"}),
        _make_completed("2026-05-28T09:59:59Z", "c0", "web_search", {"results": []}),
        # 窗内
        _make_started("2026-05-28T10:00:01Z", "c1", "web_fetch", {"url": "https://x"}),
        _make_completed("2026-05-28T10:00:02Z", "c1", "web_fetch", {"content": "ok"}),
        # 窗外(晚)
        _make_started("2026-05-28T10:00:10Z", "c2", "web_fetch", {"url": "https://y"}),
        _make_completed("2026-05-28T10:00:11Z", "c2", "web_fetch", {"content": "later"}),
    ]
    result = _collect_tool_calls_from_events(events, "2026-05-28T10:00:00Z", "2026-05-28T10:00:05Z")
    assert len(result) == 1
    assert result[0]["call_id"] == "c1"


def test_collect_handles_truncated_unparseable_result():
    # result 被截断,JSON 不合法
    truncated_result = '{"url": "https://x.com", "content": "## Task...truncated'
    events = [
        _make_started("2026-05-28T10:00:01Z", "c1", "web_fetch", {"query": "task"}),
        _make_completed("2026-05-28T10:00:02Z", "c1", "web_fetch", truncated_result, truncated=True),
    ]
    result = _collect_tool_calls_from_events(events, "2026-05-28T10:00:00Z", "2026-05-28T10:00:05Z")
    assert len(result) == 1
    call = result[0]
    assert call["result_truncated"] is True
    # 不能崩,应降级到 raw_head
    assert "result_summary" in call
    summary = call["result_summary"]
    assert summary.get("parse_error") is True
    assert "https://x.com" in summary["raw_head"]


def test_collect_handles_started_without_completed():
    events = [
        _make_started("2026-05-28T10:00:01Z", "c1", "web_fetch", {"query": "task"}),
        # 没有对应的 completed
    ]
    result = _collect_tool_calls_from_events(events, "2026-05-28T10:00:00Z", "2026-05-28T10:00:05Z")
    assert len(result) == 1
    assert result[0]["completed_at"] is None
    assert "result_summary" not in result[0]


def test_collect_preserves_call_order():
    events = [
        _make_started("2026-05-28T10:00:01Z", "c1", "web_search", {"query": "first"}),
        _make_completed("2026-05-28T10:00:02Z", "c1", "web_search", {"results": []}),
        _make_started("2026-05-28T10:00:03Z", "c2", "web_fetch", {"url": "https://x"}),
        _make_completed("2026-05-28T10:00:04Z", "c2", "web_fetch", {"content": "ok"}),
    ]
    result = _collect_tool_calls_from_events(events, "2026-05-28T10:00:00Z", "2026-05-28T10:00:05Z")
    assert [c["call_id"] for c in result] == ["c1", "c2"]
    assert result[0]["tool_name"] == "web_search"
    assert result[1]["tool_name"] == "web_fetch"


def test_collect_ignores_non_tool_events():
    events = [
        {"ts": "2026-05-28T10:00:01Z", "type": "session.created", "payload": {"x": 1}},
        {"ts": "2026-05-28T10:00:02Z", "type": "message.user_appended", "payload": {"y": 2}},
        _make_started("2026-05-28T10:00:03Z", "c1", "web_fetch", {"query": "task"}),
        _make_completed("2026-05-28T10:00:04Z", "c1", "web_fetch", {"content": "ok"}),
    ]
    result = _collect_tool_calls_from_events(events, "2026-05-28T10:00:00Z", "2026-05-28T10:00:05Z")
    assert len(result) == 1
    assert result[0]["call_id"] == "c1"
