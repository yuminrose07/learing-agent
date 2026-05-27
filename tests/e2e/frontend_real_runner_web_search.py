"""
Web Search / Fetch 真实 E2E 测试 Runner

在单会话内执行 web_search 和 web_fetch 工具测试。

用法:
    python tests/e2e/frontend_real_runner_web_search.py \
        --dataset tests/e2e/real_datasets/web-search-fetch-deep-real.json \
        --output .test_artifacts/e2e_real_runs/web-search-fetch-deep
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


@dataclass
class CaseResult:
    case_id: str
    title: str
    success: bool
    error: Optional[str] = None
    response_text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    session_id: Optional[str] = None
    duration_ms: int = 0
    user_message: str = ""


@dataclass
class RunResult:
    run_id: str
    suite_id: str
    start_time: str
    end_time: str
    base_url: str
    session_id: Optional[str]
    results: list[CaseResult]
    summary: dict


class WebSearchFetchRunner:
    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        output_dir: Optional[Path] = None,
        progress_path: Optional[Path] = None,
        run_id_override: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.output_dir = output_dir or Path(".test_artifacts/e2e_real_runs")
        self.client = httpx.AsyncClient(timeout=600.0)
        self.session_id: Optional[str] = None
        # 可选的"流式进度文件"。设置后,每个 case 跑完立刻 append 一行
        # JSON,供 Web 控制台(SSE)实时染色用。不设置则行为完全不变。
        self.progress_path: Optional[Path] = progress_path
        # 可选的 run_id 覆盖。Web 控制台 POST 时预生成 run_id 传进来,
        # 这样 API 可以立刻返回稳定 ID,不必等 subprocess 启动。
        self.run_id_override: Optional[str] = run_id_override

    def _emit_progress(self, event: dict) -> None:
        """append 一行 JSON 到 progress_path。失败仅打日志,不影响主流程。"""
        if self.progress_path is None:
            return
        try:
            self.progress_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.progress_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
                f.flush()
        except OSError as exc:
            print(f"  [progress] write {self.progress_path} failed: {exc}")

    async def create_session(self, title: str) -> str:
        resp = await self.client.post(
            f"{self.base_url}/sessions",
            json={"title": title},
        )
        resp.raise_for_status()
        data = resp.json()
        self.session_id = data["id"]
        return self.session_id

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
        resp = await self.client.get(
            f"{self.base_url}/sessions/{session_id}/events",
        )
        resp.raise_for_status()
        return resp.json()

    async def get_session(self, session_id: str) -> dict:
        resp = await self.client.get(
            f"{self.base_url}/sessions/{session_id}",
        )
        resp.raise_for_status()
        return resp.json()

    async def run_case(self, case: dict, session_id: str) -> CaseResult:
        case_id = case["id"]
        title = case.get("title", "")
        input_data = case.get("input", {})
        message = input_data.get("message", "")
        tool_chain = case.get("tool_chain", [])

        print(f"  标题: {title}")
        print(f"  工具链: {' -> '.join(tool_chain)}")
        print(f"  用户: {message[:80]}...")

        start = time.time()
        response_text = ""
        tool_calls = []

        try:
            response = await self.chat_non_stream(session_id, message)
            response_text = response.get("content", "")
            tool_calls = response.get("tool_calls", [])
            print(f"      响应: {response_text[:100]}...")
            print(f"      工具调用: {len(tool_calls)}")

            success = True

            return CaseResult(
                case_id=case_id,
                title=title,
                success=success,
                response_text=response_text,
                tool_calls=tool_calls,
                session_id=session_id,
                duration_ms=int((time.time() - start) * 1000),
                user_message=message,
            )

        except Exception as e:
            return CaseResult(
                case_id=case_id,
                title=title,
                success=False,
                error=str(e),
                response_text=response_text,
                tool_calls=tool_calls,
                session_id=session_id,
                duration_ms=int((time.time() - start) * 1000),
                user_message=message,
            )

    async def run_dataset(self, dataset_path: Path) -> RunResult:
        with open(dataset_path, "r", encoding="utf-8") as f:
            dataset = json.load(f)

        cases = dataset.get("cases", [])
        suite_id = dataset.get("suite_id", "unknown")
        session_plan = dataset.get("session_plan", {})
        session_title = session_plan.get("session_title", suite_id)

        # run_id 优先用外部注入(Web 控制台 POST 时预生成,以便 SSE 立刻
        # 知道目录名)。未注入则保持原"时间戳_suite_manual"格式。
        run_id = self.run_id_override or f"{now_str()}_{suite_id}_manual"

        start_time = now_str()

        print(f"\n{'='*60}")
        print(f"创建会话: {session_title}")
        print(f"总case数: {len(cases)}")
        print(f"{'='*60}\n")

        session_id = await self.create_session(session_title)
        print(f"Session ID: {session_id}\n")

        # 写一行 run_started,让 SSE 消费方立刻拿到 total_cases / run_id。
        self._emit_progress({
            "event": "run_started",
            "run_id": run_id,
            "suite_id": suite_id,
            "session_id": session_id,
            "total_cases": len(cases),
            "ts": now_str(),
        })

        results: list[CaseResult] = []

        for i, case in enumerate(cases):
            case_id = case["id"]

            print(f"\n[{i+1}/{len(cases)}] {case_id}")
            print(f"{'='*50}")

            result = await self.run_case(case, session_id)
            results.append(result)

            if result.success:
                print(f"  ✓ 成功 ({result.duration_ms}ms)")
            else:
                print(f"  ✗ 失败: {result.error}")

            # 单 case 完成进度。SSE 拿到这一行就染色 + 推进进度条。
            self._emit_progress({
                "event": "case_done",
                "index": i,
                "case_id": case_id,
                "title": result.title,
                "success": result.success,
                "error": result.error,
                "duration_ms": result.duration_ms,
                "tool_calls_count": len(result.tool_calls),
                "ts": now_str(),
            })

            await asyncio.sleep(1)

        end_time = now_str()

        total = len(results)
        passed = sum(1 for r in results if r.success)
        failed = total - passed

        summary = {
            "total_cases": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": round(passed / total, 4) if total > 0 else 0,
            "total_duration_ms": sum(r.duration_ms for r in results),
        }

        return RunResult(
            run_id=run_id,
            suite_id=suite_id,
            start_time=start_time,
            end_time=end_time,
            base_url=self.base_url,
            session_id=session_id,
            results=results,
            summary=summary,
        )

    def _emit_run_finished(self, run_result: RunResult, run_dir: Path) -> None:
        """run 完成事件,带最终汇总 + 落盘路径,供 SSE 推 done。"""
        self._emit_progress({
            "event": "run_finished",
            "run_id": run_result.run_id,
            "summary": run_result.summary,
            "run_dir": str(run_dir),
            "ts": now_str(),
        })

    async def save_evidence(self, run_result: RunResult, output_dir: Path) -> Path:
        run_dir = output_dir / run_result.run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        manifest = {
            "run_id": run_result.run_id,
            "suite_id": run_result.suite_id,
            "start_time": run_result.start_time,
            "end_time": run_result.end_time,
            "base_url": run_result.base_url,
            "session_id": run_result.session_id,
            "mode": "chat",
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
                    "success": r.success,
                    "error": r.error,
                    "response_text": r.response_text[:500] if r.response_text else "",
                    "tool_calls_count": len(r.tool_calls),
                    "duration_ms": r.duration_ms,
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        if run_result.session_id:
            try:
                session_data = await self.get_session(run_result.session_id)
                (run_dir / "session.json").write_text(
                    json.dumps(session_data, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            except Exception:
                pass

            try:
                events = await self.get_session_events(run_result.session_id)
                with open(run_dir / "events.jsonl", "w", encoding="utf-8") as f:
                    for event in events:
                        f.write(json.dumps(event, ensure_ascii=False) + "\n")
            except Exception:
                pass

        cases_dir = run_dir / "cases"
        for r in run_result.results:
            case_dir = cases_dir / r.case_id
            case_dir.mkdir(parents=True, exist_ok=True)

            (case_dir / "request.json").write_text(
                json.dumps({
                    "case_id": r.case_id,
                    "title": r.title,
                    "user_message": r.user_message,
                }, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )

            (case_dir / "response.json").write_text(
                json.dumps({
                    "success": r.success,
                    "error": r.error,
                    "response_text": r.response_text,
                    "tool_calls": r.tool_calls,
                }, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )

        latest_link = output_dir / "latest"
        if latest_link.is_symlink() or latest_link.exists():
            latest_link.unlink()
        latest_link.symlink_to(run_dir.relative_to(output_dir), target_is_directory=True)

        return run_dir

    async def close(self):
        await self.client.aclose()


async def main():
    parser = argparse.ArgumentParser(description="Web Search / Fetch 真实 E2E 测试 Runner")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("tests/e2e/real_datasets/web-search-fetch-deep-real.json"),
        help="数据集 JSON 文件路径",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".test_artifacts/e2e_real_runs/web-search-fetch-deep"),
        help="证据包输出目录",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Web Server 基础 URL",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只验证数据集格式，不真正执行",
    )
    parser.add_argument(
        "--progress-jsonl",
        type=Path,
        default=None,
        help="可选:实时进度文件路径,每个 case 跑完 append 一行 JSON。供 Web 控制台 SSE 消费。",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="可选:覆盖 run_id(用作落盘目录名)。Web 控制台 POST 时预生成,以便立刻返回稳定 ID。",
    )

    args = parser.parse_args()

    if args.dry_run:
        print(f"[Dry Run] 验证数据集: {args.dataset}")
        with open(args.dataset, "r", encoding="utf-8") as f:
            dataset = json.load(f)
        print(f"  Suite ID: {dataset.get('suite_id')}")
        print(f"  Description: {dataset.get('description', '')[:80]}")
        print(f"  Cases: {len(dataset.get('cases', []))}")
        session_plan = dataset.get("session_plan", {})
        print(f"  Session Title: {session_plan.get('session_title', 'N/A')}")
        print(f"  Single Session: {session_plan.get('single_session_required', False)}")
        for case in dataset.get('cases', []):
            tool_chain = case.get('tool_chain', [])
            print(f"    - {case['id']}: {' -> '.join(tool_chain)}")
        print(f"  格式验证通过")
        return

    print(f"=" * 60)
    print(f"Web Search / Fetch 真实 E2E 测试")
    print(f"=" * 60)
    print(f"数据集: {args.dataset}")
    print(f"输出目录: {args.output}")
    print(f"Web Server: {args.base_url}")

    runner = WebSearchFetchRunner(
        base_url=args.base_url,
        output_dir=args.output,
        progress_path=args.progress_jsonl,
        run_id_override=args.run_id,
    )

    try:
        run_result = await runner.run_dataset(args.dataset)

        run_dir = await runner.save_evidence(run_result, args.output)
        runner._emit_run_finished(run_result, run_dir)

        print(f"\n{'=' * 60}")
        print(f"测试完成")
        print(f"{'=' * 60}")
        print(f"Run ID: {run_result.run_id}")
        print(f"Session ID: {run_result.session_id}")
        print(f"证据包: {run_dir}")
        print(f"\n汇总:")
        print(f"  总用例: {run_result.summary['total_cases']}")
        print(f"  通过: {run_result.summary['passed']}")
        print(f"  失败: {run_result.summary['failed']}")
        print(f"  通过率: {run_result.summary['pass_rate']:.1%}")
        print(f"  总耗时: {run_result.summary['total_duration_ms']}ms")

        failed = [r for r in run_result.results if not r.success]
        if failed:
            print(f"\n失败的用例:")
            for r in failed:
                print(f"  - {r.case_id}: {r.error}")

        sys.exit(0 if run_result.summary['failed'] == 0 else 1)

    finally:
        await runner.close()


if __name__ == "__main__":
    asyncio.run(main())
