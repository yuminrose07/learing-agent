"""验证 web search runner 为评测控制台加的 additive 钩子。

只覆盖纯落盘 / progress 文件这两段(与后端 eval_routes 对接的契约):
- `_emit_progress` 把 dict 序列化成单行 JSONL,append 模式不覆盖既有内容
- `CaseResult.user_message` 可携带,且 `save_evidence` 落到 request.json
- `run_id_override` 决定 run_dir 名,与 web 控制台 POST 时预生成的 run_id 对齐

不真起 HTTP server——save_evidence 内部对 session/events 的 http 调用都被
try/except 兜底,这里通过 monkeypatch 让它们抛错,验证主流程不受影响。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "/Users/roseannk/my-agent")

from tests.e2e.frontend_real_runner_web_search import (
    CaseResult,
    RunResult,
    WebSearchFetchRunner,
    now_str,
)


def test_emit_progress_appends_jsonl(tmp_path: Path):
    progress = tmp_path / "p.jsonl"
    runner = WebSearchFetchRunner(progress_path=progress)
    runner._emit_progress({"event": "run_started", "run_id": "r1"})
    runner._emit_progress({"event": "case_done", "index": 0, "case_id": "c-1"})

    lines = progress.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "run_started"
    assert json.loads(lines[1])["case_id"] == "c-1"


def test_emit_progress_noop_when_path_unset(tmp_path: Path):
    """progress_path=None 时不应抛(向后兼容老命令行用法)。"""
    runner = WebSearchFetchRunner(progress_path=None)
    runner._emit_progress({"event": "ignored"})  # 不抛即通过


def test_case_result_carries_user_message():
    cr = CaseResult(case_id="c-1", title="t", success=True, user_message="你好")
    assert cr.user_message == "你好"


def test_save_evidence_writes_user_message_and_honors_run_id_override(tmp_path: Path):
    output_dir = tmp_path / "out"
    runner = WebSearchFetchRunner(output_dir=output_dir, run_id_override="custom-run-id")

    # save_evidence 末尾会拉 session+events,这里不起服务,直接让它们抛。
    async def boom(*_args, **_kwargs):
        raise RuntimeError("no server")
    runner.get_session = boom  # type: ignore[assignment]
    runner.get_session_events = boom  # type: ignore[assignment]

    result = CaseResult(
        case_id="c-1",
        title="第一个用例",
        success=True,
        response_text="答案",
        tool_calls=[{"name": "web_search", "args": {"q": "x"}}],
        session_id="sess-1",
        duration_ms=123,
        user_message="请帮我搜索 X",
    )
    run = RunResult(
        run_id="custom-run-id",
        suite_id="suite-x",
        start_time=now_str(),
        end_time=now_str(),
        base_url="http://localhost:8000",
        session_id="sess-1",
        results=[result],
        summary={
            "total_cases": 1, "passed": 1, "failed": 0,
            "pass_rate": 1.0, "total_duration_ms": 123,
        },
    )

    output_dir.mkdir(parents=True)
    run_dir = asyncio.run(runner.save_evidence(run, output_dir))
    try:
        assert run_dir.name == "custom-run-id"
        req = json.loads((run_dir / "cases" / "c-1" / "request.json").read_text(encoding="utf-8"))
        assert req["user_message"] == "请帮我搜索 X"
        assert req["case_id"] == "c-1"

        resp = json.loads((run_dir / "cases" / "c-1" / "response.json").read_text(encoding="utf-8"))
        assert resp["response_text"] == "答案"
        assert resp["tool_calls"][0]["name"] == "web_search"
    finally:
        asyncio.run(runner.close())


def test_emit_run_finished_writes_summary(tmp_path: Path):
    progress = tmp_path / "p.jsonl"
    runner = WebSearchFetchRunner(progress_path=progress)
    run = RunResult(
        run_id="run-z",
        suite_id="s",
        start_time=now_str(),
        end_time=now_str(),
        base_url="http://localhost:8000",
        session_id=None,
        results=[],
        summary={"total_cases": 0, "passed": 0, "failed": 0, "pass_rate": 0, "total_duration_ms": 0},
    )
    runner._emit_run_finished(run, tmp_path / "irrelevant")

    line = progress.read_text(encoding="utf-8").splitlines()[-1]
    payload = json.loads(line)
    assert payload["event"] == "run_finished"
    assert payload["run_id"] == "run-z"
    assert payload["summary"]["total_cases"] == 0
