# 2026-05-30 Phase 1A hardening: real runner 与 forge-state 数据集补齐

## 运行环境 / 时间戳

- Worktree: `/Users/roseannk/my-agent/.claude/worktrees/learning-phase-1a-hardening`
- Branch: `feature/learning-phase-1a-hardening`
- 时间: `2026-05-30T17:52:18Z`
- Provider: 未调用外部 LLM/provider；本提交只补测试数据与 runner

## 背景

doc1 裂缝 1 要求使用：

- `tests/e2e/real_runner.py`
- `tests/e2e/real_datasets/learning-mode-phase-1a-forge-state-real.json`
- `tests/e2e/real_baselines/learning-mode-phase-1a-forge-state-real.baseline.json`

但从最新 `origin/main` 切出的本 feature 工作树中，上述三项不存在。主工作树中存在同名 dataset/baseline 本地文件，但未落在当前分支可追踪历史里。

## 变更

- 新增 Phase 1A forge-state real dataset，保持 `suite_id=learning-mode-phase-1a-forge-state-real`、`dataset_version=2026-05-27-v1`、4 cases。
- 新增初始 baseline，状态保持 `not_yet_executed`，不伪造 pass。
- 新增窄版 `tests/e2e/real_runner.py`：
  - 支持 `--dataset` / `--output` / `--base-url` / `--data-dir` / `--dry-run`
  - 只覆盖当前 Phase 1A forge-state suite
  - 通过真实 Web API 驱动 `/learning-units`、`/sessions/{id}/chat`、`/learning-units/{id}`、`/sessions/{id}/events`
  - 产出 `manifest.json`、`summary.json`、`results.jsonl`、`cases/<case_id>/{request.json,response.json,session.json,learning_unit.json,events.jsonl}`

## 四层职责分类

| 文件 | 层级 | 说明 |
| --- | --- | --- |
| `tests/e2e/real_runner.py` | 测试基础设施 | 通过 Interface 层 HTTP API 采集真实 evidence，不进入产品运行时 |
| `tests/e2e/real_datasets/learning-mode-phase-1a-forge-state-real.json` | 测试数据 | Phase 1A 实跑输入事实 |
| `tests/e2e/real_baselines/learning-mode-phase-1a-forge-state-real.baseline.json` | 测试数据 | baseline 初始声明，等待后续单独实跑回填 |

Product / Runtime / Interface / Infrastructure 产品代码本提交未改。

## 验证

- `python3 -m py_compile tests/e2e/real_runner.py tests/e2e/run_phase1a_forge_e2e.py`
- `python3 tests/e2e/real_runner.py --dataset tests/e2e/real_datasets/learning-mode-phase-1a-forge-state-real.json --output /tmp/la-phase1a-dry --dry-run`

## 后续

下一提交必须真实运行该 dataset，并用 `summary.json` 回填 baseline。该实跑回填提交的 commit message 必须包含字面量 `baseline_version=`。
