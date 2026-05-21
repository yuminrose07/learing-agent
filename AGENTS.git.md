# AGENTS.git.md — Git 工作流

本文档定义本仓库的分支、提交、合并规则。AGENTS.md 中"分支隔离"与"变更留痕"两条核心规则在此展开为可执行细则。冲突时以 AGENTS.md 为准。

***

## 〇、当前模式：单人项目

本仓库目前为单人开发，下列条款放宽。**引入协作者时立刻全部恢复**——这些规则的真正用途是把人和人之间的协作摩擦提前规约掉。

| 条款 | 单人模式下的状态 |
|---|---|
| §四 PR Checklist | PR 非必须；可直接 `git push origin <feat-branch>` 后本地 fast-forward 或自合并到 main。但 checklist 本身仍是有用的 **自检表**——commit 数量、变更范围、文档留痕、本地验证四项每次都应过一遍。 |
| §四 "已通过 review" | 不适用，删除即可。 |
| §六 "禁止 PR 进入 review 后 rebase 已被他人 review 的 commit" | 不适用。私有 feature 分支随便 rebase。 |
| §六 "禁止直接 push 到 `main`" | 降级为 **自律建议**：紧急小修可直接 push，但功能/重构仍建议走 feature 分支以保留逻辑边界。 |
| §六 "禁止 `--force`" | 降级为 **谨慎使用**：单人项目可用 `--force`，但仍优先 `--force-with-lease`；只对私有 feature 分支使用，对 `main` 仅在 `git filter-repo` 这类整体重写场景使用，并先用 `git bundle` 备份。 |

仍然 **硬约束** 不变的条款（这些是单人也要遵守的工程素养）：

- §一 分支命名（`<type>/<desc>`）
- §二 Conventional Commits + 一个 commit 一件事
- §二 绝不提交 secret / 生成产物 / runtime data
- §三 `.gitignore` 基线
- §六 绝不提交 `.env` / 不用 `--no-verify` 绕 hook
- §七 全部历史教训

***

## 一、分支模型

### 1.1 长期分支

| 分支 | 角色 | 谁能直接 push |
|---|---|---|
| `main` | 唯一可发布的主干，永远可构建、测试通过 | 仅通过 PR 合入 |
| `release` | 发布快照分支，按需从 main 切出 | 仅 release 责任人 |

### 1.2 工作分支命名

所有工作必须在新分支上进行（见 AGENTS.md §二·分支隔离）。命名格式：

```
<type>/<short-kebab-description>
```

`<type>` 必须是下列之一：

- `feat/` 新功能
- `fix/` Bug 修复
- `chore/` 构建、依赖、仓库治理（不影响业务逻辑）
- `docs/` 仅文档
- `test/` 仅测试
- `refactor/` 重构（行为不变）
- `perf/` 性能优化（行为不变）

示例：
- `feat/compaction-pipeline`
- `fix/turn-usage-async`
- `chore/cleanup-runtime-artifacts`

**禁止**：
- 工具自动生成的 `codex/*`、`claude/*` 分支不得直接进入正式流程。AI 草稿期使用后，正式提 PR 前必须 rename 为标准前缀。
- 同一分支堆叠多个无关功能。
- 在已污染、未合并的分支上拉新分支（栈式分支会驮着上游的脏 commit）。

### 1.3 工作分支必须基于 `main` 的最新提交

```bash
git fetch origin
git switch -c feat/xxx origin/main
```

不要基于其他工作分支拉新分支，除非明确需要 stacked PR 且 review 时会一起合入。

***

## 二、提交规则

### 2.1 Conventional Commits

格式：

```
<type>: <subject>

<optional body — why, not what>
```

`<type>` 与分支前缀对齐：`feat` / `fix` / `chore` / `docs` / `test` / `refactor` / `perf`。

subject 用现在时祈使句，小写开头，无句号，≤72 字符。

正面示例（仓库已有）：
- `chore: add pytest and pytest-asyncio to requirements`
- `fix: wire compaction summary pipeline`
- `test: add compaction dataset runner`

**反面示例（本仓库历史）**：
- `v1`、`fix v2`（两次同名）、`reday compact`、`micro-compact`、`full compact fix` — 事后无法追溯做了什么。

### 2.2 一个 commit 一件事

提交前先 `git diff --stat` 看规模：

- 非生成代码变更 > 400 行 → 几乎一定该拆。
- 同时包含"功能代码 + 文档 + 删除日志文件 + .pyc"→ 必须拆。

**反面示例**：本仓库 `c7ac46a "full compact fix"` 一个 commit 51082 行，混了 compaction 源码、`.pyc`、48k 行 `events.jsonl`、设计文档、被删 session JSON。所有下游分支都驮着这坨东西，无法独立合并。

### 2.3 不要提交的内容

绝对不要进入任何 commit：

- 密钥与凭据：`.env`、`*.key`、`credentials.json`、token、API key。
- 生成产物：`__pycache__/`、`*.pyc`、`.pytest_cache/`、`.mypy_cache/`、`dist/`、`build/`。
- 运行时数据：`.learning_agent_data/`、`.observability/`、本地 session JSON、events.jsonl。
- 系统垃圾：`.DS_Store`、`Thumbs.db`、IDE 工程文件（除非全队统一）。

提交前自检：

```bash
git status            # 有无意外文件
git diff --cached     # 看 staged 内容
```

只 `git add` 明确的文件，不要无脑 `git add -A` / `git add .`。

### 2.4 `.env` 模板而非 `.env`

`.env.example` 入库，列出所有需要的环境变量名（值留空或填占位符）。`.env` 永久忽略。

***

## 三、`.gitignore` 基线

新建仓库时，`.gitignore` **必须是第一个 commit**，先于任何业务代码。本项目至少包含：

```gitignore
# Python
__pycache__/
*.py[cod]
*.egg-info/
.pytest_cache/
.mypy_cache/
.venv/
venv/

# Secrets
.env
.env.*
!.env.example

# IDE / OS
.DS_Store
Thumbs.db
.idea/
.vscode/

# Project runtime
.learning_agent_data/
.observability/
```

***

## 四、PR Checklist

提 PR 前自检：

- [ ] 分支名符合 §1.2 前缀规范，已从 `codex/*` 这类草稿前缀 rename。
- [ ] 基于 `origin/main` 最新 commit，无不必要的 merge commit。
- [ ] 每个 commit 都符合 Conventional Commits 格式。
- [ ] `git diff --stat origin/main..HEAD` 检查过，无生成产物、无 secret、无 runtime data。
- [ ] 单 PR 解决单一关注点，commit 数量与变更范围匹配。
- [ ] 若涉及新建文档、重构、修 Bug、新增需求，已在 `docs/changes/` 留痕（AGENTS.md §二·变更留痕）。
- [ ] 本地测试通过；若改动 UI/CLI，已手工验证。

PR 描述必须包含：

- **Why**：动机或要解决的问题。
- **What**：核心改动一句话。
- **Test plan**：如何验证。

***

## 五、常见操作 Cheatsheet

```bash
# 开新工作分支
git fetch origin
git switch -c feat/xxx origin/main

# 同步 main 的新改动到当前分支
git fetch origin
git rebase origin/main         # 优先 rebase，保持线性历史

# 提交前检查
git status
git diff --stat
git diff --cached

# 拆分一个过大的 staged 变更
git reset                       # 全部 unstage
git add -p <file>               # 交互式选择 hunk

# 看分支与历史
git log --all --oneline --graph --decorate

# 看分支独有的 commit
git log origin/main..HEAD --stat

# 改最后一个 commit message（尚未 push）
git commit --amend

# 把本地分支推到远程并建立追踪
git push -u origin feat/xxx

# 删除已合并的远程/本地分支
git branch -d feat/xxx
git push origin --delete feat/xxx
```

***

## 六、禁止事项

- **禁止** 直接 push 到 `main` 或 `release`。
- **禁止** 在公开分支上 `git push --force`；私有工作分支允许 `--force-with-lease`，绝不裸 `--force`。
- **禁止** 在 PR 进入 review 后 rebase 已被他人 review 的 commit（破坏 review 线索）。
- **禁止** 提交 `.env` 等含密文件。一旦发生：立即轮换密钥，再用 `git filter-repo` 清历史，并在 PR 中标注。
- **禁止** 用 `--no-verify` 跳过 hook，除非用户显式要求。

***

## 七、本仓库历史教训（反面教材）

供未来 review 时直接引用。带 ✓ 的是已通过 `git filter-repo` 重写历史清理掉的问题，其余仍在追溯参考。

1. ✓ **炸弹 commit**：原 `c7ac46a "full compact fix"` 把源码 + 48k 行 events.jsonl + .pyc + 设计文档塞进一个 51082 行的 commit。已重建为 `feat/compaction-pipeline` 上 5 个有结构的 commit。→ 见 §2.2。
2. ✓ **无效 commit message**：`v1` / `fix v2` ×2 / `reday compact` / `micro-compact`。已在 2026-05-21 通过 `git filter-repo --commit-callback` 全部重写为 Conventional Commits 格式。→ 见 §2.1。
3. ✓ **`.env` 入库**：API key 已轮换，历史已通过 `git filter-repo --invert-paths --path .env` 清除。备份 bundle 在 `.git/filter-repo-backup-*.bundle`。→ 见 §2.3、§六。
4. ✓ **运行时数据入库**：`.learning_agent_data/`、`.observability/`、`__pycache__/`、`*.pyc`、`.DS_Store` 已从全部历史清除；新 `.gitignore` 防止再次入库。→ 见 §三。
5. **AI 草稿前缀进入正式流程**：原 `codex/full-compact-slact-design` 等分支未 rename 即推远程，与 `chore/*`、`test/*` 混杂；已删除。今后凡 `codex/*`、`claude/*` 草稿分支必须在提 PR 前 rename。→ 见 §1.2。
6. **栈式污染分支**：原 `fix-compaction-summary-pipeline`、`test/compaction-dataset-suite` 建在炸弹 commit 之上，无法独立合并；已重建。今后未合并的分支上不再拉新分支。→ 见 §1.3。
