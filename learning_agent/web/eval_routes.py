"""
Learning-Agent E2E 评测控制台 API（/api/eval）。

只读 API,扫 tests/e2e/real_datasets/ 与 .test_artifacts/e2e_real_runs/,
返回数据集与运行结果。POST /runs 启动一个 subprocess 跑 runner,
GET /runs/{id}/stream 用 SSE 推送实时进度。

路径规范（与 frontend_real_runner_web_search.py 落盘格式保持一致）:
- 数据集: tests/e2e/real_datasets/<dataset_id>.json
- 运行产物: .test_artifacts/e2e_real_runs/<dataset_id>/<run_id>/
    - manifest.json   : {run_id, suite_id, start_time, end_time, ...}
    - summary.json    : {total_cases, passed, failed, pass_rate, ...}
    - results.jsonl   : 每行一个 case 结果
    - cases/<case_id>/{request,response}.json
- 实时进度: .test_artifacts/eval_console/<run_id>.progress.jsonl
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# 仓库根。本文件位于 learning_agent/web/eval_routes.py,
# 父目录三层上是 my-agent/。
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DATASETS_DIR = _REPO_ROOT / "tests" / "e2e" / "real_datasets"
_RUNS_DIR = _REPO_ROOT / ".test_artifacts" / "e2e_real_runs"
_PROGRESS_DIR = _REPO_ROOT / ".test_artifacts" / "eval_console"
_RUNNER_SCRIPT = _REPO_ROOT / "tests" / "e2e" / "frontend_real_runner_web_search.py"

# SSE 心跳间隔(秒);静默时发 `: ping` 注释防止反向代理切连接。
_SSE_HEARTBEAT_SECONDS = 15.0
# Tail 文件的轮询间隔(秒)。case 之间一般要十几秒,1s 足够实时感。
_TAIL_INTERVAL_SECONDS = 1.0

router = APIRouter(prefix="/api/eval", tags=["eval"])


# ───────────────────────────────
# 通用工具
# ───────────────────────────────

def _now_str() -> str:
    """与 runner 内部一致的时间戳格式。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def _read_json_safe(path: Path) -> dict[str, Any] | None:
    """读 JSON 文件,失败时返回 None 并记录日志(不抛)。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[eval] read %s failed: %s", path, exc)
        return None


def _read_jsonl_safe(path: Path) -> list[dict[str, Any]]:
    """读 JSONL,跳过坏行。"""
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("[eval] bad jsonl line in %s", path)
    return out


# ───────────────────────────────
# 活跃 run 注册表(内存)
# ───────────────────────────────
#
# server 重启会丢。重启后 progress 文件依然在磁盘上,SSE 仍能 tail,
# 但拿不到 subprocess 状态——按"找不到活跃句柄就认为已完成"处理。

_active_runs: dict[str, dict[str, Any]] = {}


def _progress_path_for(run_id: str) -> Path:
    return _PROGRESS_DIR / f"{run_id}.progress.jsonl"


# ───────────────────────────────
# 数据集
# ───────────────────────────────

def _iter_dataset_files() -> list[Path]:
    """顶层 *.json 数据集文件(忽略子目录,如 workspace/)。"""
    if not _DATASETS_DIR.is_dir():
        return []
    return sorted(
        p for p in _DATASETS_DIR.iterdir()
        if p.is_file() and p.suffix == ".json"
    )


def _dataset_path_for(dataset_id: str) -> Path:
    """校验 dataset_id 安全且文件存在,返回绝对路径。"""
    if not dataset_id or "/" in dataset_id or "\\" in dataset_id or dataset_id.startswith("."):
        raise HTTPException(status_code=400, detail=f"invalid dataset_id: {dataset_id}")
    path = _DATASETS_DIR / f"{dataset_id}.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"dataset not found: {dataset_id}")
    return path


@router.get("/datasets")
async def list_datasets() -> dict[str, Any]:
    """列出所有数据集(仅元信息,不返回 cases 详情)。"""
    items: list[dict[str, Any]] = []
    for path in _iter_dataset_files():
        data = _read_json_safe(path)
        if not data:
            continue
        cases = data.get("cases") or []
        session_plan = data.get("session_plan") or {}
        items.append({
            "id": path.stem,
            "path": str(path.relative_to(_REPO_ROOT)),
            "suite_id": data.get("suite_id", path.stem),
            "case_count": len(cases),
            "description": data.get("description", ""),
            "session_title": session_plan.get("session_title", ""),
        })
    return {"datasets": items}


# ───────────────────────────────
# 运行历史(只读)
# ───────────────────────────────

def _find_run_dir(run_id: str) -> Path:
    """按 run_id 在 <dataset_id>/<run_id>/ 二层结构里查目录。

    只做文件系统级查找(不读 JSON),拿到 Path 后由调用方按需读取产物。
    跳过 `latest` 软链,避免双重命中。
    """
    if not _RUNS_DIR.is_dir():
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    for dataset_dir in _RUNS_DIR.iterdir():
        if not dataset_dir.is_dir():
            continue
        candidate = dataset_dir / run_id
        if candidate.is_dir() and not candidate.is_symlink():
            return candidate
    raise HTTPException(status_code=404, detail=f"run not found: {run_id}")


def _summarize_run(dataset_id: str, run_dir: Path) -> dict[str, Any]:
    """读 manifest + summary,组装单个 run 的摘要。"""
    manifest = _read_json_safe(run_dir / "manifest.json") or {}
    summary = _read_json_safe(run_dir / "summary.json") or {}
    run_id = manifest.get("run_id", run_dir.name)
    return {
        "run_id": run_id,
        "dataset_id": dataset_id,
        "started_at": manifest.get("start_time"),
        "ended_at": manifest.get("end_time"),
        "session_id": manifest.get("session_id"),
        "mode": manifest.get("mode"),
        "total_cases": summary.get("total_cases", 0),
        "passed": summary.get("passed", 0),
        "failed": summary.get("failed", 0),
        "pass_rate": summary.get("pass_rate"),
        # 仍在活跃表里 = 还没跑完;否则视为已完成。
        "status": "running" if run_id in _active_runs else "done",
    }


def _summarize_active(run_id: str, info: dict[str, Any]) -> dict[str, Any]:
    """活跃 run 还没落盘 manifest/summary,从内存表造摘要。"""
    return {
        "run_id": run_id,
        "dataset_id": info.get("dataset_id"),
        "started_at": info.get("started_at"),
        "ended_at": None,
        "session_id": None,
        "mode": "chat",
        "total_cases": info.get("total_cases", 0),
        "passed": 0,
        "failed": 0,
        "pass_rate": None,
        "status": "running",
    }


@router.get("/runs")
async def list_runs() -> dict[str, Any]:
    """扫所有 <dataset_id>/<run_id>/,合并活跃 run,按 started_at 倒序。"""
    runs: list[dict[str, Any]] = []
    seen: set[str] = set()

    if _RUNS_DIR.is_dir():
        for dataset_dir in _RUNS_DIR.iterdir():
            if not dataset_dir.is_dir():
                continue
            dataset_id = dataset_dir.name
            for run_dir in dataset_dir.iterdir():
                if run_dir.is_symlink() or not run_dir.is_dir():
                    continue
                summary = _summarize_run(dataset_id, run_dir)
                runs.append(summary)
                seen.add(summary["run_id"])

    # 还没落盘的活跃 run 也要展示(刚启动还没写 manifest 的情况)。
    for run_id, info in _active_runs.items():
        if run_id in seen:
            continue
        runs.append(_summarize_active(run_id, info))

    runs.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return {"runs": runs}


@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    """单个 run 的完整结果:manifest + summary + results.jsonl 全展开。

    活跃 run 在落盘 manifest 前也允许查询——此时只回内存摘要 + 已积累的
    progress 事件,results 为空。
    """
    try:
        run_dir = _find_run_dir(run_id)
    except HTTPException:
        if run_id not in _active_runs:
            raise
        # 活跃但还没落盘
        info = _active_runs[run_id]
        return {
            "run_id": run_id,
            "dataset_id": info.get("dataset_id"),
            "manifest": {},
            "summary": {},
            "results": [],
            "status": "running",
        }
    manifest = _read_json_safe(run_dir / "manifest.json") or {}
    summary = _read_json_safe(run_dir / "summary.json") or {}
    results = _read_jsonl_safe(run_dir / "results.jsonl")
    return {
        "run_id": run_id,
        "dataset_id": run_dir.parent.name,
        "manifest": manifest,
        "summary": summary,
        "results": results,
        "status": "running" if run_id in _active_runs else "done",
    }


@router.get("/runs/{run_id}/cases/{case_id}")
async def get_case(run_id: str, case_id: str) -> dict[str, Any]:
    """单个 case 的 request + response 全文。"""
    run_dir = _find_run_dir(run_id)
    case_dir = run_dir / "cases" / case_id
    if not case_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"case not found: {case_id}")
    return {
        "run_id": run_id,
        "case_id": case_id,
        "request": _read_json_safe(case_dir / "request.json"),
        "response": _read_json_safe(case_dir / "response.json"),
    }


# ───────────────────────────────
# 启动 run + SSE 进度流
# ───────────────────────────────

class StartRunRequest(BaseModel):
    dataset_id: str
    base_url: Optional[str] = None  # 默认走 server 自己


@router.post("/runs")
async def start_run(req: StartRunRequest) -> dict[str, Any]:
    """spawn runner subprocess。立刻返回 run_id,不等 subprocess 启动完成。"""
    dataset_path = _dataset_path_for(req.dataset_id)

    run_id = f"{_now_str()}_{req.dataset_id}_console"
    progress_path = _progress_path_for(run_id)
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    # 触一下文件,确保 SSE 端立刻能 open。
    progress_path.touch(exist_ok=True)

    output_dir = _RUNS_DIR / req.dataset_id
    output_dir.mkdir(parents=True, exist_ok=True)

    # 数据集的 case 数,SSE 端在 run_started 事件前就能展示总数。
    dataset_data = _read_json_safe(dataset_path) or {}
    total_cases = len(dataset_data.get("cases") or [])

    base_url = req.base_url or os.environ.get("EVAL_RUNNER_BASE_URL", "http://localhost:8000")

    cmd = [
        sys.executable,
        str(_RUNNER_SCRIPT),
        "--dataset", str(dataset_path),
        "--output", str(output_dir),
        "--base-url", base_url,
        "--progress-jsonl", str(progress_path),
        "--run-id", run_id,
    ]
    logger.info("[eval] starting run %s: %s", run_id, " ".join(cmd))

    # 标准输出/错误丢到日志文件,避免管道阻塞 + 方便事后排查。
    log_dir = _REPO_ROOT / ".test_artifacts" / "eval_console" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{run_id}.log"
    log_fp = open(log_path, "w", encoding="utf-8")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(_REPO_ROOT),
        stdout=log_fp,
        stderr=asyncio.subprocess.STDOUT,
    )

    _active_runs[run_id] = {
        "process": proc,
        "progress_path": progress_path,
        "log_path": log_path,
        "log_fp": log_fp,
        "dataset_id": req.dataset_id,
        "started_at": _now_str(),
        "base_url": base_url,
        "total_cases": total_cases,
    }

    return {
        "run_id": run_id,
        "dataset_id": req.dataset_id,
        "total_cases": total_cases,
        "stream_url": f"/api/eval/runs/{run_id}/stream",
    }


async def _tail_progress(run_id: str) -> AsyncGenerator[str, None]:
    """Tail progress.jsonl,把每行 JSON 包成 SSE 事件 yield 出去。

    退出条件(任一):
    - 文件里读到 run_finished
    - subprocess(若在活跃表)退出 且 没有新数据
    """
    progress_path = _progress_path_for(run_id)
    active_info = _active_runs.get(run_id)
    proc: Optional[asyncio.subprocess.Process] = active_info["process"] if active_info else None

    # SSE 打开时先推一行注释,促 Starlette flush 响应头。
    yield ": stream-open\n\n"

    # 等 progress 文件出现(POST 已 touch,这里基本立刻命中,但 server 重启
    # 场景下文件可能不存在)。
    waited = 0.0
    while not progress_path.is_file() and waited < 5.0:
        await asyncio.sleep(0.2)
        waited += 0.2
    if not progress_path.is_file():
        yield f"event: error\ndata: {json.dumps({'reason': 'progress file missing'})}\n\n"
        return

    offset = 0
    pending = ""  # 拼接未完整的最后一行
    last_emit = asyncio.get_event_loop().time()
    finished = False

    while True:
        try:
            with open(progress_path, "r", encoding="utf-8") as f:
                f.seek(offset)
                chunk = f.read()
                offset = f.tell()
        except OSError as exc:
            logger.warning("[eval] tail %s failed: %s", progress_path, exc)
            yield f"event: error\ndata: {json.dumps({'reason': str(exc)})}\n\n"
            return

        if chunk:
            pending += chunk
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("[eval] bad progress line: %s", line)
                    continue
                ev_type = event.get("event", "progress")
                yield f"event: {ev_type}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                last_emit = asyncio.get_event_loop().time()
                if ev_type == "run_finished":
                    finished = True

        if finished:
            break

        # subprocess 已退出 + 文件没有更多内容,认为结束。
        if proc is not None and proc.returncode is not None:
            # 再 tail 一遍 catch up
            try:
                with open(progress_path, "r", encoding="utf-8") as f:
                    f.seek(offset)
                    chunk = f.read()
                    offset = f.tell()
            except OSError:
                chunk = ""
            if not chunk:
                # 没收到 run_finished 但进程退了——按异常 done。
                yield f"event: done\ndata: {json.dumps({'reason': 'process exited', 'returncode': proc.returncode})}\n\n"
                break

        # 静默期心跳
        now = asyncio.get_event_loop().time()
        if now - last_emit >= _SSE_HEARTBEAT_SECONDS:
            yield ": ping\n\n"
            last_emit = now

        await asyncio.sleep(_TAIL_INTERVAL_SECONDS)

    # 正常结束:清理活跃表 + 关日志文件。
    info = _active_runs.pop(run_id, None)
    if info is not None:
        log_fp = info.get("log_fp")
        if log_fp is not None:
            try:
                log_fp.close()
            except Exception:
                pass

    yield f"event: done\ndata: {json.dumps({'run_id': run_id})}\n\n"


@router.get("/runs/{run_id}/stream")
async def stream_run(run_id: str) -> StreamingResponse:
    """SSE 进度流。在活跃表里或者磁盘上有 progress 文件都可以接。"""
    progress_path = _progress_path_for(run_id)
    if run_id not in _active_runs and not progress_path.is_file():
        raise HTTPException(status_code=404, detail=f"no progress for run: {run_id}")
    return StreamingResponse(
        _tail_progress(run_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
