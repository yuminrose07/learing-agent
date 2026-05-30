# 研学模式 Phase 1B 启动 + Phase 1A 硬化方案落盘

> 日期：2026-05-30
>
> 类型：新增文档（4 份）
>
> 范围：
> - `docs/output/learning-mode-phase-1a-hardening-2026-05-30.md`（doc1，新增）
> - `docs/design/design-learning-mode-phase-1b-1e-skeleton.md`（doc2，新增）
> - `docs/design/design-learning-mode-phase-1b-orientation-context.md`（doc3，新增）
> - `docs/output/learning-mode-phase-1b-plan-checklist-2026-05-30.md`（doc4，新增）

---

## 背景

Phase 1A 铸造状态骨架（`forge_stage entry→collision` + `temperature_state` 枚举 + `FORGE_STAGE_CHANGED` 事件 + SSE + 单测 + E2E real dataset）已合入主干，但在向 Phase 1B 推进前发现地基有 4 条裂缝：

1. Phase 1A baseline 文件 `status` 仍是 `not_yet_executed`，所谓 "8/8 pass" 未实跑验证。
2. absorbing 阶段是否真的走 `STUDY_PROFILE` 而非残留 `CHAT_PROFILE` 的迁移核查不充分。
3. 自适应对齐侧面事件（`ALIGNMENT_SUGGESTED` / `SKIPPED` / `RESOLVED` / `ASSUMPTION_ACCEPTED` / `OBJECTIVE_REFINED`）emit 路径在代码中存在，但真实 JSONL 中观察不到，限流静默降级无对应事件。
4. `_apply_rate_limits` 把 alignment 建议静默降为 none，没有对应可观测事件。

裂缝若不收口直接推 Phase 1B，会在不稳地基上叠 PR，引入 regression 时难以归因。本批方案因此采用 "A+ 路径"：**推 Phase 1B + 同步补齐这 4 条裂缝**，先把地基硬化再做新功能。

---

## 关键决策

### 1. 顺序 gate（不可越级）

`doc1 (1A 硬化) 合入 main → doc2 (1B-1E 骨架) 评审通过 → doc3 (1B 详设) 评审通过 → doc4 (1B checklist) 开工`

四份文档全部登记同一顺序约束，缺前置不动后续代码。

### 2. 文件命名规约

- 与 `docs/README.md` 现有风格对齐：
  - `docs/design/` 下技术设计文档无日期后缀
  - `docs/output/` 下 PRD / 执行计划 / checklist 含日期后缀
- doc3 文件名最终定为 `design-learning-mode-phase-1b-orientation-context.md`（无日期、含 `-context` 后缀），与 PRD 中"入局情境"语义对齐。

### 3. 新增事件 schema：双事件（禁止单事件 `source` 字段隐式分叉）

- `LEARNING_UNIT_ALIGNMENT_RATE_LIMITED = "learning_unit.alignment_rate_limited"`
  由 doc1 唯一交付，1B-1E 不在主流程 emit；schema 唯一事实源在 doc1 §5。
- `LEARNING_UNIT_ORIENTATION_GENERATED = "learning_unit.orientation_generated"`（LLM 成功路径）
- `LEARNING_UNIT_ORIENTATION_FALLBACK_USED = "learning_unit.orientation_fallback_used"`（静态降级路径）
  双事件 schema 唯一事实源在 doc3 §3，doc2 与 doc4 仅引用。
- 注册位置统一使用符号锚点（`session_events.py` 中 `LEARNING_UNIT_ALIGNMENT_RATE_LIMITED` 之后追加），不绑定行号。

### 4. orientation 通道方向（不可翻案）

- orientation 走**独立 provider 调用**，产物落 `unit.orientation_context` 字段。
- 禁止进 `TurnExecutionProfile.system_prompt` / addendum / 主回合 prompt 装配链。
- LLM 调用必须有硬超时；超时 / 失败 → 静态降级 → 不阻塞主回复 → emit `ORIENTATION_FALLBACK_USED`。
- 新增模块 `learning_agent/learning_agent/orientation_policy.py`（与 `forge_policy.py` 同层注入风格，禁止 `import learning_agent.main`，防止循环依赖）。

### 5. forge_stage 推进入口唯一

- `forge_policy.maybe_advance_forge_stage` 仍是唯一推进入口。
- 1B 允许在该入口现有判断前追加 `orientation_context is not None` 前置 gate（含降级路径已尝试）。
- 不允许第二个推进入口。
- ASK 对齐轮仍跳过整个 forge 推进。

### 6. 旧 1A unit 兼容

加载时若 `forge_stage in {collision, forge, fixed, cooling}` 且 `orientation_context is None` → 直接放行，不触发生成、不阻塞主流程、不发 orientation 相关事件；仅当新建 unit（`forge_stage=entry, orientation_context=None`）走完整 1B 路径。

### 7. 1A baseline 实跑回填规约

- 单独一个 commit，commit message 必须含字面 `baseline_version=<值>`（如 `chore(1a/baseline): 实跑回填 forge-state baseline, baseline_version=2026-05-30-v1`），便于 doc4 §10 用 `git log --grep` 锁定 commit hash。
- 同 commit（或紧随其后）在 `docs/changes/` 留 1A 硬化 close 记录。

### 8. docs/changes/ 留痕规约

每个子阶段提交合并入 main 时必须在 `docs/changes/<date>-learning-mode-phase-1X-{kickoff|close|rollback}.md` 留痕。本文档即 1B 的 kickoff 留痕。

---

## 不在本次变更范围

按 PRD 与 development-plan 严格分层，本批方案**明确不解决**以下内容：

- 研学门厅（PRD 第 1 层，未列入 dev-plan）
- 理解博物馆 / UnderstandingExhibit 对象 / 五层展品（Phase 2）
- 学习人格镜子 / learner_profile / `LEARNER_PERSONALITY_OBSERVED`（Phase 4）
- 能力主题展（Phase 5）
- 1C 碰撞与直觉捕获 / 1D 用户自己的话持久化 / 1E 少量定型+降温（按顺序 gate 推进，每个独立 design + checklist）
- Runtime mode 分支硬化 / TEACH `final_answer_guard` / STUDY 掌握度判定（后续巩固期）
- `temperature_state` 6 枚举的非 `steady` 值驱动力（归 1E）
- 学习卷持久化向 event-sourcing 迁移（独立评估）
- 多元 `stop_reason` / stop modal meta / ALIGNMENT_POPUP 竞争边界（P2）

---

## 后续

1. **doc1（1A 硬化）评审与实施**：codex 按 doc1 §1-§5 执行 4 条裂缝收口；裂缝 4 引入 `LEARNING_UNIT_ALIGNMENT_RATE_LIMITED` 事件；1A baseline 实跑回填单独 commit。
2. **doc1 close 留痕**：合入 main 时在 `docs/changes/<close-date>-learning-mode-phase-1a-close.md` 留 close 记录，登记 baseline commit hash。
3. **doc2 + doc3 评审**：doc1 合入后启动；doc3 的 5 个 open question 评审中拍板，回填到 doc4 §10。
4. **doc4 checklist 实施**：所有前置 gate 满足后由 codex 在新分支 `feat/learning-phase-1b-orientation-context` 上按 doc4 §3 原子化提交，每 commit 一条 checkbox。
5. **下一份 changes 留痕**：1B 关闭时在 `docs/changes/<close-date>-learning-mode-phase-1b-close.md` 留 close 记录。

---

## 文档生成方式

本批 4 份方案文档由本仓库的 AI 代理通过两轮 workflow（起草 → 双轨对抗审稿 → 跨文档一致性审 → 重对齐 → 二轮跨文档审）生成，最终人工校对跨引用一致性并修正 doc1 中 2 处文件名漂移（`docs/design/design-learning-mode-phase-1a-forge-state-skeleton.md` 前缀缺失、`docs/output/learning-mode-development-plan-2026-05-27.md` 引用名错位）。代码实施由 codex 承担。
