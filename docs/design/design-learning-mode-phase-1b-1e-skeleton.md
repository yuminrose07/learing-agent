# 研学模式 Phase 1B-1E 技术设计骨架

> 项目：learning-agent
>
> 文档类型：技术设计骨架 / Phase 1B-1E
>
> 日期：2026-05-30
>
> 状态：v0.3，骨架，未进入实施
>
> 上游文档：
> - PRD：`docs/output/learning-mode-final-product-prd-2026-05-27.md`
> - Phase 1 总设计：`docs/design/design-learning-mode-phase-1-minimal-loop.md`
> - Phase 1A 硬化（doc1）：`docs/output/learning-mode-phase-1a-hardening-2026-05-30.md`
> - Phase 1A checklist：`docs/output/learning-mode-phase-1a-plan-checklist-2026-05-27.md`
> - 自适应对齐：`docs/design/design-learning-unit-adaptive-alignment.md`
> - 同批 1B 详设（doc3）：`docs/design/design-learning-mode-phase-1b-orientation-context.md`

---

## 0. 本文档定位

本文档是骨架（skeleton），只为 Phase 1B-1E 立边界，回答四个问题：
1. 每个子阶段在 PRD「理解铸造场」里覆盖哪一段。
2. 每个子阶段允许触达哪些状态字段、新增哪些事件。
3. 每个子阶段如何接力推进 `forge_stage`、如何与 `alignment_state` 旁路协同。
4. 每个子阶段明确不做什么、与博物馆/镜子如何不耦合。

本文档**不写实现细节**：不给 schema、不给 emit 路径、不给前端字段映射。每个子阶段进入实施前**必须**有独立的 `docs/design/design-learning-mode-phase-1X-*.md` 与 `docs/output/learning-mode-phase-1X-plan-checklist-*.md`，且独立可回滚。Phase 1B 的独立设计已先行落到本批次的姊妹文档（doc3）里。

### 0.1 codex 消费方式（重要）

本骨架文档**不可直接驱动代码提交**，只作为 1B-1E 各子阶段的 invariants 与边界来源。任何 1B-1E 的实现 PR 必须以「子阶段独立 design + 独立 checklist」为唯一上游；codex 在 §4-§7 看到的描述都属于约束而非动手清单，不要据此开始实施。原子化第一步在哪里——在各子阶段 checklist 里，不在本文档里找。

**特别注意**：若 1B-1E 子阶段 design 与本骨架字面冲突，**design 为事实源**；骨架仅定边界，设计决策归子阶段。

### 0.2 [GATE] 子阶段交付清单 + 前置门禁（前置门禁）

每个 1B/1C/1D/1E 进入实施前必须**全部**满足下列条件，缺一律不得提交实现 PR：

0. **doc1 已合入 main**：Phase 1A 硬化（doc1，`docs/output/learning-mode-phase-1a-hardening-2026-05-30.md`）必须先合入 main，1B-1E 各子阶段才可在其上依赖。

1. 独立设计文档：`docs/design/design-learning-mode-phase-1X-*.md`，含目标 / 非目标 / 状态字段 / 事件 / forge 接力 / alignment 协同 / real dataset 占位 / 关闭条件 / 不做事项。

2. 独立 checklist：`docs/output/learning-mode-phase-1X-plan-checklist-*.md`，列出原子化、可独立提交的实施步骤与每步的验收方式。

3. real dataset 文件（具体文件名见 §13）：开工前可只登记占位路径；子阶段关闭前必须由实现 PR 落实为可执行数据集。

4. real baseline 文件（具体文件名见 §13）：开工前只要求锁定路径与验收口径；子阶段关闭前**必须实跑生成**，不允许像 1A 那样留 `not_yet_executed` 状态（见同批 1A 文档）。baseline 不是 1B 开工前置物，避免形成“未实现却要求实跑”的死锁。

5. 单测覆盖：新增字段/事件至少有一条 pytest 用例。

6. 文档/代码/测试三同步；新增字段必须对上游字段缺失保持降级，允许前置阶段被回退后单独存在（见 §14）。

---

## 1. 与 PRD「三层一横」的对照

PRD `docs/output/learning-mode-final-product-prd-2026-05-27.md` §3 把研学产品拆成三层一横：

| PRD 章节 | 形态 | Phase 1B-1E 是否覆盖 |
|---|---|---|
| 研学门厅 | 学习前入口 | 不在 Phase 1 范围（见 §6 不做事项） |
| 理解铸造场 / 入局 | 学习时 | Phase 1B 覆盖 |
| 理解铸造场 / 碰撞 | 学习时 | Phase 1C 覆盖 |
| 理解铸造场 / 用户铸造 | 学习时 | Phase 1D 覆盖 |
| 理解铸造场 / 定型与降温 | 学习时 | Phase 1E 覆盖 |
| 理解博物馆 | 学习后 | Phase 2 范围，本文档只预留衔接点 |
| 学习人格镜子 | 一横 | Phase 4 范围，本文档只预留衔接点 |
| 能力主题展 | 学习后 | Phase 5 范围 |

结论：Phase 1B-1E 只覆盖「理解铸造场」中部四态（入局→碰撞→铸造→定型/降温），且只能写「场内」字段与事件，**不许**写门厅 / 博物馆 / 镜子的数据底座。

---

## 2. 已经在主干里的能力（codex 可直接依赖）

以下都是 1A 已经合入、本批可直接复用的锚点，1B-1E 不应重复实现：

- `LearningUnitPhase = "absorbing" | "outputting" | "consolidated" | "stopped"`：`learning_agent/ai/learning_unit.py`。
- `_ALLOWED_TRANSITIONS` 守卫（学习卷主链）：`learning_agent/ai/learning_unit.py:179-187`；`can_transition_to` / `transition_to` 方法：`learning_agent/ai/learning_unit.py:237-246`。
- `ForgeStage = "entry" | "collision" | "forge" | "fixed" | "cooling"` 枚举与默认值：`learning_agent/ai/learning_unit.py:57` 枚举定义、`:232` 字段默认。
- `TemperatureState`（6 枚举）：`learning_agent/ai/learning_unit.py:59-66, :233`。
- `AlignmentState` 五档（idle/suggested/active/resolved/skipped）：`learning_agent/ai/learning_unit.py:27-33`。
- 自适应对齐三道限流参数：`learning_agent/learning_agent/alignment_policy.py:66-92`；副作用编排 `apply_alignment_decision`：`learning_agent/learning_agent/alignment_policy.py:151-234`。
- forge_policy 委托器与 entry→collision 推进：`learning_agent/learning_agent/forge_policy.py:42-118`；main.py 包装 `learning_agent/learning_agent/main.py:1405-1421`；推进调用点 `main.py:1548-1550`。
- `STUDY_PROFILE` / `TEACH_PROFILE` / `ASK_PROFILE` / `CHAT_PROFILE`：`learning_agent/learning_agent/mode_service.py:99-149`。
- 事件枚举（含 `LEARNING_UNIT_FORGE_STAGE_CHANGED`、doc1 新增的 `LEARNING_UNIT_ALIGNMENT_RATE_LIMITED`）：`learning_agent/learning_agent/session_events.py:37-51`。
- 学习卷准备入口 `_prepare_learning_unit_turn`：`learning_agent/learning_agent/main.py:983-1133`；absorbing 开场 addendum 构造 `_build_absorbing_opening_addendum`：约在 `main.py:1135` 之后；流式入口 `stream_session_chat`：`main.py:1447-1645`；alignment_popup 短路：`main.py:1491-1522`。
- TEACH 反馈卡聚合渲染：`learning_agent/learning_agent/teach_flow.py:42-105`（反向问答主体在 `stream_teach_answer_flow` 所在段，1B-1E 不动）。

注：以上 file:line 锚点若与主干漂移，以代码为准；进入子阶段实施前请由该子阶段 design 文档自行重新校对。

---

## 3. forge_stage 推进的最终状态图

> 本状态图仅描述 `forge_stage` 这一独立状态机，**与 `LearningUnit.phase` 主链正交**：`phase` 仍按 `absorbing ⇄ outputting → consolidated` 主链独立演进，两者各自演化、互不替代。一个 unit 完全可以同时处于 `(phase=consolidated, forge_stage=cooling)` 或 `(phase=absorbing, forge_stage=cooling)`。

```
              [1B 入局]              [1C 碰撞]            [1D 用户铸造]           [1E 定型/降温]
                  v                     v                     v                      v
 (new unit) -> entry --(stage_changed)-> collision --(collision_recorded)-> forge --(forge_insight_captured)-> fixed --(temperature_changed)-> cooling
                  ^                                                                                          |
                  |                                                                                          v
                  +------------- (open question: cooling -> entry，留各子阶段拍板) ----------- (forge_stage 终态)
```

箭头标签为下方表格事件名的缩写，**不得据此新建 `E_*` 命名的事件类型**；实际事件类型见下表，事件命名以下表为准。

| 转移 | 实施子阶段 | 触发条件（粗略） | 配套事件 | 备注 |
|---|---|---|---|---|
| `entry → collision` | 1A 已实现 | absorbing 首轮 STUDY 成功 finalize | `LEARNING_UNIT_FORGE_STAGE_CHANGED` | 见 `forge_policy.py:69-117` |
| `collision → forge` | 1C | 检测到「碰撞已发生」信号（具体形式留 1C） | `LEARNING_UNIT_COLLISION_RECORDED` + `LEARNING_UNIT_FORGE_STAGE_CHANGED` | 1C design 拍板信号源 |
| `forge → fixed` | 1D | 用户用「自己的话」给出可持久化的复述 | `LEARNING_UNIT_FORGE_INSIGHT_CAPTURED` + `LEARNING_UNIT_FIXED_PHRASE_PERSISTED` + `LEARNING_UNIT_FORGE_STAGE_CHANGED` | 1D design 决定持久化形态 |
| `fixed → cooling` | 1E | 少量定型样本达标、温度判定可降温 | `LEARNING_UNIT_TEMPERATURE_CHANGED` + `LEARNING_UNIT_FORGE_STAGE_CHANGED` | 1E 才允许真正驱动 `temperature_state` 6 枚举 |
| 回退（任意） | 待评审 | open question | — | 见 §10 |

是否参与 replay 由各子阶段 design 拍板（见 §9）。

---

## 4. Phase 1B 入局情境生成

### 4.1 范围
- 在 `forge_stage = entry` 与 `forge_stage = collision` 之间，把 PRD §3「学习时-入局」的产品语义落进 STUDY absorbing 首轮：让首轮 assistant 输出真正承担「入局情境」，而不只是机械结构。
- 落地形态在姊妹文档 `docs/design/design-learning-mode-phase-1b-orientation-context.md`（doc3）给出最细颗粒。

### 4.2 允许触达的状态字段
- 新增「入局情境」相关字段（仅本卷范畴）的具体 schema 见 1B 设计文档（doc3）；骨架不预设字段名。
- 复用：`unit.objective.text`、`unit.alignment_state`、`unit.phase`、`unit.forge_stage`。

### 4.3 新增事件类型
- 占位（双事件，禁止用 source 字段隐式分叉）：
  - `LEARNING_UNIT_ORIENTATION_GENERATED`（LLM 成功路径，仅在生成成功时 emit）。
  - `LEARNING_UNIT_ORIENTATION_FALLBACK_USED`（静态降级路径，仅在 LLM 失败时 emit）。
- 注册位置：`learning_agent/learning_agent/session_events.py` 在 doc1 落地的 `LEARNING_UNIT_ALIGNMENT_RATE_LIMITED` 之后追加（符号锚点，不绑定行号；若 doc1 未合入则 1B Step 1 不动）。
- payload 共有字段：`learning_unit_id` / `forge_stage` / `temperature_state` / `source`（枚举 `llm|fallback`）/ `orientation_digest`。
- 不新增：博物馆 / 镜子相关任何事件。

### 4.4 与 1A forge_stage 推进的接力
- 1B 只增强 `entry` 态的产物质量，**不修改** `entry → collision` 的推进时机；该推进仍由 `forge_policy.maybe_advance_forge_stage` 在首轮 finalize 后触发（`main.py:1548-1550`、`forge_policy.py:69-117`）。
- 入局情境**走独立 provider 调用**产生 `OrientationContext` 结构体，**不进入** `TurnExecutionProfile` 的 `system_prompt` / `system_prompt_addendum` / 主对话 history，**不与** `STUDY_PROFILE` 的 `system_prompt` / addendum 通道共用。
- 1B **不新增 forge_stage 推进入口**，但允许在 `forge_policy.maybe_advance_forge_stage` 现有判断之前追加 `orientation_context is not None` 前置 gate（含 LLM 失败时的静态降级路径已尝试过）。
- Runtime 与 provider adapter **严禁**追加任何 1B 相关 prompt（四层职责约束，详见 §8 第 7 条）。

### 4.5 关闭条件
- 1B 视为完成的判据：absorbing 进入时能稳定生成 `OrientationContext` 结构体并就地保存到 unit；前端有独立容器展示 orientation；LLM 失败时走静态降级、不阻塞主回复（两事件分别 emit）；real dataset + baseline 全绿。

### 4.6 不做事项
- 不做碰撞捕获（属于 1C）。
- 不持久化用户原始直觉（属于 1D）。
- 不驱动 `temperature_state` 真实判定（仍保持 `steady`，由 1E 实现）。
- 不动 ASK 对齐轮：1B 流程在 STUDY absorbing 触发，ASK 对齐分支保持 1A 行为。

### 4.7 与博物馆 / 镜子的衔接预留
- 仅约定 1B 引入的「入局情境」字段必须**就地**保存在 `LearningUnit` 上：
  - a) 不抽 `UnderstandingExhibit` / `learner_profile`（Phase 2 / Phase 4 范畴）。
  - b) **不抽 `LearningForgeState` 子模型**（沿用 Phase 1A 约束）。
- 后续 Phase 2 / Phase 4 想消费时，自己从 unit 上读，不要求 1B 提前建表。

### 4.8 与 alignment_state 旁路如何协同
- alignment 旁路的五档（`learning_unit.py:27-33`；副作用编排 `alignment_policy.py:151-234`）与 1B 的入局情境**正交**：
  - C 档 `active` 仍走 `alignment_popup` 短路（`main.py:1491-1522`），1B 的产物在弹 modal 时不进入 prompt。
  - B 档 `suggested` 的收窄建议由该档自行处理；1B 的 orientation 产物通过独立 provider/UI 容器不进 prompt 竞争。
  - A 档 `none` / `clear_enough`：1B 全量生效。
- ASK 对齐轮**绝不**推进 forge_stage（1A 已约束），1B 不松动这一点。
- alignment 限流参数（`alignment_policy.py:66-92`，含 `MAX_SUGGESTIONS_PER_UNIT` / `COOLDOWN_AFTER_ACCEPT_ASSUMPTION`）由 1B 只读；任何 forge 推进逻辑不得调整或共享其计数器。

---

## 5. Phase 1C 碰撞与直觉捕获

### 5.1 范围
- 实现 `collision → forge` 推进：让产品能识别 absorbing 期内「碰撞已经发生」，并捕获用户在这次碰撞里冒出来的原始直觉/猜测/反应。
- 对应 PRD §3「点燃好奇、制造碰撞」与「补足缺口前的最后一个原始反应」。

### 5.2 允许触达的状态字段
- 复用：`unit.phase`、`unit.forge_stage`、`unit.alignment_state`。
- 新增字段（占位，不在骨架拍板）：「碰撞已记录」标志位、最近一次碰撞触发依据（用于 1C 自检与回滚）。
- 不允许新增「用户直觉文本」字段：那是 1D 的范畴；1C 只负责识别碰撞「发生过」，不负责把直觉落库。

### 5.3 新增事件类型
- 占位：`LEARNING_UNIT_COLLISION_RECORDED`、`LEARNING_UNIT_FORGE_STAGE_CHANGED`（复用 1A 已有事件类型，data 里区分 `from/to`）。
- 是否参与 replay 由 1C design 拍板。

### 5.4 与 forge_stage 推进的接力
- 1C 是 `collision → forge` 的实施者，必须沿用 1A 的 `forge_policy.maybe_advance_forge_stage` 委托模式（`forge_policy.py:69-117`、`main.py:1405-1421`），由 main.py 现有统一推进点调用，**不**在 prompt 流里硬塞推进；允许在该唯一入口内部重构为阶段分发器，但外部不得新增第二个 forge 推进入口。
- 必须保持「`outputting` 不处理 forge」与「ASK 对齐轮不推进 forge」两条 1A invariants。

### 5.5 关闭条件
- absorbing 期内可稳定从 `collision` 推进到 `forge`，且有 real dataset + baseline 覆盖「碰撞已发生」「碰撞未发生但 N 轮后超时」两种路径。

### 5.6 不做事项
- 不持久化直觉文本（1D）。
- 不评判直觉对错（不属于 Phase 1）。
- 不改动 alignment 旁路。

### 5.7 与博物馆 / 镜子的衔接预留
- 碰撞记录就地存 unit：
  - a) 不抽 `UnderstandingExhibit` / `learner_profile`（Phase 2 / Phase 4 范畴）。
  - b) **不抽 `LearningForgeState` 子模型**（沿用 Phase 1A 约束）。

### 5.8 与 alignment_state 旁路如何协同
- B 档/A 档下 1C 全量生效；C 档（`active`）弹 modal 期间不推进 forge——与 1A 行为一致。
- alignment 限流参数（`alignment_policy.py:66-92`，含 `MAX_SUGGESTIONS_PER_UNIT` / `COOLDOWN_AFTER_ACCEPT_ASSUMPTION`）由 1C 只读；任何 forge 推进逻辑不得调整或共享其计数器。

---

## 6. Phase 1D 用户自己的话持久化

### 6.1 范围
- 实现 `forge → fixed`：用户用「自己的话」给出对当前学习目标的复述/类比/小总结时，把这段「自己的话」就地持久化到 unit 上。
- 对应 PRD §3「让用户亲手把知识铸成自己的理解」与「定型样本」。

### 6.2 允许触达的状态字段
- 新增「用户自己的话」相关字段（schema 占位，留 1D design 拍板）。
- 复用：`unit.phase`、`unit.forge_stage`、`unit.objective`。

### 6.3 新增事件类型
- 占位：`LEARNING_UNIT_FORGE_INSIGHT_CAPTURED`、`LEARNING_UNIT_FIXED_PHRASE_PERSISTED`、`LEARNING_UNIT_FORGE_STAGE_CHANGED`。
- 必须 append-only 写入 JSONL（invariants §11），不允许就地修改既有事件。

### 6.4 与 forge_stage 推进的接力
- `forge → fixed` 的触发条件应当与「至少一次有效复述被持久化」绑定；具体 N（一次还是若干次）由 1D design 拍板，骨架不预设。
- 仍由 `forge_policy` 统一推进，不在 1D 内私造推进入口。

### 6.5 关闭条件
- absorbing 期内可稳定从 `forge` 推进到 `fixed`，且持久化的「自己的话」可在 SSE / 前端 `#learning-unit-card` 上展示（沿用现有 22 个 learning_unit_* 字段通道，不新增 SSE 字段除非 1D design 显式说明）。

### 6.6 不做事项
- 不做温度判定（1E）。
- 不做反向问答评估（已在 `teach_flow.py` 巩固期，1D 不动）。
- 不引入 `UnderstandingExhibit`：复述就地存 unit。

### 6.7 与博物馆 / 镜子的衔接预留
- 「自己的话」必须以「unit 内字段」形态存在：
  - a) 不抽 `UnderstandingExhibit` / `learner_profile`（Phase 2 / Phase 4 范畴）。Phase 2 想做展品时，自己从 unit 字段拷贝/投影出去；1D 不替 Phase 2 设计 schema。
  - b) **不抽 `LearningForgeState` 子模型**（沿用 Phase 1A 约束）。

### 6.8 与 alignment_state 旁路如何协同
- 1D 在 absorbing 期生效；C 档对齐期间不进入 1D 路径——与 1A/1C 一致。
- alignment 限流参数（`alignment_policy.py:66-92`，含 `MAX_SUGGESTIONS_PER_UNIT` / `COOLDOWN_AFTER_ACCEPT_ASSUMPTION`）由 1D 只读；任何 forge 推进逻辑不得调整或共享其计数器，1D 完全不共享 alignment 限流计数。

---

## 7. Phase 1E 少量定型 + 降温

### 7.1 范围
- 实现 `fixed → cooling`，并让 `TemperatureState` 6 枚举（`learning_unit.py:59-66`）**首次真正被驱动**——在此之前所有阶段都保持 `steady`。
- 对应 PRD §3「火候感」「少量定型」「降温收束」。

### 7.2 允许触达的状态字段
- 真正读写 `unit.temperature_state`。
- 新增「定型样本计数」类字段（占位，留 1E design 拍板）。
- 复用：`unit.phase`、`unit.forge_stage`、TEACH 已有反向问答字段。

### 7.3 新增事件类型
- 占位：`LEARNING_UNIT_TEMPERATURE_CHANGED`、`LEARNING_UNIT_FORGE_STAGE_CHANGED`。
- 是否参与 replay 由 1E design 拍板；倾向参与（温度是产品语义不是 UI 状态）。

### 7.4 与 forge_stage 推进的接力
- 1E 唯一负责 `fixed → cooling` 的触发；触发条件与「少量定型样本达标 + 温度判定可降温」绑定，二者缺一不可。
- `fixed → cooling` **必须沿用** `forge_policy.maybe_advance_forge_stage`（`forge_policy.py:69-117`）这一委托模式：不允许在 `main.py` 或新建的 `temperature_policy` 模块里私造推进入口。温度判定可拆到独立纯函数，但 **forge_stage 推进点全项目唯一**。
- `cooling` 是 `forge_stage` **这一独立状态机的终态**，**不是** `LearningUnit.phase` 的终态；`phase` 主链 `absorbing ⇄ outputting → consolidated` 独立演进，一个 unit 完全可以同时处于 `(phase=consolidated, forge_stage=cooling)` 或 `(phase=absorbing, forge_stage=cooling)`。是否允许从 `cooling` 回到 `entry`（卷重开）属于 open question，骨架不拍板。

### 7.5 关闭条件
- 真实 dataset 验证：在合理一卷学习过程中，温度可从 `steady` 走到至少一种「降温前兆」枚举，最终把 `forge_stage` 推到 `cooling`；卷结束时可进入 TEACH / consolidated 主链。
- 不破坏 outputting/consolidated 主链：`cooling` 与「学习卷主链 absorbing⇄outputting→consolidated」并行存在，cooling 只描述铸造场状态，不替代 phase。

### 7.6 不做事项
- 不实现 TEACH 反向问答增强（已在 `teach_flow.py`）。
- 不写入「掌握度反馈卡」之外的 consolidated 物料（属于后续巩固期）。
- 不修改 alignment 旁路的限流参数（`MAX_SUGGESTIONS_PER_UNIT`、`COOLDOWN_AFTER_ACCEPT_ASSUMPTION`，`alignment_policy.py:66-92`）。

### 7.7 与博物馆 / 镜子的衔接预留
- `temperature_state` 与 `forge_stage` 终态字段都就地存在 unit 上：
  - a) 不抽 `UnderstandingExhibit` / `learner_profile`（Phase 2 / Phase 4 范畴）。
  - b) **不抽 `LearningForgeState` 子模型**（沿用 Phase 1A 约束）。

### 7.8 与 alignment_state 旁路如何协同
- 1E 仍遵守「ASK 对齐轮不推进 forge」与「`outputting` 不处理 forge」。
- 温度变化与 alignment 限流**完全独立**；任何一档 alignment 下温度判定都不该被冻结，除非 1E design 明确写出例外。
- alignment 限流参数（`alignment_policy.py:66-92`）由 1E 只读，不得调整或共享其计数器。

---

## 8. 统一约束（四个子阶段都必须遵守）

1. `temperature_state` 6 枚举（`learning_unit.py:59-66`）只在 **1E** 真正驱动；1B/1C/1D 期间一律保持 `steady`。
2. 非 absorbing 阶段不处理 forge：`outputting` 与 `consolidated` 期间不允许触发 `forge_stage` 推进事件；推进调用点必须复用 `forge_policy.maybe_advance_forge_stage`（`forge_policy.py:69-117`），**且全项目只允许 `forge_policy` 一个 `forge_stage` 推进入口**，不允许在其它模块写第二个入口。
3. ASK 对齐轮不推进 forge：1A invariants，沿用 `main.py:1491-1522` 的 alignment_popup 短路；C 档期间任何子阶段都不得修改 forge_stage。
4. 学习卷主链 `absorbing ⇄ outputting → consolidated` 不变：四个子阶段都不允许新增或松动 `_ALLOWED_TRANSITIONS`（`learning_unit.py:179-187`）。
5. 四模式 profile 分离不变：STUDY_PROFILE/TEACH_PROFILE 专属研学；CHAT_PROFILE/ASK_PROFILE 不允许携带 forge 字段或新事件。
6. SSE 字段不轻易扩容：现有 `learning_unit_*` 字段优先复用；新增字段必须在子阶段 design 中显式说明前端必要性，否则不加。1B 若按 doc3/doc4 展示 orientation，只允许暴露轻量 presence / hook 元数据，不把完整 `OrientationContext` 结构体塞进 SSE。
7. 四层职责守护：1B-1E 若需要把上下文交给主回合模型，只能在 Product 层装配 `TurnExecutionProfile`；**Runtime 层（含 `stream_session_chat`、`agent_loop.run`）与 provider adapter 严禁追加任何 1B-1E 相关主回合 prompt**。1B orientation 是例外中的受控独立通道：它由 Product 层发起独立 provider 调用，产物只落 `unit.orientation_context`，禁止进入 `TurnExecutionProfile.system_prompt` / `system_prompt_addendum` / 主对话 history。

---

## 9. 事件是否参与 replay 的统一原则

- 所有新增事件**默认 append-only 写入 JSONL**（invariants §11）。
- 是否被 `session_projection.py` 消费、是否参与 SSE 重放，由各子阶段 design 在评审时拍板，**不在骨架文档定**。doc3 已对 1B 拍板：`ORIENTATION_GENERATED` / `ORIENTATION_FALLBACK_USED` 为 AGENT 可观测事件，不参与 replay。
- 若某事件仅作可观测用途（不进入产品状态），子阶段 design 必须显式说明「projection 不消费」，且在 real baseline 里把该事件标为「仅出现，不影响 state 派生」。

---

## 10. forge_stage 是否允许回退（open）

骨架视野下默认所有推进**单向**：`entry → collision → forge → fixed → cooling`。但以下三类情形子阶段评审时必须给出明确判断：
1. `fixed → forge`：用户在定型后又给出与既有「自己的话」冲突的新复述，是否允许回到 forge 再炼。
2. `collision → entry`：第一次碰撞被判失败、需要重铺入局，是否允许回退。
3. `cooling → entry`：同一 unit 内卷重开。

**骨架不拍板**，统一标注为 open question；任何子阶段若要允许回退，必须同步更新 1A 设计文档与 invariants。

---

## 11. 必须遵守的 invariants（与上游一致，本骨架不松动）

- 事实源唯一：append-only JSONL，状态变化必须有事件。
- 四层职责：Product 持产品状态 / Runtime 只消费 `TurnExecutionProfile` / Interface 只展示派生态 / Infrastructure 不携业务策略。
- **forge 相关所有字段（`forge_stage` / `temperature_state` / 1B 入局情境 / 1C 碰撞记录 / 1D 用户自己的话 / 1E 定型计数与温度）必须就地挂在 `LearningUnit` 上，不抽 `LearningForgeState` 子模型（沿用 Phase 1A 约束）。**
- N=1 不未来化：不为 Phase 2/4/5 建数据底座。
- 子阶段独立可回滚：每子阶段必须有 design + 实现 + 单测 + real dataset + baseline；不允许跨子阶段共享一次提交。独立 revert 方向见 §14。
- 学习卷主链不变：absorbing⇄outputting→consolidated，禁止 outputting→aligning、consolidated→任意前态。
- ASK 单卷启动仅 1 次阻塞澄清；非阻塞建议最多 2 次（`alignment_policy.py:66-92`）；用户同意后 3 轮内不主动建议（同段）。
- STUDY_PROFILE/TEACH_PROFILE 专属研学，CHAT_PROFILE 专属非学习卷 else 分支（`mode_service.py:99-149`）。
- 文档/代码/测试三同步：每子阶段提交里必须三类都动到。
- **docs/changes/ 留痕**：每个子阶段（含 1A 硬化）提交合并入 main 时必须在 `docs/changes/<date>-learning-mode-phase-1X-{kickoff|close|rollback}.md` 留痕（见 doc4 §6 文件名规约）。
- git 安全：commit 中文、点名 `git add`；禁止 `git add -A`、`git checkout .`、`git reset --hard`、`git stash`、`git clean`、`git rebase` 任何破坏共享工作树的操作。

---

## 12. 不做事项（本骨架文档明确不解决）

| 不做 | 原因 / 归属 |
|---|---|
| 给 1C/1D/1E 写实施级 schema 或 emit 路径 | 留各子阶段独立 design |
| 拍板 forge_stage 是否允许回退 | open question，子阶段评审 |
| 引入 `UnderstandingExhibit` / `learner_profile` | 属于 Phase 2 / Phase 4，N=1 不未来化 |
| 抽 `LearningForgeState` 子模型 | 沿用 Phase 1A 约束，禁止 |
| 研学门厅独立空间 | Phase 未列入 dev-plan |
| Runtime mode 分支硬化 / TEACH final answer guard | 留后续巩固期 |
| 多元 stop_reason / stop modal meta | P2 |
| ALIGNMENT_POPUP 与 suggestion_bar 竞争边界 | P2 |
| 持久化 event-sourcing 改造 | 留独立 Phase |
| Phase 1B 入局情境的最细颗粒（已在姊妹文档） | 见 §0 同批 doc3 文档 |

---

## 13. 子阶段交付清单（每个 1B/1C/1D/1E 进入实施前必须齐备）

交付清单内容与 §0.2 [GATE] 一致；本节补充各子阶段的具体 dataset/baseline 文件名。开工前锁定路径，关闭前实跑回填 baseline：

- 1B：
  - `tests/e2e/real_datasets/learning-mode-phase-1b-orientation-context-real.json`
  - `tests/e2e/real_baselines/learning-mode-phase-1b-orientation-context-real.baseline.json`
- 1C：
  - `tests/e2e/real_datasets/learning-mode-phase-1c-collision-capture-real.json`
  - `tests/e2e/real_baselines/learning-mode-phase-1c-collision-capture-real.baseline.json`
- 1D：
  - `tests/e2e/real_datasets/learning-mode-phase-1d-fixed-phrase-real.json`
  - `tests/e2e/real_baselines/learning-mode-phase-1d-fixed-phrase-real.baseline.json`
- 1E：
  - `tests/e2e/real_datasets/learning-mode-phase-1e-temperature-cooling-real.json`
  - `tests/e2e/real_baselines/learning-mode-phase-1e-temperature-cooling-real.baseline.json`

baseline 在子阶段关闭前**必须实跑生成**，不允许留 `not_yet_executed` 状态。

---

## 14. 实施顺序与独立 revert 语义

骨架建议按 1B → 1C → 1D → 1E 顺序推进，理由：

- 1B 是「让 entry 这一态名副其实」，不引入新转移，最低风险。
- 1C 引入第一个由产品逻辑触发的转移（`collision → forge`），需要确定碰撞信号源，工程量中等。
- 1D 引入第一个持久化产物字段，工程量较大但与 1C 解耦。
- 1E 是温度首次真正驱动，最依赖前三步沉淀的数据。

**独立 revert 的方向是后置 → 前置（不可逆）**：

- 1E 可独立回退、1B/1C/1D 仍保留；1D 可独立回退、1B/1C 仍保留；1C 可独立回退、1B 仍保留。反向不成立：1B 一旦上线，1C/1D/1E 才可叠加。
- 1C/1D/1E 引入的字段必须对**前置阶段字段缺失保持降级**：读到 `None` 时走 1A 行为；禁止把前置字段做成 NOT NULL 依赖。
- 1E 不允许在 1D 未上线时进入实施（温度判定的输入依赖「定型样本」，本身就是后置阶段对前置的依赖；1D 上线是 1E 的 hard prerequisite）。
- 同理，被回退子阶段必须有「字段缺失 → 默认值回退路径」，使叠加在其上的后续阶段在该回退发生时仍能读到合理默认。

---

## 15. 与 alignment_state 旁路的并行视图（汇总）

| 子阶段 | A 档 / none | B 档 / suggested | C 档 / active | resolved |
|---|---|---|---|---|
| 1B 入局情境 | 全量生效 | 由各档独立处理，1B 的 orientation 通过独立 provider/UI 不进 prompt | 走 alignment_popup 短路，不进入 prompt | 沿用 1A |
| 1C 碰撞捕获 | 全量生效 | 全量生效 | 不推进 forge | 沿用 1A |
| 1D 自己的话 | 全量生效 | 全量生效 | 不推进 forge | 沿用 1A |
| 1E 定型/降温 | 全量生效 | 全量生效 | 不推进 forge、不改 temperature_state | 沿用 1A |

C 档下唯一允许变化的是 `alignment_state` 自身（编排见 `alignment_policy.py:151-234`），任何 forge / temperature 字段在 C 档期间都不应被触动。若 1E design 需要在 active alignment 下继续做温度读数，必须先显式改写本表并给出不推进 forge、不改 `temperature_state` 的边界。

---

## 16. open questions（≤3）

1. forge_stage 是否允许回退（`fixed → forge` / `collision → entry` / `cooling → entry`）。骨架建议默认单向，由各子阶段在自己的 design 里拍板。

2. 新增事件（`LEARNING_UNIT_ORIENTATION_GENERATED` / `LEARNING_UNIT_ORIENTATION_FALLBACK_USED` / `COLLISION_RECORDED` / `FORGE_INSIGHT_CAPTURED` / `FIXED_PHRASE_PERSISTED` / `TEMPERATURE_CHANGED`）是否参与会话 replay。骨架建议默认参与，但留各子阶段 design 评审时确认 projection 消费方式。

3. `LEARNING_UNIT_ALIGNMENT_RATE_LIMITED` 由 doc1（`docs/output/learning-mode-phase-1a-hardening-2026-05-30.md`）交付，1B-1E 不在主流程 emit；若 1B-1E 需要在 alignment 触发时标注 `triggered_at_forge_stage`，留各子阶段按需扩展事件 data。**若 doc1 未通过评审，本骨架对 `ALIGNMENT_RATE_LIMITED` 的引用须同步回退。若 doc3 实施前发现与 doc2 §4.4 通道方向冲突，以 doc2 为事实源（doc2 已与 doc1+doc4 三方互锁）。**
