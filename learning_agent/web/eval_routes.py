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
import copy
import json
import logging
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import yaml

from tests.e2e.compile_dataset import build_dataset, compile_dataset, load_dataset_source

logger = logging.getLogger(__name__)

# 仓库根。本文件位于 learning_agent/web/eval_routes.py,
# 父目录三层上是 my-agent/。
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DATASETS_DIR = _REPO_ROOT / "tests" / "e2e" / "real_datasets"
_DATASET_SOURCES_DIR = _REPO_ROOT / "tests" / "e2e" / "real_datasets_src"
_RUNS_DIR = _REPO_ROOT / ".test_artifacts" / "e2e_real_runs"
_PROGRESS_DIR = _REPO_ROOT / ".test_artifacts" / "eval_console"
_WEB_SEARCH_RUNNER_SCRIPT = _REPO_ROOT / "tests" / "e2e" / "frontend_real_runner_web_search.py"
# 统一 runner:合并自旧的 frontend_real_runner / _v2 / _learning。
# 当前 eval console 路由策略:
#   - suite_id 以 harness- 开头  →  real_runner（harness 数据集专用判定 key）
#   - frontend_mode == "learning" →  real_runner（吸收旧 learning runner 能力）
#   - 其他 chat 数据集            →  _web_search（保持旧契约 + tool_calls 聚合）
_REAL_RUNNER_SCRIPT = _REPO_ROOT / "tests" / "e2e" / "real_runner.py"
_RUNNER_SCRIPT = _WEB_SEARCH_RUNNER_SCRIPT

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


def _validate_dataset_id(dataset_id: str) -> None:
    if not dataset_id or "/" in dataset_id or "\\" in dataset_id or dataset_id.startswith("."):
        raise HTTPException(status_code=400, detail=f"invalid dataset_id: {dataset_id}")


def _json_safe_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _dump_yaml(value: Any) -> str:
    return yaml.safe_dump(
        _json_safe_copy(value),
        allow_unicode=True,
        sort_keys=False,
        width=1000,
    )


def _case_input_text(case: dict[str, Any]) -> str:
    turns = case.get("turns")
    if isinstance(turns, list):
        messages = [
            str(turn.get("message", ""))
            for turn in turns
            if isinstance(turn, dict) and turn.get("role", "user") == "user"
        ]
        return "\n\n".join(message for message in messages if message)
    input_data = case.get("input")
    if isinstance(input_data, dict):
        return str(input_data.get("message", ""))
    return ""


def _set_case_input_text(case: dict[str, Any], message: str) -> dict[str, Any]:
    updated = copy.deepcopy(case)
    turns = updated.get("turns")
    if isinstance(turns, list) and turns:
        for turn in turns:
            if isinstance(turn, dict) and turn.get("role", "user") == "user":
                turn["message"] = message
                return updated
        first = turns[0]
        if isinstance(first, dict):
            first["role"] = "user"
            first["message"] = message
            return updated
    input_data = updated.get("input")
    if isinstance(input_data, dict):
        input_data["message"] = message
        return updated
    updated["turns"] = [{"role": "user", "message": message}]
    return updated


def _case_summary(case: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "id": case.get("id", f"case-{index + 1}"),
        "message": _case_input_text(case),
        "index": index,
    }


def _new_case_from_input(case_id: str, message: str) -> dict[str, Any]:
    case_id = case_id.strip()
    if not case_id:
        raise HTTPException(status_code=400, detail="case id is required")
    return {
        "id": case_id,
        "title": "",
        "turns": [{"role": "user", "message": message}],
        "expect": {"non_empty_response": True},
    }


def _case_message_from_dataset(dataset_id: str, case_id: str) -> str:
    try:
        _, raw = _load_source_raw(dataset_id)
        cases = raw.get("cases") or []
    except HTTPException:
        try:
            path = _dataset_path_for(dataset_id)
        except HTTPException:
            return ""
        dataset = _read_json_safe(path) or {}
        cases = dataset.get("cases") or []
    for case in cases:
        if isinstance(case, dict) and case.get("id") == case_id:
            return _case_input_text(case)
    return ""


# ───────────────────────────────
# 活跃 run 注册表(内存)
# ───────────────────────────────
#
# server 重启会丢。重启后 progress 文件依然在磁盘上,SSE 仍能 tail,
# 但拿不到 subprocess 状态——按"找不到活跃句柄就认为已完成"处理。

_active_runs: dict[str, dict[str, Any]] = {}
_deleted_cases: dict[str, dict[str, Any]] = {}


def _progress_path_for(run_id: str) -> Path:
    return _PROGRESS_DIR / f"{run_id}.progress.jsonl"


def _delete_token(dataset_id: str, case_id: str) -> str:
    return f"{dataset_id}:{case_id}:{_now_str()}"


def _safe_run_label(value: str, *, max_len: int = 96) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)
    safe = safe.strip("._-")
    return (safe or "case")[:max_len]


# ───────────────────────────────
# 数据集
# ───────────────────────────────

def _iter_dataset_source_files() -> list[Path]:
    """顶层 *.txt/*.yaml/*.yml 数据集源文件。"""
    if not _DATASET_SOURCES_DIR.is_dir():
        return []
    return sorted(
        p for p in _DATASET_SOURCES_DIR.iterdir()
        if p.is_file() and p.suffix in {".txt", ".yaml", ".yml"}
    )


def _source_path_for(dataset_id: str, *, must_exist: bool = True) -> Path:
    _validate_dataset_id(dataset_id)
    for suffix in (".txt", ".yaml", ".yml"):
        path = _DATASET_SOURCES_DIR / f"{dataset_id}{suffix}"
        if path.is_file():
            return path
    path = _DATASET_SOURCES_DIR / f"{dataset_id}.txt"
    if must_exist:
        raise HTTPException(status_code=404, detail=f"dataset source not found: {dataset_id}")
    return path


def _load_source_raw(dataset_id: str) -> tuple[Path, dict[str, Any]]:
    path = _source_path_for(dataset_id)
    try:
        raw = load_dataset_source(path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return path, raw


def _write_source_raw(path: Path, raw: dict[str, Any]) -> None:
    try:
        build_dataset(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _dump_yaml(raw)
    header = "# E2E dataset source. Edit cases here; JSON is generated before runs.\n"
    if not text.startswith("#"):
        text = header + text
    path.write_text(text, encoding="utf-8")


def _compile_dataset_for_run(dataset_id: str) -> tuple[Path, dict[str, Any], Path | None]:
    """返回 runner 使用的 JSON 路径、dataset 数据与可选源文件路径。"""
    source_path: Path | None = None
    try:
        source_path = _source_path_for(dataset_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
    if source_path is not None:
        try:
            json_path = compile_dataset(source_path, out_dir=_DATASETS_DIR)
            data = _read_json_safe(json_path) or {}
            return json_path, data, source_path
        except (ValueError, FileNotFoundError, OSError, yaml.YAMLError) as exc:
            raise HTTPException(status_code=400, detail=f"compile failed: {exc}") from exc

    json_path = _dataset_path_for(dataset_id)
    data = _read_json_safe(json_path) or {}
    return json_path, data, None


def _compile_single_case_for_run(dataset_id: str, case_id: str) -> tuple[Path, dict[str, Any], Path | None]:
    return _compile_case_subset_for_run(dataset_id, [case_id])


def _compile_case_subset_for_run(dataset_id: str, case_ids: list[str]) -> tuple[Path, dict[str, Any], Path | None]:
    _, dataset, source_path = _compile_dataset_for_run(dataset_id)
    wanted = [case_id for case_id in case_ids if case_id]
    if not wanted:
        raise HTTPException(status_code=400, detail="case_ids must not be empty")

    cases = dataset.get("cases") or []
    by_id = {case.get("id"): case for case in cases if isinstance(case, dict)}
    missing = [case_id for case_id in wanted if case_id not in by_id]
    if missing:
        raise HTTPException(status_code=404, detail=f"case not found: {', '.join(missing)}")

    single = copy.deepcopy(dataset)
    # Preserve requested order so UI-selected subsets run in the same order.
    single["cases"] = [copy.deepcopy(by_id[case_id]) for case_id in wanted]
    suffix = wanted[0] if len(wanted) == 1 else f"subset-{len(wanted)}"
    single["suite_id"] = f"{dataset.get('suite_id', dataset_id)}--{suffix}"
    tmp_dir = _PROGRESS_DIR / "case_subset_datasets"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    safe_suffix = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in suffix)
    out_path = tmp_dir / f"{dataset_id}__{safe_suffix}__{_now_str()}.json"
    out_path.write_text(
        json.dumps(single, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return out_path, single, source_path


def _runner_script_for_dataset(dataset: dict[str, Any]) -> Path:
    suite_id = str(dataset.get("suite_id") or "")
    if suite_id.startswith("harness-"):
        return _REAL_RUNNER_SCRIPT
    session_plan = dataset.get("session_plan") or {}
    if session_plan.get("frontend_mode") == "learning":
        return _REAL_RUNNER_SCRIPT
    for case in dataset.get("cases") or []:
        setup = case.get("setup") or {}
        if setup.get("frontend_mode") == "learning":
            return _REAL_RUNNER_SCRIPT
    return _WEB_SEARCH_RUNNER_SCRIPT


def _compile_status(dataset_id: str, *, source_path: Path | None = None) -> dict[str, Any]:
    json_path = _DATASETS_DIR / f"{dataset_id}.json"
    if source_path is None:
        return {
            "state": "json_only" if json_path.is_file() else "missing",
            "json_path": str(json_path.relative_to(_REPO_ROOT)) if json_path.exists() else None,
        }
    if not json_path.is_file():
        return {
            "state": "source_only",
            "json_path": None,
        }
    source_mtime = source_path.stat().st_mtime
    json_mtime = json_path.stat().st_mtime
    return {
        "state": "current" if json_mtime >= source_mtime else "stale",
        "json_path": str(json_path.relative_to(_REPO_ROOT)),
    }


def _latest_run_summary_for_dataset(dataset_id: str) -> dict[str, Any] | None:
    dataset_dir = _RUNS_DIR / dataset_id
    latest_run_dir: Path | None = None
    if not dataset_dir.is_dir():
        latest = None
    else:
        latest: dict[str, Any] | None = None
        for run_dir in dataset_dir.iterdir():
            if run_dir.is_symlink() or not run_dir.is_dir():
                continue
            summary = _summarize_run(dataset_id, run_dir)
            if latest is None or (summary.get("started_at") or "") > (latest.get("started_at") or ""):
                latest = summary
                latest_run_dir = run_dir

    # Newly-started runs can exist in memory before the runner has created a
    # manifest directory. Include them so the dataset list reflects "running"
    # immediately after POST /runs.
    for run_id, info in _active_runs.items():
        if info.get("dataset_id") != dataset_id:
            continue
        summary = _summarize_active(run_id, info)
        if latest is None or (summary.get("started_at") or "") > (latest.get("started_at") or ""):
            latest = summary
            latest_run_dir = None
    if latest is not None and latest_run_dir is not None:
        latest["failed_case_ids"] = [
            str(item.get("case_id"))
            for item in _read_jsonl_safe(latest_run_dir / "results.jsonl")
            if (item.get("success") is False or item.get("verdict") == "fail") and item.get("case_id")
        ]
    return latest


def _dataset_item_from_path(path: Path, data: dict[str, Any], *, source_path: Path | None = None) -> dict[str, Any]:
    cases = data.get("cases") or []
    session_plan = data.get("session_plan") or {}
    dataset_id = path.stem
    return {
        "id": dataset_id,
        "path": str(path.relative_to(_REPO_ROOT)),
        "source_path": str(source_path.relative_to(_REPO_ROOT)) if source_path else None,
        "suite_id": data.get("suite_id", dataset_id),
        "case_count": len(cases),
        "description": data.get("description", ""),
        "session_title": session_plan.get("session_title", ""),
        "source_type": "txt" if source_path else "json",
        "compile_status": _compile_status(dataset_id, source_path=source_path),
        "last_run": _latest_run_summary_for_dataset(dataset_id),
    }


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
    _validate_dataset_id(dataset_id)
    path = _DATASETS_DIR / f"{dataset_id}.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"dataset not found: {dataset_id}")
    return path


class CaseInputRequest(BaseModel):
    id: Optional[str] = None
    message: str = ""


@router.get("/datasets")
async def list_datasets() -> dict[str, Any]:
    """列出所有数据集(仅元信息,不返回 cases 详情)。"""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source_path in _iter_dataset_source_files():
        try:
            raw = load_dataset_source(source_path)
            data = build_dataset(raw)
        except (ValueError, OSError, yaml.YAMLError) as exc:
            logger.warning("[eval] read source %s failed: %s", source_path, exc)
            continue
        items.append(_dataset_item_from_path(source_path, data, source_path=source_path))
        seen.add(source_path.stem)

    for path in _iter_dataset_files():
        if path.stem in seen:
            continue
        data = _read_json_safe(path)
        if not data:
            continue
        items.append(_dataset_item_from_path(path, data))
    return {"datasets": items}


@router.get("/datasets/{dataset_id}")
async def get_dataset(dataset_id: str) -> dict[str, Any]:
    """返回数据集详情与可编辑 case 内容。优先读取 txt/YAML 源。"""
    try:
        source_path, raw = _load_source_raw(dataset_id)
        dataset = build_dataset(raw)
        cases = []
        for index, case in enumerate(raw.get("cases") or []):
            if not isinstance(case, dict):
                continue
            cases.append(_case_summary(case, index))
        return {
            "dataset": _dataset_item_from_path(source_path, dataset, source_path=source_path),
            "cases": cases,
            "editable": True,
        }
    except HTTPException as exc:
        if exc.status_code != 404:
            raise

    path = _dataset_path_for(dataset_id)
    dataset = _read_json_safe(path) or {}
    cases = [
        _case_summary(case, index)
        for index, case in enumerate(dataset.get("cases") or [])
        if isinstance(case, dict)
    ]
    return {
        "dataset": _dataset_item_from_path(path, dataset),
        "cases": cases,
        "editable": False,
    }


@router.post("/datasets/{dataset_id}/cases")
async def create_dataset_case(dataset_id: str, req: CaseInputRequest) -> dict[str, Any]:
    path, raw = _load_source_raw(dataset_id)
    new_case = _new_case_from_input(req.id or "", req.message)
    cases = raw.setdefault("cases", [])
    if not isinstance(cases, list):
        raise HTTPException(status_code=400, detail="dataset cases must be a list")
    if any(isinstance(case, dict) and case.get("id") == new_case["id"] for case in cases):
        raise HTTPException(status_code=409, detail=f"case already exists: {new_case['id']}")
    cases.append(new_case)
    _write_source_raw(path, raw)
    compile_dataset(path, out_dir=_DATASETS_DIR)
    return {"case": _case_summary(new_case, len(cases) - 1)}


@router.put("/datasets/{dataset_id}/cases/{case_id}")
async def update_dataset_case(dataset_id: str, case_id: str, req: CaseInputRequest) -> dict[str, Any]:
    path, raw = _load_source_raw(dataset_id)
    if req.id is not None and req.id != case_id:
        raise HTTPException(status_code=400, detail="request id must match URL case_id")
    cases = raw.get("cases") or []
    for index, case in enumerate(cases):
        if isinstance(case, dict) and case.get("id") == case_id:
            updated_case = _set_case_input_text(case, req.message)
            cases[index] = updated_case
            _write_source_raw(path, raw)
            compile_dataset(path, out_dir=_DATASETS_DIR)
            return {"case": _case_summary(updated_case, index)}
    raise HTTPException(status_code=404, detail=f"case not found: {case_id}")


@router.delete("/datasets/{dataset_id}/cases/{case_id}")
async def delete_dataset_case(dataset_id: str, case_id: str) -> dict[str, Any]:
    path, raw = _load_source_raw(dataset_id)
    cases = raw.get("cases") or []
    for index, case in enumerate(cases):
        if isinstance(case, dict) and case.get("id") == case_id:
            deleted = copy.deepcopy(case)
            token = _delete_token(dataset_id, case_id)
            _deleted_cases[token] = {
                "dataset_id": dataset_id,
                "case_id": case_id,
                "case": deleted,
                "index": index,
            }
            del cases[index]
            _write_source_raw(path, raw)
            compile_dataset(path, out_dir=_DATASETS_DIR)
            return {"deleted": case_id, "undo_token": token}
    raise HTTPException(status_code=404, detail=f"case not found: {case_id}")


@router.post("/datasets/{dataset_id}/cases/undo-delete")
async def undo_delete_dataset_case(dataset_id: str, req: dict[str, str]) -> dict[str, Any]:
    token = req.get("undo_token", "")
    deleted = _deleted_cases.pop(token, None)
    if not deleted or deleted.get("dataset_id") != dataset_id:
        raise HTTPException(status_code=404, detail="deleted case snapshot not found")

    path, raw = _load_source_raw(dataset_id)
    cases = raw.setdefault("cases", [])
    if not isinstance(cases, list):
        raise HTTPException(status_code=400, detail="dataset cases must be a list")
    case = copy.deepcopy(deleted["case"])
    case_id = case.get("id")
    if any(isinstance(existing, dict) and existing.get("id") == case_id for existing in cases):
        raise HTTPException(status_code=409, detail=f"case already exists: {case_id}")
    index = min(max(int(deleted.get("index", len(cases))), 0), len(cases))
    cases.insert(index, case)
    _write_source_raw(path, raw)
    compile_dataset(path, out_dir=_DATASETS_DIR)
    return {"case": _case_summary(case, index)}


@router.post("/datasets/{dataset_id}/compile")
async def compile_dataset_source(dataset_id: str) -> dict[str, Any]:
    source_path = _source_path_for(dataset_id)
    try:
        out_path = compile_dataset(source_path, out_dir=_DATASETS_DIR)
        data = _read_json_safe(out_path) or {}
    except (ValueError, OSError, yaml.YAMLError) as exc:
        raise HTTPException(status_code=400, detail=f"compile failed: {exc}") from exc
    return {
        "ok": True,
        "path": str(out_path.relative_to(_REPO_ROOT)),
        "case_count": len(data.get("cases") or []),
    }


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
        "skipped": summary.get("skipped", 0),
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
        "skipped": 0,
        "pass_rate": None,
        "status": "running",
    }


def _persist_startup_failed_run(run_id: str, info: dict[str, Any], reason: dict[str, Any]) -> dict[str, Any]:
    """Runner 在写 run_started 前退出时,落一份 failed run 供历史面板展示。"""
    dataset_id = str(info.get("dataset_id") or "unknown")
    run_dir = _RUNS_DIR / dataset_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    started_at = str(info.get("started_at") or _now_str())
    ended_at = _now_str()
    result_case_ids = [
        str(case_id)
        for case_id in (info.get("result_case_ids") or info.get("case_ids") or [])
        if case_id
    ]
    total_cases = int(info.get("total_cases") or len(result_case_ids) or 1)
    while len(result_case_ids) < total_cases:
        result_case_ids.append(f"case-{len(result_case_ids) + 1}")

    log_path = info.get("log_path")
    log_rel = None
    if isinstance(log_path, Path):
        try:
            log_rel = str(log_path.relative_to(_REPO_ROOT))
        except ValueError:
            log_rel = str(log_path)

    error = (
        "runner exited before writing progress "
        f"(returncode={reason.get('returncode')}); see {log_rel or 'runner log'}"
    )
    manifest = {
        "run_id": run_id,
        "suite_id": dataset_id,
        "start_time": started_at,
        "end_time": ended_at,
        "session_id": None,
        "mode": "chat",
        "startup_failed": True,
        "base_url": info.get("base_url"),
        "runner_script": info.get("runner_script"),
        "log_path": log_rel,
    }
    summary = {
        "total_cases": total_cases,
        "passed": 0,
        "failed": total_cases,
        "skipped": 0,
        "pass_rate": 0.0,
        "total_duration_ms": 0,
        "startup_failed": True,
    }
    results = [
        {
            "case_id": case_id,
            "title": "Runner startup failed",
            "success": False,
            "verdict": "fail",
            "error": error,
            "response_text": "",
            "tool_calls_count": 0,
            "duration_ms": 0,
        }
        for case_id in result_case_ids[:total_cases]
    ]

    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    with open(run_dir / "results.jsonl", "w", encoding="utf-8") as f:
        for row in results:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return {
        "run_dir": run_dir,
        "summary": summary,
        "results": results,
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
    """单个 case 的 request + response 全文 + L1 事件 + unresolved 证据。

    返回字段：
        request:             cases/<id>/request.json
        response:            cases/<id>/response.json（含 fail_reasons 等判定结果）
        events:              cases/<id>/events.jsonl（按 seq 顺序）
        unresolved_failures: cases/<id>/unresolved_failures.jsonl
        result:              results.jsonl 里该 case 的行（含 verdict/skipped_reason/state_final）

    任一文件缺失走 [] / null，不抛——刚跑完或 case 没产物时让 UI 静默降级。
    """
    run_dir = _find_run_dir(run_id)
    case_dir = run_dir / "cases" / case_id
    if not case_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"case not found: {case_id}")
    request = _read_json_safe(case_dir / "request.json")
    if isinstance(request, dict) and not request.get("user_message"):
        message = _case_message_from_dataset(run_dir.parent.name, case_id)
        if message:
            request["user_message"] = message
            request["turns"] = [message]
    # results.jsonl 是 run 级聚合 — 取本 case 那一行做 verdict / state_final 补充
    result_row: dict[str, Any] | None = None
    for row in _read_jsonl_safe(run_dir / "results.jsonl"):
        if row.get("case_id") == case_id:
            result_row = row
            break
    return {
        "run_id": run_id,
        "case_id": case_id,
        "request": request,
        "response": _read_json_safe(case_dir / "response.json"),
        "events": _read_jsonl_safe(case_dir / "events.jsonl"),
        "unresolved_failures": _read_jsonl_safe(case_dir / "unresolved_failures.jsonl"),
        "result": result_row,
    }


@router.delete("/runs/{run_id}")
async def delete_run(run_id: str) -> dict[str, Any]:
    """删除一次运行的全部证据：
        - .test_artifacts/e2e_real_runs/<dataset_id>/<run_id>/
        - .test_artifacts/eval_console/<run_id>.progress.jsonl
        - .test_artifacts/eval_console/logs/<run_id>.log
        - 若 dataset 目录下 latest 软链指向被删的 run，一并清掉

    若 subprocess 还在跑(returncode is None)拒绝(409)。已退出但 _active_runs
    没被 SSE tail 清理的情况:DELETE 顺手把表清干净。
    """
    active_info = _active_runs.get(run_id)
    if active_info is not None:
        proc = active_info.get("process")
        if proc is not None and proc.returncode is None:
            raise HTTPException(
                status_code=409,
                detail=f"run is still active: {run_id}. wait for it to finish first.",
            )
        # 已退出但表没清(没人 connect SSE),DELETE 顺带清表 + 关日志句柄。
        info = _active_runs.pop(run_id, None)
        if info is not None:
            log_fp = info.get("log_fp")
            if log_fp is not None:
                try:
                    log_fp.close()
                except Exception:
                    pass

    run_dir = _find_run_dir(run_id)  # 找不到会 raise 404
    dataset_dir = run_dir.parent

    # 主目录
    shutil.rmtree(run_dir, ignore_errors=True)

    # 同 dataset 下 latest 软链如果指向已删 run，清理
    latest_link = dataset_dir / "latest"
    if latest_link.is_symlink():
        try:
            target = (dataset_dir / os.readlink(latest_link)).resolve()
            if not target.exists() or target.name == run_id:
                latest_link.unlink()
        except OSError:
            pass

    # 进度文件 + runner 日志（best-effort，清理失败不影响主结果）
    progress_path = _progress_path_for(run_id)
    try:
        if progress_path.is_file():
            progress_path.unlink()
    except OSError:
        logger.warning("[eval] failed to remove progress file: %s", progress_path)
    log_path = _REPO_ROOT / ".test_artifacts" / "eval_console" / "logs" / f"{run_id}.log"
    try:
        if log_path.is_file():
            log_path.unlink()
    except OSError:
        logger.warning("[eval] failed to remove runner log: %s", log_path)

    logger.info("[eval] deleted run %s (dataset=%s)", run_id, dataset_dir.name)
    return {"deleted": run_id}


# ───────────────────────────────
# 启动 run + SSE 进度流
# ───────────────────────────────

class StartRunRequest(BaseModel):
    dataset_id: str
    case_id: Optional[str] = None
    case_ids: Optional[list[str]] = None
    base_url: Optional[str] = None  # 默认走 server 自己
    dry_run: bool = False


@router.post("/runs")
async def start_run(req: StartRunRequest, request: Request) -> dict[str, Any]:
    """spawn runner subprocess。立刻返回 run_id,不等 subprocess 启动完成。"""
    if req.case_ids is not None and len(req.case_ids) == 0:
        raise HTTPException(status_code=400, detail="case_ids must not be empty")
    case_ids = req.case_ids if req.case_ids is not None else ([req.case_id] if req.case_id else [])
    if case_ids:
        if len(case_ids) == 1:
            dataset_path, dataset_data, _ = _compile_single_case_for_run(req.dataset_id, case_ids[0])
        else:
            dataset_path, dataset_data, _ = _compile_case_subset_for_run(req.dataset_id, case_ids)
    else:
        dataset_path, dataset_data, _ = _compile_dataset_for_run(req.dataset_id)

    if len(case_ids) == 1:
        run_suffix = f"{req.dataset_id}_{_safe_run_label(case_ids[0])}"
    elif len(case_ids) > 1:
        run_suffix = f"{req.dataset_id}_subset-{len(case_ids)}"
    else:
        run_suffix = req.dataset_id
    run_id = f"{_now_str()}_{run_suffix}_console"
    progress_path = _progress_path_for(run_id)
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    # 触一下文件,确保 SSE 端立刻能 open。
    progress_path.touch(exist_ok=True)

    output_dir = _RUNS_DIR / req.dataset_id
    output_dir.mkdir(parents=True, exist_ok=True)

    # 数据集的 case 数,SSE 端在 run_started 事件前就能展示总数。
    total_cases = len(dataset_data.get("cases") or [])

    base_url = req.base_url or os.environ.get("EVAL_RUNNER_BASE_URL") or str(request.base_url).rstrip("/")
    runner_script = _runner_script_for_dataset(dataset_data)

    cmd = [
        sys.executable,
        str(runner_script),
        "--dataset", str(dataset_path),
        "--output", str(output_dir),
        "--base-url", base_url,
        "--progress-jsonl", str(progress_path),
        "--run-id", run_id,
    ]
    if req.dry_run:
        cmd.append("--dry-run")
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
        "case_id": case_ids[0] if len(case_ids) == 1 else None,
        "case_ids": case_ids,
        "result_case_ids": [
            str(case.get("id", f"case-{index + 1}"))
            for index, case in enumerate(dataset_data.get("cases") or [])
            if isinstance(case, dict)
        ],
        "started_at": _now_str(),
        "base_url": base_url,
        "total_cases": total_cases,
        "runner_script": str(runner_script.relative_to(_REPO_ROOT)),
    }

    return {
        "run_id": run_id,
        "dataset_id": req.dataset_id,
        "case_id": case_ids[0] if len(case_ids) == 1 else None,
        "case_ids": case_ids,
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
                reason = {"reason": "process exited", "returncode": proc.returncode}
                if active_info is not None:
                    failed = _persist_startup_failed_run(run_id, active_info, reason)
                    for index, row in enumerate(failed["results"]):
                        event = {
                            "event": "case_done",
                            "index": index,
                            "case_id": row.get("case_id"),
                            "title": row.get("title"),
                            "success": False,
                            "verdict": "fail",
                            "error": row.get("error"),
                            "duration_ms": row.get("duration_ms", 0),
                            "tool_calls_count": 0,
                            "ts": _now_str(),
                        }
                        yield f"event: case_done\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    finished_event = {
                        "event": "run_finished",
                        "run_id": run_id,
                        "summary": failed["summary"],
                        "run_dir": str(failed["run_dir"].relative_to(_REPO_ROOT)),
                        "ts": _now_str(),
                    }
                    yield f"event: run_finished\ndata: {json.dumps(finished_event, ensure_ascii=False)}\n\n"
                yield f"event: done\ndata: {json.dumps(reason)}\n\n"
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
