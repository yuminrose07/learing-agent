"""
前端真实 E2E 测试 Runner

执行真实数据集，调用前端 API，收集证据包。

用法:
    python tests/e2e/frontend_real_runner.py \
        --dataset tests/e2e/real_datasets/frontend-tool-calls-single-session.json \
        --output .test_artifacts/e2e_real_runs/frontend-tool-calls-single-session

"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx


def now_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H%M%SZ")


@dataclass
class CaseResult:
    case_id: str
    success: bool
    error: Optional[str] = None
    response_text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    duration_ms: int = 0


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


class FrontendRealRunner:
    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        output_dir: Optional[Path] = None,
        request_timeout: float = 300.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.output_dir = output_dir or Path(".test_artifacts/e2e_real_runs")
        # 300s 默认值用于覆盖多轮 ReAct combo 用例:每轮约 30-45s,5-6 轮可能累加到 200s+。
        self.client = httpx.AsyncClient(timeout=request_timeout)
        self.session_id: Optional[str] = None

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
        """非流式聊天，直接返回完整响应。"""
        resp = await self.client.post(
            f"{self.base_url}/sessions/{session_id}/chat",
            json={"message": message, "stream": False, "mode": mode},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_session_events(self, session_id: str) -> list[dict]:
        """获取会话事件流（如果后端支持）。"""
        try:
            resp = await self.client.get(
                f"{self.base_url}/internal/sessions/{session_id}/events",
            )
            if resp.status_code == 200:
                return resp.json().get("events", [])
        except Exception:
            pass
        return []

    async def run_case(self, case: dict) -> CaseResult:
        case_id = case["id"]
        input_data = case.get("input", {})
        message = input_data.get("message", "")

        start = time.time()
        try:
            # 确保会话已创建
            if not self.session_id:
                await self.create_session("真实工具调用 E2E - 单会话覆盖")

            # 发送聊天请求
            response = await self.chat_non_stream(self.session_id, message)
            content = response.get("content", "")
            duration_ms = int((time.time() - start) * 1000)

            # 尝试获取事件（可选）
            events = await self.get_session_events(self.session_id)

            return CaseResult(
                case_id=case_id,
                success=True,
                response_text=content,
                events=events,
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = int((time.time() - start) * 1000)
            return CaseResult(
                case_id=case_id,
                success=False,
                error=str(e),
                duration_ms=duration_ms,
            )

    async def run_dataset(self, dataset_path: Path) -> RunResult:
        with open(dataset_path, "r", encoding="utf-8") as f:
            dataset = json.load(f)

        cases = dataset.get("cases", [])
        suite_id = dataset.get("suite_id", "unknown")
        run_id = f"{now_str()}_{suite_id}_manual"

        start_time = now_str()
        results: list[CaseResult] = []

        for i, case in enumerate(cases):
            print(f"[{i+1}/{len(cases)}] Running {case['id']}...")
            result = await self.run_case(case)
            results.append(result)
            if result.success:
                print(f"  ✓ Success ({result.duration_ms}ms)")
            else:
                print(f"  ✗ Failed: {result.error}")

        end_time = now_str()

        # 计算汇总
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
            session_id=self.session_id,
            results=results,
            summary=summary,
        )

    async def save_evidence(self, run_result: RunResult, output_dir: Path) -> Path:
        run_dir = output_dir / run_result.run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        # manifest.json
        manifest = {
            "run_id": run_result.run_id,
            "suite_id": run_result.suite_id,
            "start_time": run_result.start_time,
            "end_time": run_result.end_time,
            "base_url": run_result.base_url,
            "session_id": run_result.session_id,
            "mode": "manual",
            "frontend_mode": "chat",
        }
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # summary.json
        (run_dir / "summary.json").write_text(
            json.dumps(run_result.summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # results.jsonl
        with open(run_dir / "results.jsonl", "w", encoding="utf-8") as f:
            for r in run_result.results:
                record = {
                    "case_id": r.case_id,
                    "success": r.success,
                    "error": r.error,
                    "response_text": r.response_text[:500] if r.response_text else "",
                    "duration_ms": r.duration_ms,
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        # cases/<case_id>/
        cases_dir = run_dir / "cases"
        for r in run_result.results:
            case_dir = cases_dir / r.case_id
            case_dir.mkdir(parents=True, exist_ok=True)

            # request.json
            (case_dir / "request.json").write_text(
                json.dumps({"case_id": r.case_id}, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )

            # response.json
            (case_dir / "response.json").write_text(
                json.dumps({
                    "success": r.success,
                    "response_text": r.response_text,
                    "error": r.error,
                }, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )

            # events.jsonl（如果有）
            if r.events:
                with open(case_dir / "events.jsonl", "w", encoding="utf-8") as f:
                    for ev in r.events:
                        f.write(json.dumps(ev, ensure_ascii=False) + "\n")

        # 软链 latest
        latest_link = output_dir / "latest"
        if latest_link.is_symlink() or latest_link.exists():
            latest_link.unlink()
        latest_link.symlink_to(run_dir.relative_to(output_dir), target_is_directory=True)

        return run_dir

    async def close(self):
        await self.client.aclose()


async def main():
    parser = argparse.ArgumentParser(description="前端真实 E2E 测试 Runner")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("tests/e2e/real_datasets/frontend-tool-calls-single-session.json"),
        help="数据集 JSON 文件路径",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".test_artifacts/e2e_real_runs/frontend-tool-calls-single-session"),
        help="证据包输出目录",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Web Server 基础 URL",
    )
    parser.add_argument(
        "--server-cmd",
        default="python -m learning_agent.learning_agent.main --web --port 8000",
        help="启动 Web Server 的命令",
    )
    parser.add_argument(
        "--no-server",
        action="store_true",
        help="如果 Web Server 已在外部启动，跳过自动启动",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=300.0,
        help="单次 HTTP 请求的超时时间(秒)。combo/多轮工具用例需要更长超时,默认 300s。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只验证数据集格式，不真正执行",
    )

    args = parser.parse_args()

    if args.dry_run:
        print(f"[Dry Run] 验证数据集: {args.dataset}")
        with open(args.dataset, "r", encoding="utf-8") as f:
            dataset = json.load(f)
        print(f"  Suite ID: {dataset.get('suite_id')}")
        print(f"  Cases: {len(dataset.get('cases', []))}")
        print(f"  格式验证通过")
        return

    print(f"=" * 60)
    print(f"前端真实 E2E 测试")
    print(f"=" * 60)
    print(f"数据集: {args.dataset}")
    print(f"输出目录: {args.output}")
    print(f"Web Server: {args.base_url}")

    # 启动 Web Server（如果未在运行）
    server_proc = None
    if not args.no_server:
        # 检查是否已在运行
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            host, port = args.base_url.replace("http://", "").split(":")
            sock.connect((host, int(port)))
            sock.close()
            print(f"  Web Server 已在运行: {args.base_url}")
        except Exception:
            print(f"  正在启动 Web Server...")
            import subprocess
            server_proc = subprocess.Popen(
                args.server_cmd.split(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            # 等待服务启动
            for i in range(30):
                try:
                    import socket
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(1)
                    host, port = args.base_url.replace("http://", "").split(":")
                    sock.connect((host, int(port)))
                    sock.close()
                    print(f"  Web Server 已启动: {args.base_url}")
                    break
                except Exception:
                    await asyncio.sleep(0.5)
            else:
                print("  [错误] Web Server 启动超时")
                if server_proc:
                    server_proc.terminate()
                sys.exit(1)

    try:
        # 执行测试
        runner = FrontendRealRunner(
            base_url=args.base_url,
            output_dir=args.output,
            request_timeout=args.request_timeout,
        )
        run_result = await runner.run_dataset(args.dataset)

        # 保存证据包
        run_dir = await runner.save_evidence(run_result, args.output)

        # 打印摘要
        print(f"\n{'=' * 60}")
        print(f"测试完成")
        print(f"{'=' * 60}")
        print(f"Run ID: {run_result.run_id}")
        print(f"证据包: {run_dir}")
        print(f"\n汇总:")
        print(f"  总用例: {run_result.summary['total_cases']}")
        print(f"  通过: {run_result.summary['passed']}")
        print(f"  失败: {run_result.summary['failed']}")
        print(f"  通过率: {run_result.summary['pass_rate']:.1%}")
        print(f"  总耗时: {run_result.summary['total_duration_ms']}ms")

        # 列出失败的用例
        failed = [r for r in run_result.results if not r.success]
        if failed:
            print(f"\n失败的用例:")
            for r in failed:
                print(f"  - {r.case_id}: {r.error}")

        # 返回退出码
        sys.exit(0 if run_result.summary['failed'] == 0 else 1)

    finally:
        await runner.close()
        if server_proc:
            print(f"\n正在停止 Web Server...")
            server_proc.terminate()
            try:
                server_proc.wait(timeout=5)
            except Exception:
                server_proc.kill()


if __name__ == "__main__":
    asyncio.run(main())
