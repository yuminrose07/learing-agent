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
    dataset_sources_dir = tmp_path / "tests" / "e2e" / "real_datasets_src"
    runs_dir = tmp_path / ".test_artifacts" / "e2e_real_runs"
    progress_dir = tmp_path / ".test_artifacts" / "eval_console"
    datasets_dir.mkdir(parents=True)
    dataset_sources_dir.mkdir(parents=True)
    runs_dir.mkdir(parents=True)
    progress_dir.mkdir(parents=True)

    monkeypatch.setattr(eval_routes, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(eval_routes, "_DATASETS_DIR", datasets_dir)
    monkeypatch.setattr(eval_routes, "_DATASET_SOURCES_DIR", dataset_sources_dir)
    monkeypatch.setattr(eval_routes, "_RUNS_DIR", runs_dir)
    monkeypatch.setattr(eval_routes, "_PROGRESS_DIR", progress_dir)
    monkeypatch.setattr(eval_routes, "_active_runs", {}, raising=False)
    monkeypatch.setattr(eval_routes, "_deleted_cases", {}, raising=False)

    return {
        "root": tmp_path,
        "datasets_dir": datasets_dir,
        "dataset_sources_dir": dataset_sources_dir,
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


def _write_dataset_source(sources_dir: Path, dataset_id: str) -> Path:
    path = sources_dir / f"{dataset_id}.txt"
    path.write_text(
        """
suite_id: editable-suite
description: 可编辑数据集
session_plan:
  frontend_mode: chat
cases:
  - id: case-a
    title: A
    input:
      message: hello
    tool_chain: [web_search]
""".strip() + "\n",
        encoding="utf-8",
    )
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


def test_list_datasets_prefers_txt_source_over_generated_json(client, fake_layout):
    _write_dataset(fake_layout["datasets_dir"], "editable-suite", case_count=9)
    _write_dataset_source(fake_layout["dataset_sources_dir"], "editable-suite")

    resp = client.get("/api/eval/datasets")
    assert resp.status_code == 200
    items = resp.json()["datasets"]
    assert len(items) == 1
    assert items[0]["id"] == "editable-suite"
    assert items[0]["source_type"] == "txt"
    assert items[0]["case_count"] == 1
    assert items[0]["compile_status"]["state"] == "stale"
    assert items[0]["last_run"] is None


def test_list_datasets_includes_latest_run_summary_and_failed_cases(client, fake_layout):
    _write_dataset(fake_layout["datasets_dir"], "first5", case_count=2)
    _write_run(
        fake_layout["runs_dir"], "first5", "old-run",
        passed=1, failed=0,
        start_time="2026-05-26T010000Z",
    )
    _write_run(
        fake_layout["runs_dir"], "first5", "new-run",
        passed=1, failed=1,
        start_time="2026-05-27T010000Z",
    )

    resp = client.get("/api/eval/datasets")
    assert resp.status_code == 200
    item = resp.json()["datasets"][0]
    assert item["last_run"]["run_id"] == "new-run"
    assert item["last_run"]["passed"] == 1
    assert item["last_run"]["failed"] == 1
    assert item["last_run"]["failed_case_ids"] == ["fail-1"]


def test_list_datasets_compile_status_source_only(client, fake_layout):
    _write_dataset_source(fake_layout["dataset_sources_dir"], "editable-suite")

    resp = client.get("/api/eval/datasets")
    assert resp.status_code == 200
    item = resp.json()["datasets"][0]
    assert item["compile_status"]["state"] == "source_only"


def test_get_dataset_source_returns_editable_cases(client, fake_layout):
    _write_dataset_source(fake_layout["dataset_sources_dir"], "editable-suite")

    resp = client.get("/api/eval/datasets/editable-suite")
    assert resp.status_code == 200
    data = resp.json()
    assert data["editable"] is True
    assert data["dataset"]["source_type"] == "txt"
    assert data["cases"][0]["id"] == "case-a"
    assert data["cases"][0]["message"] == "hello"


def test_compile_dataset_adds_legacy_input_message_for_turns(fake_layout):
    src = fake_layout["dataset_sources_dir"] / "turns-suite.txt"
    src.write_text(
        """
suite_id: turns-suite
cases:
  - id: case-a
    turns:
      - role: user
        message: hello
""".strip() + "\n",
        encoding="utf-8",
    )

    out = eval_routes.compile_dataset(src, out_dir=fake_layout["datasets_dir"])
    compiled = json.loads(out.read_text(encoding="utf-8"))
    case = compiled["cases"][0]
    assert case["turns"][0]["message"] == "hello"
    assert case["input"]["message"] == "hello"


def test_compile_dataset_preserves_txt_eval_metadata(fake_layout):
    src = fake_layout["dataset_sources_dir"] / "metadata-suite.txt"
    src.write_text(
        """
suite_id: metadata-suite
cases:
  - id: case-a
    title: A
    description: metadata should survive compile
    source: external_search
    tags: [web_search]
    tool_chain: [web_search, web_fetch]
    evidence_required: [request.json, response.json]
    severity: high
    turns:
      - role: user
        message: hello
""".strip() + "\n",
        encoding="utf-8",
    )

    out = eval_routes.compile_dataset(src, out_dir=fake_layout["datasets_dir"])
    compiled = json.loads(out.read_text(encoding="utf-8"))
    case = compiled["cases"][0]
    assert case["description"] == "metadata should survive compile"
    assert case["source"] == "external_search"
    assert case["tool_chain"] == ["web_search", "web_fetch"]
    assert case["evidence_required"] == ["request.json", "response.json"]


def test_dataset_case_crud_and_compile(client, fake_layout):
    _write_dataset_source(fake_layout["dataset_sources_dir"], "editable-suite")

    create = client.post("/api/eval/datasets/editable-suite/cases", json={
        "id": "case-b",
        "message": "hi",
    })
    assert create.status_code == 200
    assert create.json()["case"]["id"] == "case-b"
    assert create.json()["case"]["message"] == "hi"

    update = client.put("/api/eval/datasets/editable-suite/cases/case-b", json={
        "id": "case-b",
        "message": "changed",
    })
    assert update.status_code == 200
    assert update.json()["case"]["message"] == "changed"

    compile_resp = client.post("/api/eval/datasets/editable-suite/compile")
    assert compile_resp.status_code == 200
    assert compile_resp.json()["case_count"] == 2

    delete = client.delete("/api/eval/datasets/editable-suite/cases/case-b")
    assert delete.status_code == 200
    undo_token = delete.json()["undo_token"]
    detail = client.get("/api/eval/datasets/editable-suite").json()
    assert [case["id"] for case in detail["cases"]] == ["case-a"]

    undo = client.post("/api/eval/datasets/editable-suite/cases/undo-delete", json={
        "undo_token": undo_token,
    })
    assert undo.status_code == 200
    detail = client.get("/api/eval/datasets/editable-suite").json()
    assert [case["id"] for case in detail["cases"]] == ["case-a", "case-b"]


def test_start_run_defaults_base_url_to_eval_request_origin(client, fake_layout, monkeypatch):
    _write_dataset_source(fake_layout["dataset_sources_dir"], "editable-suite")
    runner = fake_layout["root"] / "tests" / "e2e" / "frontend_real_runner_web_search.py"
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_text("# fake runner\n", encoding="utf-8")
    monkeypatch.setattr(eval_routes, "_WEB_SEARCH_RUNNER_SCRIPT", runner)
    monkeypatch.setattr(eval_routes, "_REAL_RUNNER_SCRIPT", runner)
    captured = {}

    class FakeProcess:
        returncode = None

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(eval_routes.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    resp = client.post("/api/eval/runs", json={
        "dataset_id": "editable-suite",
        "case_id": "case-a",
    })
    assert resp.status_code == 200
    cmd = captured["cmd"]
    assert cmd[cmd.index("--base-url") + 1] == "http://testserver"

    run_id = resp.json()["run_id"]
    info = eval_routes._active_runs.pop(run_id)
    info["log_fp"].close()


def test_startup_failed_run_is_persisted_for_history(fake_layout):
    log_path = fake_layout["progress_dir"] / "logs" / "run-startup-failed.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("boom", encoding="utf-8")
    info = {
        "dataset_id": "editable-suite",
        "started_at": "2026-05-29T000000Z",
        "total_cases": 1,
        "result_case_ids": ["case-a"],
        "base_url": "http://testserver",
        "runner_script": "tests/e2e/frontend_real_runner_web_search.py",
        "log_path": log_path,
    }

    eval_routes._persist_startup_failed_run(
        "run-startup-failed",
        info,
        {"reason": "process exited", "returncode": 1},
    )

    run_dir = fake_layout["runs_dir"] / "editable-suite" / "run-startup-failed"
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    result = json.loads((run_dir / "results.jsonl").read_text(encoding="utf-8").strip())
    assert summary["failed"] == 1
    assert summary["startup_failed"] is True
    assert manifest["base_url"] == "http://testserver"
    assert result["case_id"] == "case-a"
    assert result["success"] is False
    assert "runner exited before writing progress" in result["error"]


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


def test_get_case_backfills_empty_request_from_dataset_source(client, fake_layout):
    _write_dataset_source(fake_layout["dataset_sources_dir"], "editable-suite")
    run_dir = _write_run(fake_layout["runs_dir"], "editable-suite", "run-empty-request")
    _write_case(run_dir, "case-a", user_message="", response_text="跳过")

    resp = client.get("/api/eval/runs/run-empty-request/cases/case-a")
    assert resp.status_code == 200
    data = resp.json()
    assert data["request"]["user_message"] == "hello"
    assert data["request"]["turns"] == ["hello"]


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
