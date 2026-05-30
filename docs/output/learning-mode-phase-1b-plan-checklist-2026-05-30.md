# 研学模式 Phase 1B 计划清单（入局情境生成）

> 项目：Learning-Agent
>
> 文档类型：计划清单 / 关闭闸门（Phase 1B）
>
> 日期：2026-05-30
>
> 状态：v0.5，待评审（已合入双轨审稿 P0+择优 P1/P2 + 跨文档一致性建议 + 5 条 cross_recs + 第二轮跨文档小修）
>
> 配套技术设计：`docs/design/design-learning-mode-phase-1b-orientation-context.md`（doc3，待 codex 产出）
>
> 配套前置硬化：`docs/output/learning-mode-phase-1a-hardening-2026-05-30.md`（doc1，1A 裂缝收口）
>
> 真实数据集（占位）：`tests/e2e/real_datasets/learning-mode-phase-1b-orientation-context-real.json`
>
> Baseline（占位）：`tests/e2e/real_baselines/learning-mode-phase-1b-orientation-context-real.baseline.json`

---

## 1. 阶段目标与本文定位

Phase 1B 的目标是：在 `forge_stage=entry` 阶段，系统能为一个新创建的学习卷生成「入局情境」（OrientationContext），让用户在进入 collision 之前先看到「目标 / 边界 / 已知陷阱 / 学习路径建议」，为首次学习价值（FIRST_VALUE_DELIVERED）提供锚点。

本文档的角色与 Phase 1A checklist (`docs/output/learning-mode-phase-1a-plan-checklist-2026-05-27.md`) 同构：**关闭闸门**。

- 不写「怎么做」，只列「要做什么」与「做完的判定标准」。
- 全部条目都要在 doc3 实施完毕后由 codex 勾选并留 PR / commit / baseline 锚点。
- 任一条目不达成 → Phase 1B 不可关闭。

> 凡涉及「怎么实现」「字段精确定义」「LLM 调用契约」一律不在本文展开，统一交给 doc3。

---

## 2. 前置准备（开工硬门）

下列项不通过 → **禁止动 Phase 1B 任何代码**。

- [ ] doc1（1A 硬化）已交付并合入 main：
  - [ ] `tests/e2e/real_baselines/learning-mode-phase-1a-forge-state-real.baseline.json` 的 `status` 字段从 `not_yet_executed` 实跑回填为 `pass` 或带 `implementation_gap`（当前 baseline 行 5）。**该回填动作必须作为单独一个 commit 提交**，commit message 必须显式包含 `baseline_version` 取值（如 `chore(1a/baseline): 实跑回填 forge-state baseline，baseline_version=2026-05-30-v1`），且同一 commit（或紧随其后的同分支 commit）在 `docs/changes/` 留 1A 硬化 close 记录文件（如 `docs/changes/<close-date>-learning-mode-phase-1a-close.md`）；§10 #2 引用该 commit hash 而非仅版本号字符串。
  - [ ] `absorbing` 进入时 profile 切换为 `STUDY_PROFILE` 的路径在单测中被覆盖（`learning_agent/learning_agent/mode_service.py:122`，`learning_agent/ai/learning_unit.py:196`）。
  - [ ] 对齐侧面事件 `ALIGNMENT_SUGGESTED / SKIPPED / RESOLVED / ASSUMPTION_ACCEPTED / OBJECTIVE_REFINED` 至少在一份真实 JSONL 中被观察到（`learning_agent/learning_agent/session_events.py:39-47`）。
  - [ ] 新增 `ALIGNMENT_RATE_LIMITED` 事件常量已落地，限流静默降级路径有 emit 调用（`learning_agent/learning_agent/alignment_policy.py:75`、`151-235`）。

- [ ] doc3（1B 技术设计）已经过用户评审，且本文 §10 open_questions 已在 doc3 阶段回填完毕。

- [ ] 1A baseline 版本号锁定：1B 开工时把 doc1 实跑后的 1A baseline 记入 `baseline_version` 字段（如 `2026-05-30-v1`，具体取值由 doc1 实跑当日确定），1B 期间禁止就地改写该 baseline；§10 #2 给出决议口径，并以上文实跑回填 commit 的 hash 作为引用锚点。

- [ ] 分支策略：Phase 1B 必须新开分支 `feat/learning-phase-1b-orientation-context`，分支起点 = doc1 合入 main 后的 HEAD；不要求工作树干净；codex 不主动 `git stash` / `git clean`，用户本地未相关 dirty 改动自行处理。

> 理由：1A baseline 未实跑 + absorbing→STUDY 迁移未核查 + 对齐事件实证缺失，地基本身就漂；1B 在其上推进会被地基扰动污染验收。

---

## 3. 代码 checklist

每条对应**单独一个** commit（commit message 中文，含子节号，如 `feat(1b/§3.1): LearningUnit 新增 orientation_context 字段`）。**禁止合并提交**：§3.1-§3.6 一个 checkbox = 一个 commit，否则 §8 逆序 revert 流程会断裂。锚点来源于现网代码，doc3 落地时若锚点偏移需在本表对应行同步更新行号。

### 3.1 模型层（Product）

- [ ] `LearningUnit` 增加 `orientation_context` 字段，默认 `None`，旧 JSON 缺字段能加载（插入位置：`learning_agent/ai/learning_unit.py:230-234`，紧跟 `first_value_delivered_at`、与 `forge_stage` / `temperature_state` 同区块，归入「Phase 1A 铸造状态骨架」注释块之内或紧邻其后）。
- [ ] **旧 1A unit 兼容性条款（显式）**（cross_recs #1，confirmed）：加载时若 `forge_stage in {collision, forge, fixed, cooling}` 且 `orientation_context is None`，**直接放行**——不触发生成、不阻塞主流程、不发 orientation 相关事件；**仅当新建 unit**（`forge_stage=entry` 且 `orientation_context is None`）时才走完整 1B 路径（即 §3.2 三段守卫触发的生成 + 降级链路）。该条款必须在加载入口处显式落地（断言或注释指明放行分支），并由 §4.1 单元测试覆盖。
- [ ] 新增 `OrientationContext` 模型（字段集由 doc3 拍板，本 checklist 不列具体字段）。**字段集 invariant 类约束**：必须包含 `source` 字段，枚举值取自集合 `{llm, fallback}`；`user_override` 是否纳入由 §10 #3 拍板（默认本期不引入）。
- [ ] `_ALLOWED_TRANSITIONS` 不改（`learning_agent/ai/learning_unit.py:179-184`）；orientation 生成是 `forge_stage` 内事件，不是 phase 迁移。
- [ ] `forge_stage` 仍只支持 `entry → collision` 单向（`learning_agent/learning_agent/forge_policy.py:97-99`）；orientation 仅作为「entry 阶段达成首轮学习价值」的必要条件之一，不引入新的 stage。

### 3.2 编排层（Application）

- [ ] `forge_policy.maybe_advance_forge_stage` 在判断 `entry → collision` 之前新增「**orientation 生成尝试已发生**」前置条件，gate 直接复用「`unit.orientation_context is not None`」（成功或降级路径均算已尝试）（`learning_agent/learning_agent/forge_policy.py:69-118`）。
- [ ] 新增模块 `learning_agent/learning_agent/orientation_policy.py`（与 `forge_policy.py` 同层注入风格），导出 `maybe_generate_orientation(*, store, emit_unit_event, session, unit) -> OrientationContext | None`。由 `_prepare_learning_unit_turn` 在满足下列三段守卫时调用（`learning_agent/learning_agent/main.py:983-1133`）：`unit.phase == "absorbing"` and `unit.forge_stage == "entry"` and `unit.orientation_context is None`。**禁止将 `first_value_delivered_at is None` 写入守卫**，避免与 FIRST_VALUE_DELIVERED 事件次序耦合；该三段守卫天然让 ASK 对齐轮不触发 orientation。
- [ ] **依赖注入边界登记（防循环依赖）**（cross_recs #5，confirmed）：`orientation_policy.py` 与 `forge_policy.py` 同层、同注入风格——**禁止直接 `import learning_agent.main`** 或任何反向引用主入口模块；所需依赖（`store` / `emit_unit_event` / `session` / `unit`）一律通过函数参数注入，与 `forge_policy.py` 现有风格一致。该边界由 §4.1 单测断言（模块顶层 import 集合不含 `learning_agent.main`）。
- [ ] orientation 生成失败时走静态降级（具体降级策略由 doc3 拍板），**禁止阻塞主回复流**；单次 LLM 调用必须有硬超时（具体秒数由 doc3 拍板，本 checklist 强制要求该上限存在），超时即降级。
- [ ] ASK 对齐轮（alignment_popup 短路路径 `learning_agent/learning_agent/main.py:1491-1522`）不触发 orientation 生成，也不消费 orientation；active alignment 短路 return 后由下一 STUDY 轮补生成 orientation（§5 新增 case 覆盖）。
- [ ] **Runtime 边界 invariant**：orientation 生成通过独立 provider 调用完成，不进入主对话 history，不通过 `TurnExecutionProfile` 暴露给 Runtime；Runtime 仍只收到主 absorbing 回合的 profile。doc3 不允许翻案。
- [ ] **Resume 不重生成**：学习卷跨会话 resume 时，`orientation_context is not None` 即直接复用，不重新生成、不重发 `LEARNING_UNIT_ORIENTATION_GENERATED` 事件。
- [ ] **Metrics 不变性**：orientation 生成轮不改 `first_value_delivered_at` 的 t0 语义、不算 FIRST_VALUE；TTFV t0 仍以「首条 absorbing 阶段 assistant 消息成功 finalize」为准。

### 3.3 事件层（Infrastructure-as-fact-source）

- [ ] `session_events.py` 增加两个新常量（**在 `LEARNING_UNIT_FORGE_STAGE_CHANGED`（`learning_agent/learning_agent/session_events.py:50`）之后追加**，与 forge / first_value 同族内聚）：
  - `LEARNING_UNIT_ORIENTATION_GENERATED = "learning_unit.orientation_generated"`（**仅 LLM 成功路径**）
  - `LEARNING_UNIT_ORIENTATION_FALLBACK_USED = "learning_unit.orientation_fallback_used"`（**仅静态降级路径**）
- [ ] 事件命名规约：全文事件名使用全限定字符串（如 `learning_unit.orientation_generated`），单测断言禁止裁切前缀写法（不允许写 `ORIENTATION_GENERATED`）。
- [ ] orientation 生成成功 → 追加 `LEARNING_UNIT_ORIENTATION_GENERATED`；payload 强制项：`learning_unit_id` / `forge_stage` / `temperature_state` / `source=llm` / `orientation_digest`（结构体的稳定哈希或截断摘要，算法由 doc3 拍板并在 §6 文档 checklist 中登记）。
- [ ] orientation 生成失败 → 追加 `LEARNING_UNIT_ORIENTATION_FALLBACK_USED`，payload 强制项与成功事件同形（含 `orientation_digest`），`source=fallback`，**不丢事件**。
- [ ] 事件写入失败不阻塞主流程（与 Phase 1A 同等承诺）。
- [ ] 事件遵循 append-only invariant；禁止 update 既有事件做「修正」。Resume 路径已在 §3.2 保证不重发。

### 3.4 SSE / API 层（Interface）

- [ ] `GET /learning-units/{unit_id}` payload 增加 `orientation_context` 字段。
- [ ] assistant SSE metadata 在 **`orientation_context` 已存在的所有回合（含生成本轮）** 追加 `orientation_context_present=true`；仅做存在标记，结构体走 REST 拉取，避免 SSE 帧膨胀。
- [ ] 非研学习会话（CHAT/ASK 独立 session）payload 中 `orientation_context` 字段不出现或恒为 `null`。
- [ ] SSE `learning_unit_*` 字段计数同步更新（22 → 23）；如有 SSE schema 文档同步登记；若无 schema 文档则在 code comment 中维护并在 §6 显式注明「无 SSE schema 文档」。

### 3.5 前端层（Web）

- [ ] `web/static` 下学习卷卡片渲染器消费 `orientation_context`：在 `#learning-unit-card` 内新增 `[data-testid="orientation-context"]` 容器；`orientation_context == null` 时 `hidden`；非研学习会话不渲染该容器。DOM 结构由 doc3 给出 wireframe。
- [ ] 收到 `orientation_context_present=true` 的 SSE metadata 后，前端主动拉取最新 `learning_unit` 一次刷新卡片，不轮询。
- [ ] 静态测试覆盖（`tests/test_observability_static.js`）：必须断言 `[data-testid="orientation-context"]` 容器存在，且 orientation 存在 / 缺失 / 降级三态可见性正确（exists / hidden / visible）。

### 3.6 注册与配置

- [ ] 两个新事件常量（`LEARNING_UNIT_ORIENTATION_GENERATED` 与 `LEARNING_UNIT_ORIENTATION_FALLBACK_USED`）在事件注册表 / projection 中**显式分别登记**，避免落到「未知事件」分支被静默吞掉；下游 projection / 指标必须把两者作为独立事件类型计算（不允许仅靠 `source` 字段隐式分叉）。
- [ ] 配置开关（如 `learning.orientation.enabled`）默认开启，但保留环境变量可一键禁用（用于回滚兜底）；具体 key 由 doc3 拍板。

---

## 4. 测试 checklist

四类测试缺一不可，且彼此独立可执行。

### 4.1 单元测试

- [ ] `OrientationContext` 模型默认值 / 缺字段加载 / 字段越界拒绝；`source` 枚举值校验。
- [ ] **旧 1A unit 兼容性回归**（cross_recs #1，confirmed）：构造 `forge_stage ∈ {collision, forge, fixed, cooling}` 且 `orientation_context is None` 的历史 unit JSON，加载后不触发生成、不发 orientation 事件、主流程可继续；仅 `forge_stage=entry` + `orientation_context is None` 走完整 1B 路径。
- [ ] **依赖注入边界断言**（cross_recs #5，confirmed）：`orientation_policy` 模块顶层 import 集合不包含 `learning_agent.main`（防循环依赖回归）。
- [ ] orientation 生成函数：成功路径返回结构体并追加 `LEARNING_UNIT_ORIENTATION_GENERATED` 事件（payload 含 `orientation_digest`）。
- [ ] orientation 生成失败 / LLM 超时路径：触发降级，返回降级结构体，事件类型为 `LEARNING_UNIT_ORIENTATION_FALLBACK_USED`（不是同名事件的 `source` 分叉），主流程不抛。
- [ ] `forge_policy.maybe_advance_forge_stage` 在 `orientation_context is None`（即首轮尚未尝试）时拒绝 `entry → collision`；orientation 尝试发生后（含降级）允许推进。
- [ ] ASK 对齐轮三段守卫不命中：不触发 orientation 生成。
- [ ] Resume 路径：`orientation_context is not None` 时再次进入 absorbing 不重新生成、不重发事件。

### 4.2 集成测试

- [ ] `_prepare_learning_unit_turn` 首轮 absorbing 调用路径覆盖 orientation 生成 + 后续 STUDY 回复合并入同一回合；**该回合 SSE metadata 必须带 `orientation_context_present=true`**（首轮含本轮）。
- [ ] 事件 JSONL 在一个完整学习卷生命周期里的事件序列：`CREATED → ORIENTATION_GENERATED → FIRST_VALUE_DELIVERED → FORGE_STAGE_CHANGED(entry→collision)`，顺序与去重正确（事件名以全限定字符串断言）。
- [ ] 学习卷 stop / resume modal 路径不破（回归 `learning_agent/learning_agent/main.py:1491-1522` 三 modal 短路）；resume 路径 orientation 不重生成。

### 4.3 E2E real dataset

- [ ] 新增 `tests/e2e/real_datasets/learning-mode-phase-1b-orientation-context-real.json` 覆盖第 5 节五个 case。
- [ ] 经由 `tests/e2e/real_runner.py` 统一入口跑通（不允许新写一份 runner）。
- [ ] 每个 case 留存 request / response / session / learning_unit / events 五件套。

### 4.4 Baseline 实跑 + 回归

- [ ] 新增 `tests/e2e/real_baselines/learning-mode-phase-1b-orientation-context-real.baseline.json`，**首次必须实跑回填 `status=pass` 或带具体 `implementation_gap`**，禁止留 `not_yet_executed`（避免重蹈 1A baseline 漂移覆辙）。
- [ ] 回归 1A 既有 4 case (`lm-p1a-fuzzy-index-stage` / `lm-p1a-light-compaction-stage` / `lm-p1a-ask-does-not-advance-stage` / `lm-p1a-stage-event-after-first-value`) 全部 pass，禁止因 1B 引入 regression。运行依据 §2 锁定的 1A `baseline_version`（以 §2 实跑回填 commit hash 为准）。

---

## 5. 真实数据集 Cases（占位，doc3 实施时确认命名）

| Case 占位 ID | 目标 | 关键验收 |
|------|------|----------|
| `lm-p1b-orientation-generated-on-first-turn` | 新研学卷首轮 absorbing 后生成 orientation | learning_unit payload 含 `orientation_context`，事件流含一条 `learning_unit.orientation_generated` |
| `lm-p1b-no-attempt-blocks-stage-advance` | **首轮 orientation 生成尝试未发生**时 forge_stage 不推进 | 在 orientation 尝试事件出现之前，`learning_unit.forge_stage_changed(entry→collision)` 不出现；尝试事件出现后才允许推进 |
| `lm-p1b-orientation-fallback-on-llm-failure` | LLM 失败/超时走静态降级且不阻塞回复 | 事件为 `learning_unit.orientation_fallback_used`（不是 generated 同名事件），回复成功 finalize，`TTFV_p50 <= 1A baseline TTFV_p50 * 1.5` |
| `lm-p1b-ask-alignment-does-not-trigger-orientation` | ASK 对齐轮不消费也不生成 orientation | alignment_popup 短路路径无 orientation 相关事件 |
| `lm-p1b-orientation-defer-after-active-alignment` | active alignment 短路后由下一 STUDY 轮补生成 orientation | active alignment 轮无 orientation 事件，紧接的 STUDY 轮出现 `learning_unit.orientation_generated`，事件次序正确 |

> case 最终命名以 doc3 §10.2 落地为准，命名前缀强制 `lm-p1b-`，case 数量 ≥5；落地后回写本表。禁止单边漂移。

---

## 6. 文档 checklist

- [ ] doc3 (`docs/design/design-learning-mode-phase-1b-orientation-context.md`) 与最终实现行号一致（差异 >3 行需要 doc3 同步修订）。
- [ ] doc3 必须明确给出 `orientation_digest` 的摘要算法（输入字段、哈希算法、截断长度）。
- [ ] `docs/README.md` 研学读取顺序表（当前 `docs/README.md:21-29`）新增「研学 Phase 1B」一行，指向 doc3 + 本 checklist。
- [ ] `docs/changes/` 落两份（`<close-date>` 取值规则：§7 全绿当天的本地日期 `YYYY-MM-DD`；**未全绿前不创建 close/acceptance 文件**）：
  - [ ] `docs/changes/2026-05-30-learning-mode-phase-1b-kickoff.md`（开工记录，写在 doc1 合入 + doc3 评审通过当天）。
  - [ ] `docs/changes/<close-date>-learning-mode-phase-1b-close.md`（关闭记录）。
- [ ] 本文档底部 §10 open_questions（见下方 [open-questions](#open-questions) 锚点）在 doc3 实施前全部回填为「已对齐：xxx」或「已删除：xxx」状态。
- [ ] 若仓库不存在 SSE schema 文档，本 checklist §3.4 已声明在 code comment 中维护；此条以「已确认无 schema 文档」勾选。

---

## 7. 关闭条件（全绿才允许打 close）

下列全部满足，Phase 1B 才算关闭：

- [ ] §3 代码 checklist 全部勾选，每项绑定到合入 main 的 commit hash（每个 checkbox = 一个 commit，禁止合并）。
- [ ] §4 测试 checklist 全部通过，包含 baseline 实跑 + 1A 回归（依据 §2 锁定的 `baseline_version`，以 §2 实跑回填 commit hash 为引用锚点）。
- [ ] §6 文档 checklist 全部勾选。
- [ ] 一份 acceptance 留痕：`docs/output/acceptance-report-learning-mode-phase-1b-<close-date>.md`，含 baseline 摘要 / 真实 JSONL 摘要 / 前端截图三件套。
- [ ] 学习卷主链 `absorbing⇄outputting→consolidated` 不变（人工 grep 确认 `_ALLOWED_TRANSITIONS` 在 `learning_agent/ai/learning_unit.py:179-184` 未被 1B 改写）。
- [ ] N=1 invariant 守住：本阶段未引入「为 1C/1D/1E 预留的数据底座」。

---

## 8. 回滚条件

任一关键代码 PR 引入下列 regression → **立即回退到 1A**：

- 真实数据集出现「学习卷创建失败 / 主回复 finalize 失败 / TTFV 显著退化（`TTFV_p50 > 1A baseline TTFV_p50 * 1.5`，与 §5 fallback case 阈值一致）」中任一项。
- 1A 回归 4 case 中任意一条由 pass 转 fail。
- 事件流出现 `_ALLOWED_TRANSITIONS` 之外的 phase 迁移。
- ASK 对齐轮被观察到推进了 `forge_stage` 或触发了 `learning_unit.orientation_generated` / `learning_unit.orientation_fallback_used`。
- Runtime 主对话 history 中出现 orientation 生成调用的痕迹（违反 §3.2 Runtime 边界 invariant）。
- **1A 自身 baseline 出现非已知 `implementation_gap` 类 regression 时，先回退引入该 regression 的外部 PR**（cross_recs #3），1B 不得自行 patch 1A baseline（即：1A baseline 的修复路径归 1A 硬化通道，1B 仅负责回退自身引入或外部引入的污染源，不得就地改写 §2 锁定的 baseline）。

回退操作（共享工作树前提，禁止 `git reset --hard` / `git stash` / `git clean`）：

1. 新开 revert 分支：`git checkout -b revert/phase-1b-<reason>`。
2. **按 §3 子节逆序 revert**：§3.5 前端 → §3.4 SSE → §3.3 事件 → §3.2 编排 → §3.1 模型。每个 checkbox 对应 commit 用 `git revert <commit>` 反向，避免模型层与 SSE/前端字段悬挂。
3. 保留 `tests/e2e/real_datasets/learning-mode-phase-1b-*.json` 与 baseline 文件，但 baseline `status` 改为 `rolled_back`；real_runner 对 `status=rolled_back` 的 baseline 按 **skip** 处理（不阻塞 1A 回归），如 runner 当前不支持需在本步同 commit 内补改 runner 语义。
4. 在 `docs/changes/<date>-learning-mode-phase-1b-rollback.md` 留痕。

---

## 9. 不做事项（本文档明确不解决的边界）

- 不替代 doc3 的技术设计内容（本 checklist 只列做什么、不写怎么做）。
- 不预设 Phase 1C / 1D / 1E 关闭条件（N=1 invariant：未验证就不预建）。
- 不修改 `_ALLOWED_TRANSITIONS`、不引入 `outputting→aligning` 等违反学习卷主链的迁移。
- 不抽 LearningForgeState 子模型（沿用 1A 文档约束）。
- 不动 TEACH 反向问答（`learning_agent/learning_agent/teach_flow.py:42-105`）相关字段。
- 不重新定义 alignment 五档与三道限流（仅在 doc1 范围内补齐 `ALIGNMENT_RATE_LIMITED` 事件，超出由独立阶段处理）。
- 不引入持久化 event-sourcing 重写、不引入 TEACH final answer guard、不动多元 stop_reason（均在后续巩固期）。
- 不预留「研学门厅独立空间 / 理解博物馆 / 学习人格镜子 / 能力主题展」相关结构（这些在 dev-plan 中明确归属 Phase 2-5）。
- 本期默认不引入「用户改写 orientation」能力（`source=user_override`）；若需引入由 §10 #3 拍板并补 §3.5 入口、§3.3 鉴权与事件链。

---

<a id="open-questions"></a>

## 10. Open Questions（codex 在 doc3 实施前对齐）

> 本文已就关闭闸门尽可能拍板。仍有以下问题需 codex 与用户/方案作者对齐后回填到本表，并在 doc3 中明确拍板。

1. **doc3 自身的 open_questions 锚点（拍板顺序硬约束）**（cross_recs #4，confirmed）：OrientationContext 字段集是否包含 `known_pitfalls`？计划维度上限是否 ≤5？失败降级的静态模板形态如何？三者答案直接决定 §3.1 / §3.2 / §4.1 的勾选条目。**拍板顺序**：`doc3 拍板 → 回填本节 → 动 1B 代码`（与 doc2 §0.1「design 为事实源」表述对齐）；**禁止跳序**——doc3 未拍板前不得回填本节，本节未回填前不得提交 §3 任一 commit。**附带**：real dataset case 最终命名口径（前缀是否统一 `lm-p1b-`、分隔符 `-` 或 `_`）一并在此拍板，§5 表已声明「以 doc3 §10.2 落地为准、回写本表」。doc3 §10.2 case_id 必须用 `lm-p1b-` 前缀；若 doc3 保留 `1b-case-*` 风格则视为未拍板，本节不得回填。
2. **1A baseline 版本号锁定（引用 commit hash）**（cross_recs #2，confirmed）：1B 开工时锁定 doc1 实跑后的 1A `baseline_version`（如 `2026-05-30-v1`，具体取值由 doc1 实跑当日确定，由用户拍板写入本表）；**引用形式必须是 §2 实跑回填动作所对应的 commit hash**（commit message 含 `baseline_version` 取值，且 `docs/changes/` 留 1A 硬化 close 记录），不得仅引用版本号字符串。1B 期间不得改写该 baseline。**回归判定语义**仍需用户拍板：若 1A 实跑结果含 `implementation_gap`，1B 是否允许在已知 gap 项目上继续推进（接受 gap 作为已知账目），还是必须先消除 gap？
3. **`source=user_override` 是否本期纳入**：§9 默认本期不引入用户改写 orientation 能力；若用户拍板纳入，需补 §3.5 前端入口 checklist + §3.3 鉴权约束 + 事件链（是否新增 `LEARNING_UNIT_ORIENTATION_USER_OVERRIDDEN`）。本期默认「不纳入」。
4. **orientation LLM 调用硬超时秒数**：§3.2 已强制要求硬超时存在，具体秒数（建议区间 ≤8s）由 doc3 拍板写入本表。

---

## 11. 状态

STATUS: NEEDS_REVIEW

待 doc1 与 doc3 评审通过、§10 open_questions 全部回填后，本文档转 READY，进入 codex 实施。

