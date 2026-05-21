# 2026-05-21 新建 AGENTS.git.md（Git 工作流）

## 背景

历史分支治理不规范：存在 51082 行的炸弹 commit `c7ac46a`、`v1` / `fix v2` ×2 / `reday compact` 等无效 message、`.env` 入库（API key 已泄漏）、`.learning_agent_data/` 与 `.observability/` 长期被跟踪、`codex/*` AI 草稿分支直接进入正式流程、栈式分支基于污染基底拉新分支无法独立合并。

## 改动

新增 `AGENTS.git.md`，七节：

1. 分支模型（`main`/`release` 长期分支 + `<type>/<desc>` 工作分支命名）
2. 提交规则（Conventional Commits + 单 commit 单职责）
3. `.gitignore` 基线（Python / secrets / IDE / runtime 必填项）
4. PR Checklist（分支命名、commit 规模、文档留痕、本地验证）
5. 常见操作 Cheatsheet
6. 禁止事项（push main、`--force`、`.env`、`--no-verify`）
7. 本仓库历史教训（炸弹 commit、无效 message、`.env` 泄漏、运行时数据入库、AI 前缀混用、栈式污染）

## 配套清理动作（同期完成）

- `chore/cleanup` 已 fast-forward 到 `main`（`71a68b1` → `0753b84`），新 `main` 含 `.gitignore` + `pytest` 依赖。
- 基于干净 `main` 重建 `feat/compaction-pipeline`，5 个有结构的 commit 替代原 `c7ac46a` 一个 51082 行炸弹 commit。
- 污染分支待删除清单：`release` / `codex/full-compact-slact-implementation` / `codex/fix-compaction-summary-pipeline` / `test/compaction-dataset-suite` / `codex/full-compact-slact-design`。

## 未决项

- `.env` 历史泄漏：必须轮换 API key，并用 `git filter-repo` 清历史。
- `.trae/` 在新 `.gitignore` 中被整目录忽略，但 main 上仍有 4 个已跟踪文件；c7ac46a 新增的 3 个 `.trae/specs/expose-context-usage-observability/*.md` 因此未能进入 `feat/compaction-pipeline`。需选择：迁移到 `docs/` 或放开 `.gitignore` 中 `.trae/` 规则。

## 引用

- AGENTS.md §二·分支隔离、§二·变更留痕
- AGENTS.md §四·高级约束目录（`AGENTS.<module>.md` 入口约定）
