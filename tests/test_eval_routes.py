"""E2E 评测控制台 API(/api/eval)的只读路径单元测试。

只覆盖文件扫读这一类纯 IO 行为(数据集列表 / 运行历史 / 单 run / 单 case
+ 404 / 校验路径)。subprocess + SSE 部分由手测在浏览器验证,这里不模拟。

测试通过 monkeypatch 把 eval_routes 模块级目录常量重指向 tmp_path,然后用
FastAPI TestClient 直接打 router。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.web import eval_routes


@pytest.fixture
def fake_layout(tmp_path: Path, monkeypatch):
    """搭一份与生产对齐的 datasets/runs 目录骨架,返回 root。"""
    datasets_dir = tmp_path / "tests" / "e2e" / "real_datasets"
    runs_dir = tmp_path / ".test_artifacts" / "e2e_real_runs"
    progress_dir = tmp_path / ".test_artifacts" / "eval_console"
    datasets_dir.mkdir(parents=True)
    runs_dir.mkdir(parents=True)
    progress_dir.mkdir(parents=True)

    monkeypatch.setattr(eval_routes, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(eval_routes, "_DATASETS_DIR", datasets_dir)
    monkeypatch.setattr(eval_routes, "_RUNS_DIR", runs_dir)
    monkeypatch.setattr(eval_routes, "_PROGRESS_DIR", progress_dir)
    monkeypatch.setattr(eval_routes, "_active_runs", {}, raising=False)

    return {
        "root": tmp_path,
        "datasets_dir": datasets_dir,
        "runs_dir": runs_dir,
        "progress_dir": progress_dir,
    }


@pytest.fixture
def client(fake_layout):
    app = FastAPI()
    app.include_router(eval_routes.router)
    return TestClient(app)


def _write_dataset(datasets_dir: Path, dataset_id: str, *, case_count: int = 2) -> Path:
    """造一份最小数据集 JSON。"""
    cases = [
        {"id": f"case-{i+1}", "title": f"用例 {i+1}", "tool_chain": ["web_search"]}
        for i in range(case_count)
    ]
    payload = {
        "suite_id": dataset_id,
        "description": f"{dataset_id} 的描述",
        "session_plan": {"session_title": f"{dataset_id} 会话"},
        "cases": cases,
    }
    path = datasets_dir / f"{dataset_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _write_run(
    runs_dir: Path,
    dataset_id: str,
    run_id: str,
    *,
    passed=1,
    failed=0,
    start_time: str = "2026-05-27T010000Z",
    end_time: str = "2026-05-27T010500Z",
) -> Path:
    """造一个已完成 run 的产物结构。"""
    run_dir = runs_dir / dataset_id / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({
        "run_id": run_id,
        "suite_id": dataset_id,
        "start_time": start_time,
        "end_time": end_time,
        "session_id": "sess-abc",
        "mode": "chat",
    }, ensure_ascii=False), encoding="utf-8")
    (run_dir / "summary.json").write_text(json.dumps({
        "total_cases": passed + failed,
        "passed": passed,
        "failed": failed,
        "pass_rate": round(passed / max(passed + failed, 1), 4),
        "total_duration_ms": 1234,
    }, ensure_ascii=False), encoding="utf-8")
    with open(run_dir / "results.jsonl", "w", encoding="utf-8") as f:
        for i in range(passed):
            f.write(json.dumps({
                "case_id": f"case-{i+1}", "title": f"用例 {i+1}",
                "success": True, "error": None,
                "response_text": "ok", "tool_calls_count": 1, "duration_ms": 600,
            }, ensure_ascii=False) + "\n")
        for i in range(failed):
            f.write(json.dumps({
                "case_id": f"fail-{i+1}", "title": f"失败 {i+1}",
                "success": False, "error": "boom",
                "response_text": "", "tool_calls_count": 0, "duration_ms": 200,
            }, ensure_ascii=False) + "\n")
    return run_dir


def _write_case(run_dir: Path, case_id: str, *, user_message="hello", response_text="world"):
    case_dir = run_dir / "cases" / case_id
    case_dir.mkdir(parents=True)
    (case_dir / "request.json").write_text(json.dumps({
        "case_id": case_id,
        "title": "用例 X",
        "user_message": user_message,
    }, ensure_ascii=False), encoding="utf-8")
    (case_dir / "response.json").write_text(json.dumps({
        "success": True, "error": None,
        "response_text": response_text,
        "tool_calls": [{"name": "web_search", "args": {"q": "x"}}],
    }, ensure_ascii=False), encoding="utf-8")


# ─────────────────────────────
# GET /api/eval/datasets
# ─────────────────────────────

def test_list_datasets_empty(client):
    resp = client.get("/api/eval/datasets")
    assert resp.status_code == 200
    assert resp.json() == {"datasets": []}


def test_list_datasets_lists_top_level_json_only(client, fake_layout):
    _write_dataset(fake_layout["datasets_dir"], "first5", case_count=3)
    _write_dataset(fake_layout["datasets_dir"], "deep", case_count=1)
    # 子目录里的 JSON 不应被列出
    sub = fake_layout["datasets_dir"] / "workspace"
    sub.mkdir()
    (sub / "ignored.json").write_text("{}", encoding="utf-8")

    resp = client.get("/api/eval/datasets")
    assert resp.status_code == 200
    items = resp.json()["datasets"]
    ids = sorted(it["id"] for it in items)
    assert ids == ["deep", "first5"]
    first5 = next(it for it in items if it["id"] == "first5")
    assert first5["case_count"] == 3
    assert first5["session_title"] == "first5 会话"
    assert first5["suite_id"] == "first5"


# ─────────────────────────────
# GET /api/eval/runs
# ─────────────────────────────

def test_list_runs_empty(client):
    resp = client.get("/api/eval/runs")
    assert resp.status_code == 200
    assert resp.json() == {"runs": []}


def test_list_runs_sorted_desc_by_started_at(client, fake_layout):
    _write_run(
        fake_layout["runs_dir"], "first5", "2026-05-27T010000Z_first5_console",
        passed=2, failed=0,
        start_time="2026-05-27T010000Z",
    )
    # 较早 + 失败
    early = _write_run(
        fake_layout["runs_dir"], "deep", "2026-05-26T010000Z_deep_console",
        passed=1, failed=1,
        start_time="2026-05-26T010000Z",
    )
    # latest 软链不应被当成 run
    try:
        (fake_layout["runs_dir"] / "deep" / "latest").symlink_to(early, target_is_directory=True)
    except (OSError, NotImplementedError):
        pass

    resp = client.get("/api/eval/runs")
    assert resp.status_code == 200
    runs = resp.json()["runs"]
    assert len(runs) == 2
    assert runs[0]["dataset_id"] == "first5"  # 较新在前
    assert runs[0]["status"] == "done"
    assert runs[1]["dataset_id"] == "deep"
    assert runs[1]["failed"] == 1


def test_list_runs_includes_active_without_manifest(client, fake_layout):
    eval_routes._active_runs["live-run-x"] = {
        "dataset_id": "first5",
        "started_at": "2026-05-27T020000Z",
        "total_cases": 5,
    }
    try:
        resp = client.get("/api/eval/runs")
        assert resp.status_code == 200
        runs = resp.json()["runs"]
        assert any(r["run_id"] == "live-run-x" and r["status"] == "running" for r in runs)
    finally:
        eval_routes._active_runs.pop("live-run-x", None)


# ─────────────────────────────
# GET /api/eval/runs/{id}
# ─────────────────────────────

def test_get_run_returns_manifest_summary_and_results(client, fake_layout):
    _write_run(fake_layout["runs_dir"], "first5", "run-1", passed=1, failed=1)
    resp = client.get("/api/eval/runs/run-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["run_id"] == "run-1"
    assert data["dataset_id"] == "first5"
    assert data["manifest"]["session_id"] == "sess-abc"
    assert data["summary"]["passed"] == 1
    assert len(data["results"]) == 2


def test_get_run_404(client):
    resp = client.get("/api/eval/runs/missing-run-id")
    assert resp.status_code == 404


# ─────────────────────────────
# GET /api/eval/runs/{id}/cases/{cid}
# ─────────────────────────────

def test_get_case_returns_request_and_response(client, fake_layout):
    run_dir = _write_run(fake_layout["runs_dir"], "first5", "run-2")
    _write_case(run_dir, "case-1", user_message="问点东西", response_text="答案")

    resp = client.get("/api/eval/runs/run-2/cases/case-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["request"]["user_message"] == "问点东西"
    assert data["response"]["response_text"] == "答案"
    assert data["response"]["tool_calls"][0]["name"] == "web_search"


def test_get_case_404_on_missing_case(client, fake_layout):
    _write_run(fake_layout["runs_dir"], "first5", "run-3")
    resp = client.get("/api/eval/runs/run-3/cases/nope")
    assert resp.status_code == 404


# ─────────────────────────────
# POST /api/eval/runs(只校验拒绝路径,不真起 subprocess)
# ─────────────────────────────

@pytest.mark.parametrize("bad_id", ["../etc", "a/b", "a\\b", ".hidden"])
def test_post_run_rejects_invalid_dataset_id(client, bad_id):
    resp = client.post("/api/eval/runs", json={"dataset_id": bad_id})
    assert resp.status_code == 400


def test_post_run_404_when_dataset_missing(client):
    resp = client.post("/api/eval/runs", json={"dataset_id": "ghost"})
    assert resp.status_code == 404


# ─────────────────────────────
# GET /api/eval/runs/{id}/stream
# ─────────────────────────────

def test_stream_404_when_no_progress_and_not_active(client):
    resp = client.get("/api/eval/runs/ghost-id/stream")
    assert resp.status_code == 404
