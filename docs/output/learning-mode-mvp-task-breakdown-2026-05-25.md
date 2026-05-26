# 学习模式一阶段 MVP 任务拆解（前端 / 后端 / 指标 / 验收）

> 项目：Learning-Agent
>
> 文档类型：执行任务清单
>
> 日期：2026-05-25
>
> 上游依据：
> - [docs/output/learning-mode-mvp-definition-2026-05-25.md](../output/learning-mode-mvp-definition-2026-05-25.md)（产品定义稿）
> - [docs/output/report-learning-mode-product-diagnosis-2026-05-25.md](../output/report-learning-mode-product-diagnosis-2026-05-25.md)（诊断报告）
> - [docs/design/design-learning-unit-adaptive-alignment.md](../design/design-learning-unit-adaptive-alignment.md)（技术设计稿）
> - [docs/design/design-modes-refactor-chat-vs-learning-unit.md](../design/design-modes-refactor-chat-vs-learning-unit.md)（学习卷主设计）

---

## 一、本文档要回答什么

把 MVP 定义稿的 4 个产品件，落成可分到工程师手里的任务清单。每个任务标注：

- **现状**：现有代码已经做到哪里
- **动作**：MVP 阶段要改什么
- **复用点**：可以直接利用的现成实现
- **风险**：迁移、兼容、回退点

不做新一轮产品讨论；产品口径完全沿用 MVP 定义稿和 adaptive alignment 设计稿。

---

## 二、4 个产品件 → 工程动作总览

| MVP 产品件 | 后端 | 前端 | 指标 |
|-----------|------|------|------|
| 主题输入 | 沿用 [`POST /learning-units`](../../learning_agent/web/web_server.py) | 新增学习卷入口与主题输入框 | `learning_unit.created` 事件 |
| 学习开场 | 首轮模板：目标卡片 + 学习地图 + 实质讲解 | 新增学习卷视图（目标卡片 + 学习地图 + 消息流） | `time_to_first_learning_value` |
| 轻量收窄 | `alignment_state` 三档 + `should_run_alignment()` + 新增动作 API | 非阻塞建议条 + 收窄按钮 | `forced_alignment_rate`、`clarification_turns_per_unit` |
| 结束验收 | TEACH 触发链路打通 + 收尾反馈生成 | 「讲讲看 / 题集 / 复述」UI + 掌握度反馈卡片 | `teach_entry_rate`、`unit_completion_rate` |

---

## 三、后端任务（B 系列）

### B1. `LearningUnit` 数据模型字段调整

**现状**

[`learning_agent/ai/learning_unit.py:19-24`](../../learning_agent/ai/learning_unit.py#L19-L24) 当前定义：

```python
LearningUnitPhase = Literal["aligning", "absorbing", "outputting", "consolidated"]
```

且 `LearningUnit.phase` 默认值是 `"aligning"`（[learning_unit.py:136](../../learning_agent/ai/learning_unit.py#L136)），`effective_mode()` 在 `aligning` 时返回 `"ask"`（[learning_unit.py:156-164](../../learning_agent/ai/learning_unit.py#L156-L164)）。

**动作**

按 adaptive alignment §8.1-8.2 调整：

1. 从 `LearningUnitPhase` 移除 `"aligning"`，新默认 `"absorbing"`。
2. 新增字段：
   - `alignment_state: Literal["idle","suggested","active","resolved","skipped"] = "idle"`
   - `objective_status: Literal["working","refined","confirmed"] = "working"`
   - `assumption_note: str = ""`
   - `alignment_reason: str = ""`
   - `clarification_count: int = 0`
   - `last_alignment_at: Optional[datetime] = None`
3. 修订 `_ALLOWED_TRANSITIONS`（[learning_unit.py:121-127](../../learning_agent/ai/learning_unit.py#L121-L127)）：去掉 `aligning -> absorbing`，主链改为 `absorbing -> outputting -> {absorbing|consolidated}`。
4. 删除已无意义的 `aligning_round` 字段（[learning_unit.py:139](../../learning_agent/ai/learning_unit.py#L139)）或保留为 `0` 做兼容。
5. 调整 `effective_mode()`：`absorbing -> chat`、`outputting -> teach`，不再因 `aligning` 返回 `ask`。

**复用**

- `UnitObjective`、`ConceptItem`、`TangentNote`、`TeachSession`、`TeachQuestion` 全部保留（[learning_unit.py:61-110](../../learning_agent/ai/learning_unit.py#L61-L110)）。
- 状态转移检查函数 `can_transition_to / transition_to` 框架不变。

**风险与处理**

- 已有持久化 JSON：[`learning_unit_store.py`](../../learning_agent/learning_agent/learning_unit_store.py) 每卷一文件。按 adaptive alignment §8.4 在 `LearningUnitStore.load_all` 中做投影迁移：`phase=aligning` 旧数据加载时改为 `phase=absorbing, alignment_state=active, objective_status=working`。
- 不回写历史 events（保持 append-only 不变）。

### B2. Product 层 turn 调度改为自适应对齐

**现状**

[`learning_agent/learning_agent/main.py:531-577`](../../learning_agent/learning_agent/main.py#L531-L577) 的 `_prepare_learning_unit_turn()` 直接把 `unit.effective_mode()` 当作本轮模式；`create_learning_unit` 强制 `session.mode = AgentMode.ASK`（[main.py:447](../../learning_agent/learning_agent/main.py#L447)）。

**动作**

1. `create_learning_unit`：移除 `session.mode = AgentMode.ASK`，改为 `AgentMode.CHAT`（因为新默认 phase 是 `absorbing`）。
2. 在 [`learning_agent/learning_agent/`](../../learning_agent/learning_agent/) 新增 `alignment_policy.py`，实现：

```python
class AlignmentDecision(BaseModel):
    mode: Literal["none", "suggested", "active"]
    reason: Literal["clear_enough","too_broad","goal_drift",
                    "conflicting_scope","missing_learnable_target"]
    suggested_objective: str = ""
    assumption_note: str = ""

def should_run_alignment(unit, user_input, recent_messages) -> AlignmentDecision: ...
```

   一阶段判定规则保持轻量（adaptive alignment §5.3）：
   - A 档（`none`）：input 包含可识别学习对象且词数 ≥ N。
   - B 档（`suggested`）：input 有学习意图但缺切口（如"教我整个项目"）。
   - C 档（`active`）：input 无可学对象 / 多目标冲突。

   一阶段允许靠关键词 + 长度启发式实现，不需要意图分类器。

3. 改写 `_prepare_learning_unit_turn`：

```python
effective_mode = AgentMode.CHAT if unit.phase == "absorbing" else AgentMode.TEACH
if unit.phase == "absorbing":
    decision = should_run_alignment(unit, user_input, recent_messages)
    if decision.mode == "active" and unit.clarification_count == 0:
        effective_mode = AgentMode.ASK
    unit.alignment_state = decision.mode if decision.mode != "none" else "idle"
    unit.alignment_reason = decision.reason
    unit.assumption_note = decision.assumption_note
```

4. 限流（adaptive alignment §9.3）：单卷启动期阻塞澄清 ≤1，中途非阻塞建议 ≤2；超出时强制走 `chat`。

**复用**

- `AgentMode` 枚举、`mode_service`、`TurnExecutionProfile` 均不动。
- Runtime 继续只消费 profile，无需感知 alignment。

### B3. 学习卷首轮开场模板

**现状**

学习卷创建后没有专门的首轮 prompt 模板；目前依赖通用 chat persona 给响应。

**动作**

在 absorbing 首轮（`message.user_appended` 之后、`message.assistant_started` 之前）注入开场指令，输出固定结构：

1. **工作目标卡片**：一句话 `working_objective`，标记 `objective_status="working"`。
2. **学习地图**：3-5 个 bullet（模块 / 概念 / 顺序）。
3. **第一段实质讲解**：从地图的第一个切口直接开讲。
4. （仅 B 档 alignment_state=suggested 时）附一行 suggestion：`如果你想更聚焦，我可以帮你收窄成 X`。

实现位置：建议放在 [`main.py`](../../learning_agent/learning_agent/main.py) 的 `_prepare_learning_unit_turn`，通过 `system_prompt_addendum` 或 `metadata` 传给 runtime。

**复用**

- 已有 `_resolve_session_persona_key`（[main.py:565](../../learning_agent/learning_agent/main.py#L565)）—— persona 继续叠加，但首轮 schema 由学习卷模板强约束。

### B4. 收窄相关动作 API

**现状**

已有：

- `POST /learning-units/{id}/confirm-objective`（[web_server.py:498](../../learning_agent/web/web_server.py)）
- `POST /learning-units/{id}/advance`（[web_server.py:510](../../learning_agent/web/web_server.py)）

缺：用户主动收窄、接受系统假设、改写目标的入口。

**动作**

按 adaptive alignment §11.2 增加三个端点（建议直接新增，不要折叠进 advance，语义更清晰）：

| 端点 | 行为 |
|------|------|
| `POST /learning-units/{id}/align` | 用户点"帮我收窄"。把 `alignment_state` 设为 `active`，下一轮强制走 ASK。 |
| `POST /learning-units/{id}/accept-assumption` | 用户点"先按这个学"。`alignment_state = skipped`，重置 nagging 计数。 |
| `POST /learning-units/{id}/refine-objective` | 写入新的 working/refined objective，自动把 `objective_status` 升级。 |

每个端点都要走 `learning_unit_store.lock(unit_id)`（[learning_unit_store.py:130-136](../../learning_agent/learning_agent/learning_unit_store.py#L130-L136)）避免 RMW 竞态。

**复用**

- FastAPI handler 风格、`_learning_unit_payload` 序列化（[web_server.py:457](../../learning_agent/web/web_server.py)）。
- 现有 Pydantic request 模型套路。

### B5. 结束验收（TEACH）链路打通

**现状**

- 数据模型 `TeachSession`、`TeachQuestion` 已经存在（[learning_unit.py:103-110](../../learning_agent/ai/learning_unit.py#L103-L110)）。
- `_prepare_learning_unit_turn` 在 `phase=outputting` 时切到 TEACH（[main.py:560](../../learning_agent/learning_agent/main.py#L560)），但生成、评判、结束反馈是否端到端跑通需确认。

**动作**

MVP 阶段先保证最小闭环可用：

1. 用户在 absorbing 中点"讲讲看"或"做 3 题" → 后端 `advance_learning_unit(unit_id, "outputting")`。
2. 进入 outputting 后生成 `TeachSession`（题量 ≤ 3，kind = `sa` 复述题为主，`mc` 可选）。
3. 用户提交答案 → 评判（一阶段可用模型 LLM-as-judge，避免单独评分模块）。
4. TEACH 收尾：聚合 `passed/needs_review` → 输出"掌握 / 不清楚 / 下一步"反馈卡片 → `phase = consolidated`。
5. 反馈卡片字段：`mastered: list[str]`、`gaps: list[str]`、`next_topic_suggestion: str`。

**复用**

- TEACH 协议 / persona / runtime 不动。
- 已有 `verification_status: Optional["passed","skipped"]`（[learning_unit.py:141](../../learning_agent/ai/learning_unit.py#L141)）。

**与 MVP 的取舍**

MVP 不要求题集复杂或评判精准——只要求结束动作明显、反馈可读。题量 / 评判精度可在 P1 阶段再优化。

---

## 四、前端任务（F 系列）

### F1. 学习卷入口

**现状**

[`web/index.html`](../../web/index.html) 欢迎页只有 Chat / Ask / Study (disabled) 三个 mode 卡（[index.html:93-109](../../web/index.html#L93-L109)）；[`web/static/app.js`](../../web/static/app.js) 中完全没有 learning-unit 相关代码（grep 0 命中）。

**动作**

1. 欢迎页第 4 张卡：「学习模式 · 学懂一个主题」，副文案沿用 adaptive alignment §7.4 推荐 —— *"直接开始学一个概念、模块或设计；如果范围太大，我会帮你逐步收窄"*。
2. 侧边栏新增「学习卷」分组，列出未完成的 unit（调用 `GET /learning-units`）。
3. 主题输入：选模式后，输入框 placeholder 改为「想学什么主题？例如 compaction 主链 / Product Runtime 分层」。
4. 首条消息发送时：先 `POST /learning-units`，再发送消息（与新 unit 绑定）。

### F2. 学习卷视图骨架

**新建组件（HTML/CSS/JS 直接扩展现有 [app.js](../../web/static/app.js)，不引入框架）**：

1. 顶部进度条：`学习中 · 复述检验 · 已收束`（adaptive alignment §7.3，删掉"对齐目标"主阶段）。
2. 左侧目标卡片区：
   - `working_objective`（一句话）
   - `objective_status` 徽标：`working / refined / confirmed`
   - `assumption_note`（B 档时显示，"我先按这个理解"）
3. 右侧消息流：复用现有 chat 消息组件。

### F3. 首轮开场展示

把 B3 后端模板的三段输出渲染成 3 个视觉分区：

- 目标卡片（顶部）
- 学习地图（卡片下方，可折叠的 3-5 bullet）
- 第一段讲解（正常消息气泡）

不要把这三段拼成单个长文本气泡——视觉分块是学习模式区别于 Chat 的可感知信号。

### F4. 非阻塞建议条 + 收窄按钮

按 adaptive alignment §7.2：

- 在目标卡片下方一行 suggestion bar，仅当 `alignment_state="suggested"` 时显示。
- 文案：`这卷我先按 X 展开；如果你想更聚焦，我可以再收窄。`
- 按钮：
  - `先按这个学` → `POST /accept-assumption`
  - `帮我收窄` → `POST /align`
- 用户在主消息流也可输入自然语言（"帮我收窄目标"），由 B2 的判定函数识别并触发。
- 同卷同类建议 ≤ 2 次（前端再加一道节流，配合 B2 限流）。

### F5. 结束验收 UI

- absorbing 阶段右下角常驻按钮：`讲讲看`（主）、`做 3 题`（次）。点击 → `POST /advance target_phase=outputting`。
- outputting 阶段：题目卡片 + 答题输入 + 提交。
- consolidated 阶段：掌握度反馈卡片（B5 的三个字段），下方一个轻量问题——*"下次你还想学一个主题时，会不会优先用学习模式？"*（赞 / 否两个按钮，写入 `learning_unit.feedback_reuse_intent`）。

### F6. SSE / message metadata 消费

学习卷消息 SSE 必须携带（adaptive alignment §11.3）：

- `learning_unit_id`
- `learning_unit_phase`
- `alignment_state`
- `objective_status`
- `alignment_reason`
- `assumption_note`

前端收到后，**直接更新目标卡片和进度条状态**——不要靠轮询 `GET /learning-units/{id}`。刷新后通过 `GET` 恢复一次即可。

后端已在 `_prepare_learning_unit_turn` 写入 `learning_unit_id / learning_unit_phase`（[main.py:553-555](../../learning_agent/learning_agent/main.py#L553-L555)），按 B1 字段新增同步扩展。

---

## 五、指标与埋点（M 系列）

### M1. 事件类型新增

在 [`learning_agent/learning_agent/session_events.py`](../../learning_agent/learning_agent/session_events.py) 的 `SessionEventType` 中新增：

```python
LEARNING_UNIT_CREATED = "learning_unit.created"
LEARNING_UNIT_PHASE_CHANGED = "learning_unit.phase_changed"
LEARNING_UNIT_ALIGNMENT_SUGGESTED = "learning_unit.alignment_suggested"
LEARNING_UNIT_ALIGNMENT_STARTED = "learning_unit.alignment_started"
LEARNING_UNIT_ALIGNMENT_RESOLVED = "learning_unit.alignment_resolved"
LEARNING_UNIT_ALIGNMENT_SKIPPED = "learning_unit.alignment_skipped"
LEARNING_UNIT_OBJECTIVE_REFINED = "learning_unit.objective_refined"
LEARNING_UNIT_ASSUMPTION_ACCEPTED = "learning_unit.assumption_accepted"
LEARNING_UNIT_FIRST_VALUE_DELIVERED = "learning_unit.first_value_delivered"  # 触达第一段讲解
LEARNING_UNIT_TEACH_ENTERED = "learning_unit.teach_entered"
LEARNING_UNIT_CONSOLIDATED = "learning_unit.consolidated"
LEARNING_UNIT_REUSE_FEEDBACK = "learning_unit.reuse_feedback"  # 复用意愿轻量问卷
```

`visibility` 一律走 `AGENT`（参与时间轴重建），不走 `OBSERVABILITY`，因为它们是产品事件不是诊断事件（[session_events.py:36-44](../../learning_agent/learning_agent/session_events.py#L36-L44)）。

Payload 最小集合（adaptive alignment §12.2）：

```python
{"learning_unit_id", "phase", "alignment_state",
 "objective_status", "reason", "clarification_count"}
```

### M2. 4 个核心指标的计算口径

MVP 定义稿 §11.1 的 4 个指标，直接基于 M1 事件计算：

| 指标 | 计算公式 |
|------|----------|
| 首个学习价值时间 | `ts(first_value_delivered) - ts(learning_unit.created)`，按卷聚合 p50 / p90 |
| 学习卷完成率 | `count(consolidated) / count(created)`，时间窗口 7d |
| 验收进入率 | `count(teach_entered) / count(created)` |
| 复用意愿 | `count(reuse_feedback.value=yes) / count(reuse_feedback)` |

辅助指标（adaptive alignment §12.1）也用同套事件直接算，不需要额外埋点。

### M3. 观测面板

[`web/observability.html`](../../web/observability.html) 已是 L3 时间线视图。新增一个 "Learning Units" tab，展示：

- 单卷时间轴（沿用现有 events viewer）
- 4 个核心指标 7d 聚合卡片
- 按 `alignment_reason` 分布的饼图（一阶段不强求，可放 P1）

---

## 六、验收（V 系列）

### V1. P0 必过项（MVP §12.1，五条全部成立才算 MVP 立住）

1. 用户能一句话说清「学习模式 vs Chat」的差别。 → 用户访谈 ≥ 3 人，开放式回答收敛。
2. 用户进入后，首轮能明显感到"在被教"。 → 首轮内容必含目标卡片 + 学习地图 + 第一段讲解三块。
3. 用户能走到一个明确的结束动作。 → `teach_entry_rate ≥ 60%`。
4. 用户结束后，知道自己学到了什么。 → 反馈卡片显示 mastered/gaps/next。
5. 用户愿意下次再次用它来学一个主题。 → `reuse_feedback.value=yes` 占比 ≥ 70%。

### V2. 失败信号（出现任一即视为 MVP 未立住，回头改）

- `forced_alignment_rate > 30%`（系统又频繁拦截了）
- 用户在 absorbing 主动切回 Chat 占比 > 20%
- `first_value_delivered` p50 反而比旧路径慢
- 用户开放反馈中 ≥ 1/3 把学习模式描述为"更复杂的 Chat"

### V3. 测试用例（基于现有测试结构扩展）

[`tests/test_learning_unit_store.py`](../../tests/test_learning_unit_store.py) / [`tests/test_learning_unit_api.py`](../../tests/test_learning_unit_api.py) 已存在，需补充：

- 新字段序列化往返
- 旧 JSON（含 `phase=aligning`）加载时投影迁移
- `should_run_alignment` 三档边界用例（清楚 / 略宽 / 模糊各 ≥ 2 例）
- 限流：第二次启动澄清被强制跳过
- `/align`、`/accept-assumption`、`/refine-objective` 三个端点的 happy path

集成测试：复用 [`tests/observability_asserts.py`](../../tests/observability_asserts.py)（L2 reusable assertions）断言事件序列。

---

## 七、与 adaptive alignment 设计稿的差异核对

设计稿与 MVP 定义稿在主体方向一致，几处需要明确选择：

| 议题 | adaptive alignment 设计 | MVP 定义稿 | 本任务清单选择 |
|------|--------------------------|------------|----------------|
| `phase` 是否含 `aligning` | 移除 | 未明确 | **移除**（跟设计稿） |
| `Ask` 触发档位 | 三档 A/B/C | 三档（清楚直讲 / 略宽边提示 / 模糊澄清 1 次） | **一致** |
| 目标卡片状态 | `working / refined / confirmed` | 未具体定义 | **跟设计稿** |
| 阻塞澄清次数 | 启动期 ≤ 1，中途建议 ≤ 2 | "允许 1 次短暂澄清" | **跟设计稿（更严格）** |
| 结束验收形态 | TEACH（题集） | 讲讲看 / 轻量题集 / 复述 | **TEACH 实现，UI 双入口** |
| 新增 API 端点 | `/align`、`/accept-assumption`、`/refine-objective` 或并入 advance | "提供一个不重的收窄机制" | **独立端点，语义更清晰** |
| 进度条文案 | `学习中 · 复述检验 · 已收束` | 未具体定义 | **跟设计稿** |

无矛盾。

---

## 八、复用 vs 新增 总览

| 模块 | 复用 | 需要改 | 全新 |
|------|------|--------|------|
| `LearningUnit` 数据模型 | UnitObjective / Concept / Tangent / TeachSession | phase 集合、转移规则 | 6 个 alignment / objective 字段 |
| Store / 持久化 | `LearningUnitStore` 文件 + lock 机制 | `load_all` 加投影迁移 | — |
| Product 调度 | `_prepare_learning_unit_turn` 骨架、`mode_service` | `create_learning_unit` 不再强制 ASK | `alignment_policy.py` / `should_run_alignment` |
| API | `_learning_unit_payload`、现有端点风格 | — | `/align`、`/accept-assumption`、`/refine-objective` |
| Runtime | 完全不动 | — | — |
| Observability | 事件存储、L3 viewer | `SessionEventType` 加新常量 | 12 个 `learning_unit.*` 事件 |
| 前端 | 消息流组件、SSE 解析、modal 样式 | 欢迎页扩第 4 张卡 | 学习卷视图、目标卡片、suggestion bar、TEACH UI、反馈卡片 |
| 测试 | `test_learning_unit_*.py`、`observability_asserts.py` | 扩用例 | `test_alignment_policy.py` |

**结论**：MVP 90% 是在现有骨架上扩展；唯一全新的产品件是「学习卷视图前端」与「alignment policy 判定」。

---

## 九、建议的实施顺序

按依赖关系，**串行 + 并行混合**：

1. **B1** 数据模型（包含旧数据迁移用例）—— 基石
2. **B2** Product 调度 + `alignment_policy` —— 决定行为
3. **M1** 事件类型新增 —— 让 B1/B2 写得出事件
4. **B4** 收窄动作 API —— 让前端有可调端点
5. 并行：
   - **B3** 首轮开场模板（后端）
   - **F1 / F2 / F3** 学习卷入口与首轮视图（前端）
6. **F4** suggestion bar + 收窄按钮（依赖 B2 + B4）
7. **B5** TEACH 链路打通（后端） + **F5** 结束验收 UI（前端）—— 可并行
8. **F6** SSE metadata 全链路串通（穿插上线）
9. **M2 / M3** 指标聚合与观测面板 —— 上线前最后一步
10. **V1 / V2 / V3** 灰度验收

预估 ≈ 2 个迭代（按周计 1.5-2 周），具体看 TEACH 现有完成度。

---

## 十、明确不在 MVP 范围

以下事项**有意延后**，发生需求时再单独立项，避免再次失焦：

- 多卷并行 / 跨卷关联 / 知识图谱
- 长期学习路径规划
- 复杂记忆主链的对外展示
- 题集生成的精细化策略（标签、难度梯度、教科书式覆盖）
- 概念抽取的产品化暴露（tangent notes 当前作为后端能力存在即可，不进 MVP UI）
- 学习卷的可观测视图深度交互（先复用 L3 viewer）
- 复用意愿之外更复杂的满意度问卷

这条清单与 MVP 定义稿 §九、§十三 P2 列表一致。

---

## 十一、下一步

本任务清单稳定后建议进入：

1. 在 [`docs/changes/2026-05-25-learning-mode-mvp-task-breakdown.md`](../changes/) 落一份 short changelog，标记任务清单生效。
2. 把 B1 + B2 + M1 拍进当前迭代，B1 在第一天就开 PR——它是其他所有任务的前置。
3. 若 B5（TEACH）的现有实现已经端到端可跑，可把 F5 提前并行，缩短关键路径。
