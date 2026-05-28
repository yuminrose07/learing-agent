"""
真实 E2E 数据集统一 Runner

合并自 frontend_real_runner.py / _v2.py / _learning.py 三者。本 runner 通过 HTTP
打外部 dev server，按数据集 case 的字段形态自动分流驱动，并在每条 case 跑完后
做最小可用的 expect/forbid 不变量判定。

不在范围:
    - frontend_real_runner_web_search.py 是独立的 web_search 专用 runner，有自己
      的进度事件契约 (_emit_progress) 和 eval console 集成，本次合并不动它。
    - env_overrides 自动注入。runner 不持有 dev server 进程，注入需手工 export。
    - runtime-overview / unresolved-failures GET 端点（决策不加，靠拷文件 + events
      反推）。
    - 完整版 expect/forbid 判定（if_X_then_Y、_should_equal 等留下次迭代）。

用法:
    python tests/e2e/real_runner.py \\
        --dataset tests/e2e/real_datasets/<suite>.json \\
        --output .test_artifacts/e2e_real_runs/<suite>/ \\
        --base-url http://localhost:8000 \\
        --data-dir .learning_agent_data
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx


RUNNER_VERSION = "1.0.0"
REQUEST_TIMEOUT_DEFAULT = 300.0  # 多轮 ReAct combo 可能累加到 200s+
TERMINAL_STATES = {"completed", "error"}
NON_TERMINAL_STATES = {"idle", "aligning", "building_context", "calling_llm", "streaming", "executing_tool"}


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


# ── Progress emitter (eval console subprocess contract) ─────────────────────


class ProgressEmitter:
    """写 jsonl 给 eval_routes._tail_progress 用。

    契约（沿用 frontend_real_runner_web_search.py 已建立的事件 schema）:
        run_started:   {event, run_id, suite_id, session_id, total_cases, ts}
        case_done:     {event, index, case_id, title, success, error,
                         duration_ms, tool_calls_count, ts, verdict, skipped_reason}
        run_finished:  {event, run_id, summary, run_dir, ts}

    success 字段语义（UI eval.js 期望 true/false 二分）:
        verdict=pass    → success=true
        verdict=fail    → success=false
        verdict=skipped → success=null（加 skipped_reason 让 UI 能展示原因）
    """

    def __init__(self, path: Optional[Path]):
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)

    def emit(self, event: dict) -> None:
        if self.path is None:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
                f.flush()
        except Exception:
            # 进度写失败不影响业务执行。
            pass


# ── Data structures ─────────────────────────────────────────────────────────


@dataclass
class CaseEvidence:
    """单 case 的真实证据包，喂给判定引擎。"""
    response_texts: list[str] = field(default_factory=list)
    user_messages: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    session_id: Optional[str] = None
    learning_unit_id: Optional[str] = None
    unresolved_failures: list[dict] = field(default_factory=list)
    state_final: str = "unknown"


@dataclass
class CaseVerdict:
    case_id: str
    verdict: str  # "pass" | "fail" | "skipped"
    fail_reasons: list[str] = field(default_factory=list)
    unknown_assertions: list[str] = field(default_factory=list)
    evaluated_expect_count: int = 0
    evaluated_forbid_count: int = 0
    skipped_reason: Optional[str] = None


@dataclass
class CaseResult:
    case_id: str
    title: str
    duration_ms: int
    evidence: CaseEvidence
    verdict: CaseVerdict
    transport_error: Optional[str] = None  # HTTP 层异常（非业务 fail）


@dataclass
class RunResult:
    run_id: str
    suite_id: str
    start_time: str
    end_time: str
    base_url: str
    data_dir: str
    results: list[CaseResult]
    summary: dict


# ── Judge engine (minimal viable set) ───────────────────────────────────────


SUPPORTED_EXPECT_KEYS = {
    "non_empty_response",
    "state_final_in",
    "assistant_message_count_min",
    "agent_loop_invoked",
    "events_should_include_any",
    "events_should_include_any_of",
    "events_should_not_include_any",
    "unresolved_failure_count_max",
    "runtime_overview_captured",
}

SUPPORTED_FORBID_KEYS = {
    "empty_response",
    "state_final_is_streaming",
    "state_final_is_executing_tool",
    "state_final_is_calling_llm",
    "assistant_message_missing",
    "agent_unhandled_error_event",
    "orphan_tool_call_uncompensated",
}


def evaluate_case(case: dict, evidence: CaseEvidence) -> CaseVerdict:
    """对一条 case 跑最小可用判定，返回 verdict。"""
    expect = case.get("expect", {}) or {}
    forbid = case.get("forbid", {}) or {}
    fail_reasons: list[str] = []
    unknown_assertions: list[str] = []
    eval_expect = 0
    eval_forbid = 0

    event_types = [e.get("type", "") for e in evidence.events]
    response_non_empty = [r for r in evidence.response_texts if r and r.strip()]

    # ── expect ──
    for key, value in expect.items():
        if key not in SUPPORTED_EXPECT_KEYS:
            unknown_assertions.append(f"expect.{key}")
            continue
        eval_expect += 1

        if key == "non_empty_response" and value is True:
            if not response_non_empty:
                fail_reasons.append("expect.non_empty_response: response 全为空")

        elif key == "state_final_in":
            allowed = [s.lower() for s in (value or [])]
            if evidence.state_final.lower() not in allowed:
                fail_reasons.append(
                    f"expect.state_final_in: 期望 {allowed}, 实际 '{evidence.state_final}'"
                )

        elif key == "assistant_message_count_min":
            n = int(value)
            if len(response_non_empty) < n:
                fail_reasons.append(
                    f"expect.assistant_message_count_min: 期望 >= {n}, 实际 {len(response_non_empty)}"
                )

        elif key == "agent_loop_invoked" and value is True:
            # L1 没有 agent.stateChanged。turn 进了 agent loop 的真实证据是
            # user 消息被 append（message.user_appended）。
            if not any(t == "message.user_appended" for t in event_types):
                fail_reasons.append("expect.agent_loop_invoked: 没有 message.user_appended 事件")

        elif key in ("events_should_include_any", "events_should_include_any_of"):
            expected = set(value or [])
            if expected and not (expected & set(event_types)):
                fail_reasons.append(
                    f"expect.{key}: 期望出现 {sorted(expected)} 中任一, 实际都没出现"
                )

        elif key == "events_should_not_include_any":
            forbidden = set(value or [])
            hit = forbidden & set(event_types)
            if hit:
                fail_reasons.append(
                    f"expect.events_should_not_include_any: 不该出现的事件出现了 {sorted(hit)}"
                )

        elif key == "unresolved_failure_count_max":
            n = int(value)
            actual = len(evidence.unresolved_failures)
            if actual > n:
                fail_reasons.append(
                    f"expect.unresolved_failure_count_max: 期望 <= {n}, 实际 {actual}"
                )

        elif key == "runtime_overview_captured" and value is True:
            if not evidence.events:
                fail_reasons.append("expect.runtime_overview_captured: events.jsonl 为空")

    # ── forbid ──
    for key, value in forbid.items():
        if key not in SUPPORTED_FORBID_KEYS:
            unknown_assertions.append(f"forbid.{key}")
            continue
        eval_forbid += 1

        if key == "empty_response" and value is True:
            if not response_non_empty:
                fail_reasons.append("forbid.empty_response: response 全为空")

        elif key == "state_final_is_streaming" and value is True:
            if evidence.state_final.lower() == "streaming":
                fail_reasons.append("forbid.state_final_is_streaming: 终态卡在 streaming")

        elif key == "state_final_is_executing_tool" and value is True:
            if evidence.state_final.lower() == "executing_tool":
                fail_reasons.append("forbid.state_final_is_executing_tool: 终态卡在 executing_tool")

        elif key == "state_final_is_calling_llm" and value is True:
            if evidence.state_final.lower() == "calling_llm":
                fail_reasons.append("forbid.state_final_is_calling_llm: 终态卡在 calling_llm")

        elif key == "assistant_message_missing" and value is True:
            if not response_non_empty:
                fail_reasons.append("forbid.assistant_message_missing: 无 assistant 消息")

        elif key == "agent_unhandled_error_event" and value is True:
            # L1 没有 agent.unhandledError。真实的"未兜住的错误"信号是 web 层
            # 外层 rescue 写的 unresolved_failure（layer 含 outer）。
            outer = [
                r for r in evidence.unresolved_failures
                if "outer" in str(r.get("layer", "")).lower()
            ]
            if outer:
                fail_reasons.append(
                    f"forbid.agent_unhandled_error_event: web 层外层 rescue 触发 {len(outer)} 次"
                )

        elif key == "orphan_tool_call_uncompensated" and value is True:
            # L1 真实信号：流中断 = message.stream_failed；orphan 被补偿 =
            # 写了一条 synthetic tool 消息（is_error=True → tool.call_failed）。
            had_interrupt = "message.stream_failed" in event_types
            had_compensation = "tool.call_failed" in event_types
            if had_interrupt and not had_compensation:
                fail_reasons.append(
                    "forbid.orphan_tool_call_uncompensated: stream_failed 后未见 tool.call_failed（synthetic 补偿）"
                )

    verdict_label = "fail" if fail_reasons else "pass"
    return CaseVerdict(
        case_id=case["id"],
        verdict=verdict_label,
        fail_reasons=fail_reasons,
        unknown_assertions=unknown_assertions,
        evaluated_expect_count=eval_expect,
        evaluated_forbid_count=eval_forbid,
    )


# ── Runner ──────────────────────────────────────────────────────────────────


class RealRunner:
    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        output_dir: Optional[Path] = None,
        data_dir: Path = Path(".learning_agent_data"),
        request_timeout: float = REQUEST_TIMEOUT_DEFAULT,
        progress: Optional[ProgressEmitter] = None,
        forced_run_id: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.output_dir = output_dir or Path(".test_artifacts/e2e_real_runs")
        self.data_dir = data_dir
        self.client = httpx.AsyncClient(timeout=request_timeout)
        self._shared_session_id: Optional[str] = None  # 跨 case 共享 session 时用
        self._progress = progress or ProgressEmitter(None)
        self._forced_run_id = forced_run_id

    # ── HTTP primitives ──

    async def create_session(self, title: str) -> str:
        resp = await self.client.post(
            f"{self.base_url}/sessions",
            json={
                "title": title,
                "mode_metadata": {
                    "source": "eval",
                    "eval_run_id": self._forced_run_id,
                },
            },
        )
        resp.raise_for_status()
        return resp.json()["id"]

    async def create_learning_unit(self, seed_text: str) -> tuple[str, str]:
        resp = await self.client.post(
            f"{self.base_url}/learning-units",
            json={
                "seed_text": seed_text,
                "source": "ai_distilled",
                "mode_metadata": {
                    "source": "eval",
                    "eval_run_id": self._forced_run_id,
                },
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["session_id"], data["id"]

    async def chat_non_stream(
        self,
        session_id: str,
        message: str,
        mode: str = "chat",
    ) -> dict:
        resp = await self.client.post(
            f"{self.base_url}/sessions/{session_id}/chat",
            json={"message": message, "stream": False, "mode": mode},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_session_events(self, session_id: str) -> list[dict]:
        try:
            resp = await self.client.get(
                f"{self.base_url}/sessions/{session_id}/events",
            )
            if resp.status_code == 200:
                return resp.json().get("events", [])
        except Exception:
            pass
        return []

    # ── Per-case orchestration ──

    @staticmethod
    def _extract_turns(case: dict) -> list[dict]:
        """统一旧/新 schema 输入。turns 优先，input.message 退化为单 turn。"""
        if case.get("turns"):
            return case["turns"]
        input_data = case.get("input") or {}
        msg = input_data.get("message")
        if msg:
            return [{"role": "user", "message": msg}]
        return []

    @staticmethod
    def _resolve_mode(case: dict, dataset_frontend_mode: str) -> str:
        setup = case.get("setup") or {}
        return setup.get("frontend_mode") or dataset_frontend_mode or "chat"

    def _filter_unresolved_by_session(self, session_id: str) -> list[dict]:
        """从全局 unresolved_failures.jsonl 按 session_id 过滤。"""
        log_path = self.data_dir / "unresolved_failures.jsonl"
        if not log_path.exists():
            return []
        records: list[dict] = []
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("session_id") == session_id:
                        records.append(rec)
        except Exception:
            return []
        return records

    @staticmethod
    def _infer_state_final(events: list[dict], response_non_empty: bool) -> str:
        """从真实 L1 事件推断终态。

        L1 只写消息生命周期事件（session_manager._event_type_for_entry +
        record_message_stream_failed/interrupted），**没有** agent 状态机事件
        （agent.stateChanged 那些只在 EventBus 内存广播，不落 JSONL）。所以
        终态只能从消息生命周期推：

        - 取 message_end / message.stream_failed / message.interrupted 的最后一条:
            - message_end 在最后  → completed（含"中断后 finalize 又补了一条"的恢复路径）
            - stream_failed / interrupted 在最后 → error
        - 没有任何生命周期事件:
            - 有非空响应 → completed（rescue 路径可能没写 message_end）
            - 否则 → unknown
        """
        lifecycle_types = {
            "message_end",
            "message.stream_failed",
            "message.interrupted",
        }
        lifecycle = [e for e in events if e.get("type") in lifecycle_types]
        if not lifecycle:
            return "completed" if response_non_empty else "unknown"
        last = lifecycle[-1].get("type")
        return "completed" if last == "message_end" else "error"

    async def run_case(
        self,
        case: dict,
        dataset_frontend_mode: str,
        session_per_case: bool,
    ) -> CaseResult:
        case_id = case["id"]
        title = case.get("title", "")
        setup = case.get("setup") or {}
        env_overrides = setup.get("env_overrides") or {}

        evidence = CaseEvidence()
        verdict = CaseVerdict(case_id=case_id, verdict="pass")
        turns = self._extract_turns(case)
        planned_user_messages = [
            str(turn.get("message", ""))
            for turn in turns
            if isinstance(turn, dict) and turn.get("message")
        ]

        # ── env_overrides 处理 ──
        # 真实注入靠用户手工 export(runner 不持有 dev server 进程，改不了它的 env)。
        # 这里看 runner 自己进程的 env(由 eval_routes 启 subprocess 时继承自 dev
        # server 启动 shell)：所有需要的 key 都对得上 → 用户真做了 manual 设置,跑;
        # 任一 key 缺失/不匹配 → skip,避免污染统计。
        if env_overrides:
            mismatched = [
                k for k, v in env_overrides.items()
                if os.environ.get(k) != str(v)
            ]
            if mismatched:
                msg = (
                    f"manual_only env 未设置/不匹配: {mismatched}. "
                    f"手工 export 后重启 dev server 再跑此 case."
                )
                print(f"  ⊘ skip: {msg}")
                evidence.user_messages = planned_user_messages
                return CaseResult(
                    case_id=case_id,
                    title=title,
                    duration_ms=0,
                    evidence=evidence,
                    verdict=CaseVerdict(
                        case_id=case_id,
                        verdict="skipped",
                        skipped_reason=msg,
                    ),
                )
            print(f"  ✓ manual env 已就位: {list(env_overrides.keys())}")

        if not turns:
            return CaseResult(
                case_id=case_id,
                title=title,
                duration_ms=0,
                evidence=evidence,
                verdict=CaseVerdict(
                    case_id=case_id,
                    verdict="skipped",
                    skipped_reason="no_turns_or_input_message",
                ),
            )

        mode = self._resolve_mode(case, dataset_frontend_mode)
        start = time.time()

        try:
            # ── session/learning_unit 建立 ──
            session_id: Optional[str] = None
            learning_unit_id: Optional[str] = None
            create_lu = bool(setup.get("create_learning_unit", False))
            seed_text = setup.get("seed_text", "")

            if create_lu:
                if not seed_text:
                    raise ValueError("setup.create_learning_unit=true 但缺 seed_text")
                session_id, learning_unit_id = await self.create_learning_unit(seed_text)
                print(f"  创建 learning_unit: {learning_unit_id}, session: {session_id}")
            elif session_per_case or self._shared_session_id is None:
                session_id = await self.create_session(f"E2E - {case_id}")
                if not session_per_case:
                    self._shared_session_id = session_id
                print(f"  新建 session: {session_id}")
            else:
                session_id = self._shared_session_id
                print(f"  复用 session: {session_id}")

            evidence.session_id = session_id
            evidence.learning_unit_id = learning_unit_id

            # ── 跑 turns ──
            for i, turn in enumerate(turns):
                msg = turn.get("message", "")
                if not msg:
                    continue
                evidence.user_messages.append(msg)
                print(f"  [{i+1}/{len(turns)}] 用户: {msg[:60]}...")
                response = await self.chat_non_stream(session_id, msg, mode=mode)
                content = response.get("content", "") or ""
                evidence.response_texts.append(content)
                if response.get("tool_calls"):
                    evidence.tool_calls.extend(response["tool_calls"])
                print(f"      响应({len(content)} chars): {content[:80]}...")

            # ── 抓 evidence ──
            evidence.events = await self.get_session_events(session_id)
            evidence.unresolved_failures = self._filter_unresolved_by_session(session_id)
            _resp_non_empty = any(r and r.strip() for r in evidence.response_texts)
            evidence.state_final = self._infer_state_final(evidence.events, _resp_non_empty)

            # ── 判定 ──
            verdict = evaluate_case(case, evidence)

            duration_ms = int((time.time() - start) * 1000)
            return CaseResult(
                case_id=case_id,
                title=title,
                duration_ms=duration_ms,
                evidence=evidence,
                verdict=verdict,
            )

        except Exception as e:
            duration_ms = int((time.time() - start) * 1000)
            return CaseResult(
                case_id=case_id,
                title=title,
                duration_ms=duration_ms,
                evidence=evidence,
                verdict=CaseVerdict(
                    case_id=case_id,
                    verdict="fail",
                    fail_reasons=[f"transport_error: {type(e).__name__}: {e}"],
                ),
                transport_error=str(e),
            )

    # ── Dataset orchestration ──

    async def run_dataset(self, dataset_path: Path) -> RunResult:
        with open(dataset_path, "r", encoding="utf-8") as f:
            dataset = json.load(f)

        suite_id = dataset.get("suite_id", "unknown")
        cases = dataset.get("cases", [])
        session_plan = dataset.get("session_plan") or {}
        session_per_case = bool(session_plan.get("session_per_case", True))
        dataset_frontend_mode = session_plan.get("frontend_mode", "chat")

        run_id = self._forced_run_id or f"{now_str()}_{suite_id}_manual"
        start_time = now_str()
        results: list[CaseResult] = []

        print(f"\n{'=' * 60}")
        print(f"Suite: {suite_id}")
        print(f"Cases: {len(cases)}")
        print(f"frontend_mode: {dataset_frontend_mode}, session_per_case: {session_per_case}")
        print(f"{'=' * 60}\n")

        # eval console SSE 先消费 run_started 拿到 total_cases。session_id 这里
        # 还不知道（session_per_case 时每 case 都新建），留 None。
        self._progress.emit({
            "event": "run_started",
            "run_id": run_id,
            "suite_id": suite_id,
            "session_id": None,
            "total_cases": len(cases),
            "ts": now_str(),
        })

        for i, case in enumerate(cases):
            print(f"\n[{i+1}/{len(cases)}] {case['id']}")
            print(f"  标题: {case.get('title', '')}")
            result = await self.run_case(case, dataset_frontend_mode, session_per_case)
            results.append(result)
            v = result.verdict
            if v.verdict == "pass":
                print(f"  ✓ pass ({result.duration_ms}ms, expect={v.evaluated_expect_count}, forbid={v.evaluated_forbid_count})")
            elif v.verdict == "skipped":
                print(f"  ⊘ skipped: {v.skipped_reason}")
            else:
                print(f"  ✗ fail ({result.duration_ms}ms)")
                for reason in v.fail_reasons:
                    print(f"      - {reason}")
            if v.unknown_assertions:
                print(f"      [unknown: {len(v.unknown_assertions)}]")

            # case_done: success 字段 UI 用 true/false 二分着色，skipped 走 null。
            if v.verdict == "pass":
                success_field: Optional[bool] = True
            elif v.verdict == "fail":
                success_field = False
            else:
                success_field = None
            error_msg = result.transport_error or (
                "; ".join(v.fail_reasons) if v.fail_reasons else None
            )
            self._progress.emit({
                "event": "case_done",
                "index": i,
                "case_id": result.case_id,
                "title": result.title,
                "success": success_field,
                "verdict": v.verdict,
                "skipped_reason": v.skipped_reason,
                "error": error_msg,
                "duration_ms": result.duration_ms,
                "tool_calls_count": len(result.evidence.tool_calls),
                "ts": now_str(),
            })

        end_time = now_str()
        summary = self._compute_summary(results)

        return RunResult(
            run_id=run_id,
            suite_id=suite_id,
            start_time=start_time,
            end_time=end_time,
            base_url=self.base_url,
            data_dir=str(self.data_dir),
            results=results,
            summary=summary,
        )

    @staticmethod
    def _compute_summary(results: list[CaseResult]) -> dict:
        total = len(results)
        passed = sum(1 for r in results if r.verdict.verdict == "pass")
        failed = sum(1 for r in results if r.verdict.verdict == "fail")
        skipped = sum(1 for r in results if r.verdict.verdict == "skipped")
        fail_breakdown: dict[str, int] = {}
        unknown_keys: dict[str, int] = {}
        for r in results:
            for reason in r.verdict.fail_reasons:
                # 取冒号前的 key 作 breakdown
                head = reason.split(":", 1)[0].strip()
                fail_breakdown[head] = fail_breakdown.get(head, 0) + 1
            for k in r.verdict.unknown_assertions:
                unknown_keys[k] = unknown_keys.get(k, 0) + 1
        return {
            "total_cases": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "pass_rate": round(passed / total, 4) if total > 0 else 0.0,
            "fail_breakdown": fail_breakdown,
            "unknown_assertion_keys": unknown_keys,
            "total_duration_ms": sum(r.duration_ms for r in results),
        }

    # ── Evidence persistence ──

    async def save_evidence(self, run_result: RunResult, output_dir: Path) -> Path:
        run_dir = output_dir / run_result.run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        manifest = {
            "run_id": run_result.run_id,
            "suite_id": run_result.suite_id,
            "start_time": run_result.start_time,
            "end_time": run_result.end_time,
            "base_url": run_result.base_url,
            "data_dir": run_result.data_dir,
            "mode": "manual",
            "runner_version": RUNNER_VERSION,
            "runner_supports": {
                "env_overrides_injection": False,
                "unresolved_failure_capture": True,
                "events_capture": True,
                "expect_forbid_evaluation": "minimal",
            },
        }
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        (run_dir / "summary.json").write_text(
            json.dumps(run_result.summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        with open(run_dir / "results.jsonl", "w", encoding="utf-8") as f:
            for r in run_result.results:
                record = {
                    "case_id": r.case_id,
                    "title": r.title,
                    "verdict": r.verdict.verdict,
                    "fail_reasons": r.verdict.fail_reasons,
                    "unknown_assertions": r.verdict.unknown_assertions,
                    "evaluated_expect_count": r.verdict.evaluated_expect_count,
                    "evaluated_forbid_count": r.verdict.evaluated_forbid_count,
                    "skipped_reason": r.verdict.skipped_reason,
                    "transport_error": r.transport_error,
                    "session_id": r.evidence.session_id,
                    "learning_unit_id": r.evidence.learning_unit_id,
                    "state_final": r.evidence.state_final,
                    "response_text_preview": (r.evidence.response_texts[0][:300] if r.evidence.response_texts else ""),
                    "duration_ms": r.duration_ms,
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        cases_dir = run_dir / "cases"
        for r in run_result.results:
            case_dir = cases_dir / r.case_id
            case_dir.mkdir(parents=True, exist_ok=True)

            (case_dir / "request.json").write_text(
                json.dumps({
                    "case_id": r.case_id,
                    "title": r.title,
                    # UI modal 期望的字段（eval.js pickUserPrompt 读 user_message）。
                    # 多 turn 用 \n--- 分隔展示。
                    "user_message": "\n--- 下一轮 ---\n".join(r.evidence.user_messages) if r.evidence.user_messages else "",
                    "turns": r.evidence.user_messages,
                }, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            (case_dir / "response.json").write_text(
                json.dumps({
                    "verdict": r.verdict.verdict,
                    # UI modal 期望单数字段 response_text（eval.js:1044）。
                    # 多 turn 用 \n--- 分隔展示，最后一轮在末尾。
                    "response_text": "\n--- 下一轮 ---\n".join(r.evidence.response_texts) if r.evidence.response_texts else "",
                    "response_texts": r.evidence.response_texts,  # 完整原始保留
                    "tool_calls": r.evidence.tool_calls,
                    "state_final": r.evidence.state_final,
                    "fail_reasons": r.verdict.fail_reasons,
                    "unknown_assertions": r.verdict.unknown_assertions,
                    "transport_error": r.transport_error,
                    "duration_ms": r.duration_ms,
                }, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            if r.evidence.events:
                with open(case_dir / "events.jsonl", "w", encoding="utf-8") as f:
                    for ev in r.evidence.events:
                        f.write(json.dumps(ev, ensure_ascii=False) + "\n")

            if r.evidence.unresolved_failures:
                with open(case_dir / "unresolved_failures.jsonl", "w", encoding="utf-8") as f:
                    for rec in r.evidence.unresolved_failures:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        latest_link = output_dir / "latest"
        if latest_link.is_symlink() or latest_link.exists():
            latest_link.unlink()
        latest_link.symlink_to(run_dir.relative_to(output_dir), target_is_directory=True)

        return run_dir

    def emit_run_finished(self, run_result: RunResult, run_dir: Path) -> None:
        """run 完成事件,带最终汇总 + 落盘路径,SSE 收到这条会推 done。"""
        self._progress.emit({
            "event": "run_finished",
            "run_id": run_result.run_id,
            "summary": run_result.summary,
            "run_dir": str(run_dir),
            "ts": now_str(),
        })

    async def close(self):
        await self.client.aclose()


# ── CLI ─────────────────────────────────────────────────────────────────────


async def main():
    parser = argparse.ArgumentParser(description="真实 E2E 数据集统一 Runner")
    parser.add_argument("--dataset", type=Path, required=True, help="数据集 JSON 路径")
    parser.add_argument("--output", type=Path, required=True, help="证据包输出目录")
    parser.add_argument("--base-url", default="http://localhost:8000", help="dev server base URL")
    parser.add_argument("--data-dir", type=Path,
                        default=Path(os.environ.get("LA_DATA_DIR", ".learning_agent_data")),
                        help="dev server 的 data_dir（默认读 env LA_DATA_DIR，回退 .learning_agent_data）。"
                             "用于按 session_id 过滤拷 unresolved_failures.jsonl")
    parser.add_argument("--request-timeout", type=float, default=REQUEST_TIMEOUT_DEFAULT,
                        help=f"单次 HTTP 请求超时（默认 {REQUEST_TIMEOUT_DEFAULT}s）")
    parser.add_argument("--progress-jsonl", type=Path, default=None,
                        help="eval console SSE 进度文件路径。设了就按 run_started/case_done/run_finished 契约写")
    parser.add_argument("--run-id", default=None,
                        help="强制使用的 run_id（由 eval_routes 传入以对齐 SSE 订阅）")
    parser.add_argument("--dry-run", action="store_true", help="只校验数据集 schema，不发请求")
    args = parser.parse_args()

    if args.dry_run:
        print(f"[Dry Run] 验证数据集: {args.dataset}")
        with open(args.dataset, "r", encoding="utf-8") as f:
            dataset = json.load(f)
        suite_id = dataset.get("suite_id")
        cases = dataset.get("cases", [])
        print(f"  Suite ID: {suite_id}")
        print(f"  Cases: {len(cases)}")
        for case in cases:
            turns = case.get("turns") or ([{"role": "user", "message": (case.get("input") or {}).get("message", "")}] if (case.get("input") or {}).get("message") else [])
            setup = case.get("setup") or {}
            tags = []
            if setup.get("create_learning_unit"):
                tags.append("LU")
            if setup.get("manual_only"):
                tags.append("manual_only")
            if setup.get("env_overrides"):
                tags.append(f"env({len(setup['env_overrides'])})")
            tag_str = f" [{', '.join(tags)}]" if tags else ""
            print(f"    - {case['id']}: {len(turns)} turns{tag_str}")
        print(f"  schema 校验通过")
        return

    print(f"=" * 60)
    print(f"真实 E2E Runner v{RUNNER_VERSION}")
    print(f"=" * 60)
    print(f"数据集: {args.dataset}")
    print(f"输出: {args.output}")
    print(f"Server: {args.base_url}")
    print(f"data_dir: {args.data_dir}")

    # 检查 server 是否在跑
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        host, port = args.base_url.replace("http://", "").replace("https://", "").split(":")
        sock.connect((host, int(port)))
        sock.close()
        print(f"  ✓ Server 已在运行")
    except Exception:
        print(f"  ✗ Server 未运行，请先启动:")
        print(f"    python -m learning_agent.learning_agent.main --web --port {port if 'port' in locals() else 8000}")
        sys.exit(2)

    runner = RealRunner(
        base_url=args.base_url,
        output_dir=args.output,
        data_dir=args.data_dir,
        request_timeout=args.request_timeout,
        progress=ProgressEmitter(args.progress_jsonl),
        forced_run_id=args.run_id,
    )
    try:
        run_result = await runner.run_dataset(args.dataset)
        run_dir = await runner.save_evidence(run_result, args.output)
        runner.emit_run_finished(run_result, run_dir)

        print(f"\n{'=' * 60}")
        print(f"完成")
        print(f"{'=' * 60}")
        print(f"Run ID: {run_result.run_id}")
        print(f"证据包: {run_dir}")
        s = run_result.summary
        print(f"\n  Total: {s['total_cases']}  pass: {s['passed']}  fail: {s['failed']}  skip: {s['skipped']}")
        print(f"  pass_rate: {s['pass_rate']:.1%}  total_duration: {s['total_duration_ms']}ms")
        if s["fail_breakdown"]:
            print(f"\n  fail_breakdown:")
            for k, v in sorted(s["fail_breakdown"].items(), key=lambda x: -x[1]):
                print(f"    {k}: {v}")
        if s["unknown_assertion_keys"]:
            print(f"\n  unknown assertion keys (skipped by minimal judge):")
            for k, v in sorted(s["unknown_assertion_keys"].items(), key=lambda x: -x[1])[:10]:
                print(f"    {k}: {v}")

        sys.exit(0 if s["failed"] == 0 else 1)
    finally:
        await runner.close()


if __name__ == "__main__":
    asyncio.run(main())
