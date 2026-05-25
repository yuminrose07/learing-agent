# 技术设计文档：模式重构 —— 闲聊 vs. 学习卷

> **目标**：把当前 `Chat / Ask / Study` 三模式收口为「闲聊」与「学习卷」两条主路径，让模式边界与产品语义重新对齐。
> **设计原则**：模式属于 Product / Application 层编排；学习卷是顶层容器，其内部以阶段状态机推进；旧的 Persona（思路）覆层与本次重构正交，不动。
> **文档日期**：2026-05-25
> **实现状态**：需求已对齐，方案待评审，未开始实现。
> **取代**：本文档替换 [design-chat-ask-study-modes.md](design-chat-ask-study-modes.md) 中关于产品模式三分的部分；该旧文中关于 Runtime/Product 分层、单一 ReAct 内核、TurnExecutionKind 等基础设施约束**继续生效**。

---

## 一、背景：当前模式边界为什么模糊

当前线上有三个产品模式 `Chat / Ask / Study`：

- `Chat`：默认的通用对话。
- `Ask`：先反问、收口目标，再作答（真正的协议级模式，有 `ask_state` 状态机）。
- `Study`：定位为「深度学习」，但实现上并没有独立协议或状态机，本质只是一个元数据标签 + 一组 prompt 风格。

诊断结论：

1. 当前的三个模式混合了**两个正交的维度**：
   - **协议维度**（是否在回答前先对齐？是否要进入一个特定的多阶段流程？）
   - **风格维度**（语气、节奏、深度倾向）
   `Ask` 是协议级模式，`Chat` 是默认协议，但 `Study` 只是风格，没有自己的协议骨架——这导致用户分不清「Study 跟 Chat 到底有什么区别」。
2. 用户提出的核心诉求是「从知识输入到知识输出闭环」（典型例子：与某种思路对话学习概念，再在另一个场景下复述/输出，验证自己是否真的学到了）。当前的 `Study` 没有承载这条闭环。
3. Persona（哲人思路）已经独立成「思路」覆层，由 topbar 的小选择器控制，与本次模式重构不耦合，本文档不再展开。

---

## 二、需求对齐结果（来自 2026-05-25 的对话）

| 维度 | 决策 |
|------|------|
| **学习目标的定义路径** | MVP **仅做 AI 协助提炼**（沿用 ASK 的对齐反问能力，把问题收成一句目标）。同时**在数据模型与接口层为「从材料导入」预留扩展位**，不能写成死路。 |
| **学习卷的颗粒度** | **一卷一个明确概念**（例：RAG、注意力机制、Bellman 方程）。颗粒小、易闭环、Teach 阶段好验收。 |
| **闲聊与学习卷的穿越规则** | **单向升格**：闲聊可以一键升格为一卷学习；学习卷一旦开启，会专心走流程，不在同一窗口内插回闲聊。 |

这三条决策构成本文档的硬约束，后续实现不应偏离。

---

## 二.5 运行约束（第二轮对齐：状态机与 UI 行为）

针对状态机各阶段的可观察性、对齐节奏、阶段切换信号、吸收/验收判定，第二轮对齐补充以下 8 条硬约束。所有后续实现必须满足，违反需在 PR 描述里显式说明。

| # | 约束 | 实现含义 |
|---|------|----------|
| C1 | **轻感知**：用户看得到当前阶段，但**不显示**状态机术语 | 顶部细长进度条显示「**对齐目标 · 学习中 · 复述检验**」三段；不出现 aligning/absorbing/outputting 字样 |
| C2 | **入口决定意图**，不做意图分类 | 「学习」按钮 / 闲聊「升格」入口 = 唯一进入 aligning 的路径；不让 LLM 路由判断"这句要不要先对齐" |
| C3 | **对齐阶段硬上限 3 轮反问**，最后一轮强制收口 | 第 1-2 轮 AI 可反问（每轮 ≤ 2 个问题）；第 3 轮 AI 不得再问，必须给出一句话目标草稿 + 「确认开始 / 我改一下」按钮 |
| C4 | **阶段切换只信用户显式确认**，不信 LLM 自判 | 每次跨阶段都要走用户点击；系统可以**提示**用户该切了，但不自动切 |
| C5 | **"学完"由 TEACH 验收定义**，不由用户口头声明定义 | absorbing → consolidated **必经** TEACH；用户在 absorbing 阶段说"学完了"会被理解为"开始验收"，进入 outputting，不会跳过 |
| C6 | **用户坚持关卷时尊重**，但要留痕 | TEACH 期间用户选择关卷：consolidated 时写入 `verification_status: "skipped"` 标记；下次同主题再学时系统可提示"上次这卷没收完" |
| C7 | **吸收阶段无轮数硬指标**，靠概念清单做柔性提示 | 触发"准备好就讲讲看"提示的条件：`concept_list ≥ 5` **且** 最近 3 轮无新概念加入；或用户连续 2 轮提问明显重复同一面 |
| C8 | **新概念按与目标的语义距离分流** | 概念抽取时由 LLM 同时给出 0-1 的 relevance score；≥ 阈值 → `concept_list`；< 阈值 → `tangent_notes`。tangent_notes **在 absorbing/outputting 阶段静默累积，不打扰用户**；只在 `consolidated` 阶段（关卷收尾页 / 已结束学习卷卡片）暴露「📌 为它另开一卷」入口——与 P3 单卷模式保持一致 |
| C9 | **学习卷状态可持久化、可恢复** | `LearningUnit` 包含的所有运行时状态（phase、concept_list、tangent_notes、aligning_round、teach_state、TEACH 题集与答题记录）必须全部落库；用户关闭浏览器再回来时，前端能根据 phase 重建进度条、按钮、目标卡片三件套 |

这 9 条与 §二 的 3 条一样，是**需求层硬约束**，不是实现技巧。任何调整需要重新对齐。

---

## 三、目标架构总览

### 3.1 顶层概念

引入两个用户可见的入口：

- **闲聊（Chat）**：轻量、单轮或多轮自由对话；不预设目标，不进入任何状态机；与现状 `Chat` 模式行为一致。
- **学习卷（Learning Unit）**：顶层容器，包含一个明确的「学习目标」和一个内部阶段状态机；用户启动后，对话窗口会按阶段推进。

`Ask` **不再是顶层按钮**，而是作为学习卷启动时的「对齐子阶段」内化。
`Study` **作为顶层模式被移除**，其原本想表达的「深度学习场景」由学习卷 + 阶段状态机承载。

### 3.2 学习卷的阶段状态机

每一卷学习是一条线性管线，分四个阶段，由内部状态机驱动：

```
[aligning]  →  [absorbing]  →  [outputting]  →  [consolidated]
   对齐         吸收（学）       输出（讲）        固化（记）
```

| 阶段 | 用户视角 | 系统行为 | 出口条件 |
|------|----------|----------|----------|
| `aligning` | "我想学 X" → AI 反问 → 一句话目标 | 复用 ASK 的 single_pass + ask_state 协议；**反问上限 3 轮**（见 C3） | 用户点击「**确认开始**」按钮（C4） |
| `absorbing` | 与 AI 对谈式学习这个概念 | 普通 ReAct 对话；后台抽取「概念清单」并打 relevance score（见 C8） | 用户点击「**我想讲讲看**」（C4）；系统**只在 C7 条件下提示**，不自动切 |
| `outputting` | **反向问答**：AI 提问，用户作答；AI 给出针对性反馈 | 新增 TEACH 协议（详见 §3.4）；是 absorbing → consolidated 的**必经路径**（C5） | 所有概念验收完毕，或用户主动关卷（若有未验收概念，记 `verification_status="skipped"`，见 C6） |
| `consolidated` | 一卷收束，会话进入只读 | 把「目标 + 概念清单 + 用户输出片段 + verification_status」沉淀进 L2/L3 记忆 | 关卷即终态 |

阶段状态机由学习卷内部维护，对运行时（Runtime）只暴露当前阶段对应的协议类型（`ASK / CHAT / TEACH`）。这一层与现有的 `mode_service.build_turn_profile` 对接，**不需要改 Runtime 内核**。

### 3.3 学习目标的定义

MVP 路径：**AI 协助提炼**。

1. 用户在闲聊里聊到某话题，或在学习卷入口直接说"我想学 X"。
2. 系统进入 `aligning` 阶段，复用现有 ASK 协议反问。
3. 用户确认后，把这一句产生的话存为 `LearningUnit.objective.text`。

预留接口（不在 MVP 实现，但数据模型要兼容）：

```python
class LearningObjective(BaseModel):
    text: str                              # AI 协助提炼或用户手写的一句话目标
    source: Literal["ai_distilled",        # MVP 唯一实现
                    "user_written",        # 预留
                    "material_imported",   # 预留（从 PDF/链接/文本导入）
                    "promoted_from_chat"]  # 预留（从闲聊一键升格）
    source_ref: Optional[str] = None       # material_imported 时指向上传文件 id
                                           # promoted_from_chat 时指向 chat session 片段
```

API 层只暴露 `source="ai_distilled"`；其他枚举值在 Schema 中**已经存在**，但服务端拒绝接受——这样未来加新路径时**只改实现，不改 schema**。

**对齐节奏（落实 C3）**：

- `ask_state` 内部维护一个 `aligning_round` 计数（从 1 开始）。
- 第 1-2 轮：AI 可以反问，**每轮 prompt 限制最多 2 个问题**（由系统提示词显式约束）。
- 第 3 轮：系统提示词切换为「**禁止再反问**，请基于已有信息给出一句话目标草稿」；前端在收到第 3 轮回复时**强制渲染**「确认开始 / 我改一下」两个按钮，不再让用户单纯输入文字。
- 用户在任何一轮都可以直接点「确认开始」跳过剩余反问。

### 3.4 TEACH 协议（反向问答）

TEACH 是这次重构唯一**新增**的协议级模式。

设计要点：

- 与 ASK 一样，是 `single_pass` 模式（AI 一回合只出题，不要边出题边作答）。
- 系统提示词强制角色互换：AI 是出题人，用户是被考者；AI 不主动给答案，除非用户作答之后给反馈。
- **出题数量**：默认 **Top-3 概念**（按 `concept_list.relevance` 降序选）。前端额外提供「再多问 3 个」按钮，用户主动点了才追加。
- **题型组合（落实"减少打字摩擦"的产品原则）**：默认 1 个 TEACH session 出 3 题，**2 道选择题 + 1 道简答题**。
  - **选择题**：4 个选项，1 正 3 干扰；前端渲染为单选按钮组，用户点选即提交，**全程不需要打字**。
  - **简答题**：开放回答，多行文本框输入。
  - **排列**：`[SA(Top-1 concept)] → [MC(Top-2)] → [MC(Top-3)]`。简答题放在 relevance 最高的概念上（最该被验收的那个），让用户在精力最足时面对它；选择题压在后面起"巩固 + 收尾"作用。
  - 每题独立提交、独立反馈，不在一屏内一次性发 3 题压垮用户。
- **评分机制（落实 P1）**：
  - **MC**：精确匹配选项标签，**零 LLM 调用**。出题阶段就把"哪个选项是正确答案"固化在题目结构里。
  - **SA**：LLM-as-judge——把用户回答 + 该 ConceptItem 的 `summary + examples` 喂给评判模型（默认 Haiku，见 P4），要求输出 `{verdict: "passed" | "needs_review", reason: str}` 结构化 JSON。MVP 阶段不引入复杂 rubric，**预留 rubric 字段**便于后续升级到带 ConceptItem 级 rubric 的 P1(b) 方案。
  - 每题独立判定；3 题中 ≥ 2 道 `passed` 视为该 session **整体通过**，UI 给"恭喜过关 / 关卷收尾"的提示；否则按 §3.4 失败处理走"回去再聊一聊" / "关卷留痕"二选一。
- **题集生成时机**：用户点击「我想讲讲看」进入 TEACH 时，系统**立即生成完整 3 题题集**（一次 LLM 调用，结构化输出），之后用户答题阶段不再依赖模型生成题目，只在 SA 评分时调一次模型。这样 TEACH 阶段的延迟可预测、API 消耗可控。
- 内部状态机 `teach_state`（与 `ask_state` 同构）：
  - `generating`：题集生成中（短暂态）
  - `queued`：题集已生成，待用户开始
  - `prompted`：当前题已出，等用户答
  - `evaluating`：评估用户答案（MC 同步即返回，SA 异步等模型）
  - `passed` / `needs_review`：单题验收状态
- 单 TEACH session 终态：3 道题全部判完，并由用户点击「关卷收尾」或「回去再聊一聊这块」决定下一步（详见下方"必经性与失败处理"）。

**必经性与失败处理（落实 C5/C6）**：

- absorbing → consolidated **没有直达路径**，必须经过 outputting；用户在 absorbing 阶段说「学完了」**等价于**点击「我想讲讲看」，进入 TEACH。
- TEACH 期间用户答错或答不上时，AI 给反馈后，**前端在该消息下方渲染两个出口按钮**：
  - 「**回去再聊一聊这块**」→ phase 回到 `absorbing`，被问错的那条概念 `status` 标为 `needs_review`，`concept_list` 不丢失。
  - 「**我先关卷，下次再说**」→ phase 跃迁到 `consolidated`，所有未通过/未问到的概念在 consolidated 写入时携带 `verification_status="skipped"` 标记。
- 阶段图因此实际包含一条逆向边：
  ```
  [aligning] → [absorbing] ⇄ [outputting] → [consolidated]
  ```
  这条逆向边是 TEACH 唯一允许的回退，**不存在** outputting → aligning 或 consolidated → 任意前态。

### 3.5 概念清单：连接 `absorbing` 与 `outputting` 的胶水

在 `absorbing` 阶段，系统在每一轮回答之后做一次**轻量概念抽取**（不阻塞主回答，可以走异步或同回合 tail-task），把回答中出现的关键概念、定义、例子追加到学习卷的 `concept_list`：

```python
class ConceptItem(BaseModel):
    id: str
    name: str                              # "注意力机制"
    summary: str                           # 一句话定义，作为 TEACH 出题与判分的参考
    examples: list[str] = []
    relevance: float                       # 0-1，由抽取 LLM 同时给出，对应 objective 的相关度
    status: Literal["new", "covered",      # 还没问过 / 已 Teach 通过 / 待复习
                    "needs_review"] = "new"
```

抽取出的每条概念按 relevance 分流（落实 C8）：

| relevance 区间 | 落地位置 | UI 行为 |
|----------------|---------|--------|
| ≥ 0.6（自然延伸） | `LearningUnit.concept_list` | 不打扰用户，参与 TEACH 出题 |
| < 0.6（跑题信号） | `LearningUnit.tangent_notes`（一个轻量字段 `list[TangentNote]`） | absorbing/outputting 阶段**静默累积、不暴露入口**；只在进入 `consolidated` 后（关卷收尾页 + 已结束学习卷卡片）展示「📌 这个值得单独学一卷」入口（与 P3 单卷模式一致） |

阈值 `0.6` 是经验起步值，需要在 Step 2 实现后通过实际对话样本调整；放在配置里，不写死。

`outputting` 阶段直接遍历 `concept_list`（不含 tangent_notes）出题。这是这次重构里最有产品价值、也最容易做错的一环——MVP 阶段抽取规则要克制（宁可漏，不要造），后续再迭代。

### 3.6 闲聊→学习卷的单向升格

在闲聊窗口里，对任意一条用户消息或一段对话，提供一个轻量入口（按钮或斜杠命令）：

- 用户点击后，系统把当前对话片段作为「目标素材」喂给 ASK 协议，进入 `aligning` 阶段。
- 升格会**创建一条新的学习卷会话**（不污染原闲聊会话）。
- 原闲聊会话保持不变，用户可以继续闲聊。

学习卷会话**不允许反向跳回闲聊**。如果用户在学习卷中途想闲聊，需自己切到闲聊窗口（新开或回到原会话）。

### 3.7 用户感知与 UI 信号面板

落实 C1/C4/C7，集中说明用户在一卷学习中能看到的所有界面元素：

| 元素 | 出现时机 | 内容 |
|------|----------|------|
| **顶部进度条** | 进入学习卷会话起 | 三段：「**对齐目标 · 学习中 · 复述检验**」，当前段高亮。不出现 aligning/absorbing/outputting 字样（C1） |
| **手动推进按钮** | 始终在进度条旁可见 | 「确认开始」（aligning 第 3 轮强制出现）/「我想讲讲看」（absorbing 阶段始终可点）/「关卷收尾」（outputting 阶段始终可点）。点击 = 唯一的阶段切换信号（C4） |
| **柔性提示气泡** | C7 条件命中时 | 一个不打断当前回答的小气泡，文案如「准备好就来讲讲看，看看哪里还没透」。可关闭；同一卷内同一类提示最多出现 2 次 |
| **目标卡片** | confirm-objective 之后始终顶置 | 一句话学习目标 + 「我要改一下目标」入口（点击会让你确认是否要废卷重开，不允许中途无痕改目标） |
| **tangent 入口** | 仅在进入 `consolidated` 后显示 | 关卷收尾页 + 已结束学习卷卡片上展示折叠角标，hover 展开列出跑题概念 + 「为它另开一卷」按钮（C8）；absorbing/outputting 期间**不显示**，避免与 P3 单卷模式冲突 |
| **TEACH 题目卡片** | TEACH 阶段每出 1 题 | 选择题：题干 + 4 个单选按钮 + 「提交」按钮（点选即可，无需打字）。简答题：题干 + 多行文本框 + 「提交」按钮。3 题以题集形式逐题串行展示，每题独立提交、独立反馈，提交后进入下一题 |
| **TEACH 题集进度小点** | TEACH 阶段始终在题目上方显示 | 3 个小圆点表示题集进度（待答 / 当前 / 已答正确 / 已答错误），让用户感知"还剩几题"，但不暴露具体打分 |
| **TEACH 出口按钮** | TEACH 反馈消息下方 | 「回去再聊一聊这块」/「我先关卷，下次再说」——见 §3.4 |

不出现的元素（明确避免）：

- 不显示 `aligning_round` 计数（用户感知是"AI 又问了一句"，不是"还有 2 轮"）。
- 不显示概念清单的 `relevance` 分数。
- 不显示概念清单的完整内容（避免变成应试模式让用户产生背诵焦虑）；TEACH 阶段才让用户看到具体概念。

### 3.8 数据模型汇总

下列 Pydantic 模型构成 `LearningUnit` 的完整运行时状态，所有字段必须落库以满足 C9（断线续传）。**这是 Step 1 实现的合同基线**——若实现期需要新增字段，必须回到本文档登记后再动代码。

> **命名提示**：实现代码中 `LearningUnit.objective` 字段的类型叫 **`UnitObjective`**，不是 `LearningObjective`。这是为了与 `learning_agent/ai/models.py` 中独立的顶层目标实体 `LearningObjective`（用于 `/objectives` 端点）显式区分——两者 MVP 不互相引用，命名上也不复用。文档下面的伪代码沿用 `LearningObjective` 是为了阅读上的连贯，但请以代码中的 `UnitObjective` 为准。

```python
# 顶层容器
class LearningUnit(BaseModel):
    id: str
    session_id: str                                # 与底层会话 1:1（会话表里同步加 learning_unit_id 反向引用）
    objective: LearningObjective
    phase: Literal["aligning", "absorbing",
                   "outputting", "consolidated"]
    concept_list: list[ConceptItem] = []
    tangent_notes: list[TangentNote] = []
    aligning_round: int = 1                        # ASK 反问轮次 (1-3)，C3
    teach_session: Optional[TeachSession] = None   # 进入 TEACH 时创建；保留以便续传 (C9)
    verification_status: Optional[
        Literal["passed", "skipped"]] = None       # consolidated 时写入，C6
    created_at: datetime
    updated_at: datetime


class LearningObjective(BaseModel):
    text: str
    source: Literal["ai_distilled",                # MVP 唯一支持
                    "user_written",                # 预留
                    "material_imported",           # 预留
                    "promoted_from_chat"]          # 预留
    source_ref: Optional[str] = None               # material 时指向上传文件 id；promoted 时指向 chat 片段
    confirmed: bool = False                        # 用户点「确认开始」后置 True（C4）


class ConceptItem(BaseModel):
    id: str
    name: str                                      # "注意力机制"
    summary: str                                   # 一句话定义，SA 评分参考材料
    examples: list[str] = []
    relevance: float                               # 0-1，抽取 LLM 同时输出 (C8)
    status: Literal["new", "covered",
                    "needs_review"] = "new"        # 只在 TEACH 阶段流转


class TangentNote(BaseModel):
    id: str
    name: str
    summary: str
    relevance: float                               # < 0.6 阈值
    detected_at: datetime


class TeachSession(BaseModel):
    id: str
    questions: list[TeachQuestion]                 # 长度 = 3（默认 Top-3）
    current_index: int = 0                         # 当前在答第几题（0-2）
    state: Literal["generating", "queued", "prompted",
                   "evaluating", "passed", "needs_review"]
    aggregate_passed: Optional[bool] = None        # ≥ 2/3 → True；session 结束时写入
    created_at: datetime
    completed_at: Optional[datetime] = None


class TeachQuestion(BaseModel):
    id: str
    concept_id: str                                # 指向 ConceptItem.id
    kind: Literal["mc", "sa"]
    stem: str                                      # 题干
    # MC 专有
    choices: Optional[list[str]] = None            # 4 个
    correct_index: Optional[int] = None            # 0-3
    # SA 专有
    rubric: Optional[str] = None                   # 预留，MVP 可空（升级 P1(b) 时填）
    # 用户答案 & 评分
    user_answer: Optional[str] = None              # MC 存选中索引的字符串形式；SA 存原文
    verdict: Optional[Literal["passed", "needs_review"]] = None
    judge_reason: Optional[str] = None             # SA 的 LLM 反馈原文；MC 为空
```

底层会话表（chat session）的 schema 同步加一个反向引用字段：

```python
class ChatSession(BaseModel):
    # ... 现有字段保持不变
    learning_unit_id: Optional[str] = None         # 若为闲聊会话则为 None
```

**持久化策略**：

- 学习卷会话每次 phase 跃迁、concept_list 追加、TeachQuestion 提交后，整棵 LearningUnit JSON 写库（增量 patch 而非全量覆盖）。
- 前端 `GET /learning-units/{id}` 拉取完整状态，根据 `phase` + `teach_session.state` 派生当前 UI（进度条位置、按钮可用性、目标卡片、题目卡片）。
- TeachSession 一旦生成即锁定（详见 §8.4），即使后续 absorbing 又追加了新概念也不重生题集。

---

## 四、前后端契约变化

### 4.1 前端

- 主入口工具栏只保留两个按钮：「闲聊」、「学习」。
- 「学习」按钮点击：弹一个轻量启动器，要求填或说一个学习目标 → 进入 `aligning` 阶段。
- 学习卷会话的顶部要显示当前阶段（`对齐 / 吸收 / 输出 / 固化`），并提供"进入下一阶段"的手动按钮（即使有自动出口条件，也保留人工干预）。
- 闲聊会话顶部新增「升格为学习卷」入口（icon + 文案，不抢眼但找得到）。
- 「思路」（Persona）选择器保留在 topbar，与新模式正交，不动。

### 4.2 后端

- `AgentMode` 枚举：
  - 保留 `CHAT`。
  - 移除 `ASK`、`STUDY` 作为顶层模式（ASK 协议本身保留，仅作为学习卷子阶段使用；STUDY 整段删除）。
  - 新增 `TEACH`。
- 新增 `LearningUnit` 模型与状态机；学习卷会话的 `mode` 字段在运行时根据 `phase` 派生：
  | phase | effective mode |
  |-------|----------------|
  | aligning | ASK |
  | absorbing | CHAT |
  | outputting | TEACH |
  | consolidated | （只读，不再调模型） |
- 新增 REST 端点（草案）：
  - `POST /learning-units` 创建一卷，body = `{ "seed_text": "...", "source": "ai_distilled" }`，返回 unit_id + 初始 phase=aligning + 一个空 objective。
  - `POST /learning-units/{id}/confirm-objective` 确认目标，phase 进入 `absorbing`。
  - `POST /learning-units/{id}/advance` 手动推进阶段（带 `target_phase` 参数）。
  - `GET /learning-units/{id}` 读卷（含 objective、concept_list、phase）。
  - `POST /chat-sessions/{id}/promote-to-learning-unit` 闲聊升格入口。
  - 现有 `/sessions` API 中，学习卷会话沿用相同的会话存储，仅多一个 `learning_unit_id` 关联字段；前端按字段判断走哪种 UI。
- `mode_service.build_turn_profile` 扩展，理解 `TEACH` 协议；TEACH 也走 `TurnExecutionKind.SINGLE_PASS`（与 ASK 同构）。
- 现有 `/objectives` 端点：评估是直接复用为学习卷的 objective 持久化，还是另起一张表。**建议**：复用 `Objective` 概念，但语义收紧到「一卷一目标」；不重新发明轮子。在 §六实现计划里会再确认。

### 4.3 数据迁移

- 旧会话里 `mode == "study"` 的历史会话：保留只读，前端把它当 `chat` 展示，不再可写。
- 旧会话里 `mode == "ask"` 且仍在 `aligning` 状态的会话：MVP 直接归类为「废弃中」，前端给一个提示让用户决定续做还是关掉。（讨论时如果用户接受，也可以做一键转成学习卷，但这是 nice-to-have，不进 MVP。）

---

## 五、不在本次重构范围内

为了防止范围漂移，以下显式不做：

- 不动 Runtime 内核（agent_loop、compaction、event log 等）。
- 不动 Persona 覆层（哲人思路）系统。
- 不重做记忆系统的 L1/L2/L3 架构；TEACH/consolidated 阶段只是按现有接口写入。
- 不做「学习卷之间的依赖图 / 知识图谱」。每一卷是独立单元，关联留给未来。
- 不做「从材料导入」「用户手写目标」「闲聊升格」之外的目标定义路径——但数据模型层为它们留好枚举位。
- 不做学习卷的协作 / 分享 / 复读他人卷。

---

## 六、分阶段实施计划

把工作拆成三步走，每一步都要可独立验证、可回滚。

### Step 1：协议层（后端，无 UI 改动）

1. 在 `learning_agent/learning_agent/mode_service.py` 增加 `TurnExecutionKind` 对 TEACH 的支持，定义 TEACH 的 system prompt 骨架。
2. 在 `learning_agent/ai` 下定义 `LearningUnit`、`LearningObjective`、`ConceptItem`、`teach_state` 数据结构（Pydantic 模型）。
3. 在 `learning_agent/learning_agent/main.py` 的会话调度里，识别 `learning_unit_id` 字段，根据 phase 选择 effective mode。
4. 测试：`tests/test_mode_layering.py` 扩展，加 TEACH 协议的最小 happy path。

**验收**：能从测试代码手动驱动一卷从 aligning → absorbing → outputting → consolidated 全部走通；前端零改动；旧的 Chat / Ask 用例全绿。

### Step 2：REST 接口 + 概念抽取

1. 在 `web/web_server.py` 加 `/learning-units` 系列端点。
2. 概念抽取实现：在 `absorbing` 阶段的回合后插入一次轻量 LLM 调用，把概念追加到 `concept_list`；走异步任务，不阻塞主流。
3. 测试：API 集成测试 + 概念抽取的 unit test（mock LLM 返回固定结构）。

**验收**：从 curl 可以完整跑通一卷；前端仍然零改动。

### Step 3：前端 UI

1. 工具栏从三按钮（Chat / Ask / Study）压成两按钮（闲聊 / 学习）。
2. 学习启动器（一个轻量弹层，沿用现有 modal 样式）。
3. 学习卷会话视图：phase 进度条 + 手动推进按钮 + TEACH 阶段的特殊样式。
4. 闲聊会话的「升格」入口。
5. 旧 `mode == "ask" | "study"` 会话的兼容降级展示。

**验收**：走完完整学习卷闭环；闲聊→升格→学习卷链路通；浏览器手测三种思路（持久化、刷新、跨刷新）。

---

## 七、测试策略

| 层 | 测什么 | 关键用例 |
|----|--------|----------|
| Unit | mode_service / 状态机 / 概念抽取 | TEACH profile 渲染、phase 跃迁、单元状态合法转移、非法转移被拒 |
| Integration | REST 端点 + 持久化 | 完整一卷的生命周期、升格创建独立会话、旧 ASK 会话兼容只读 |
| E2E（手测） | 浏览器端 UI | 两按钮切换、启动器输入→对齐反问→吸收对谈→TEACH 出题→关卷 |
| 回归 | 现有用例 | `tests/test_mode_layering.py` 现有 8 个用例必须全绿；`tests/test_web_static_app.js` 渲染相关用例不被新 UI 改坏 |

---

## 八、需要再次确认的开放点

### 8.1 第二轮对齐后已收口（仅留作变更记录）

- **概念抽取的触发时机**：已收口为「每回合后异步抽取一次」（见 §3.5），柔性提示的触发条件由 C7 给出。
- **学习卷与现有 Objective 的关系**：倾向于**复用** `Objective` 表，给它加一个可选的 `learning_unit_id` 反向引用。Step 1 实现时确认。

### 8.2 第二轮对齐后新增开放点 → 已收口

通读补完后产生的 5 个开放点 P1-P5 在 2026-05-25 全部敲定：

| # | 决策 | 落地位置 |
|---|------|---------|
| P1 | **LLM-as-judge 起步**；只用于简答题；选择题走精确匹配。预留 rubric 字段以便后续升级到带 ConceptItem 级 rubric 的方案。 | §3.4 评分机制 |
| P2 | **Top-3 by relevance**，题型组合 **2 道选择题 + 1 道简答题**（SA 放在 Top-1 概念）；前端「再多问 3 个」按钮作为追加入口。 | §3.4 出题数量 / 题型组合 |
| P3 | MVP **单卷模式**：同一时刻至多 1 个非 consolidated 学习卷；想开新卷需先关旧卷。 | §四 后端契约（Step 2 实现） |
| P4 | 概念抽取 + SA 判分默认使用 **Haiku 4.5**；主对话维持现有模型选择。质量不达标时再升级。 | §3.4 / §3.5 |
| P5 | MVP **冷启动**，不跨卷贯通；跨卷概念检索作为独立后续项目，不进本次重构。 | §五 范围外 |

### 8.3 实现细节级决策 → 已收口

四项实现细节也在第二轮对齐中按推荐方案敲定：

| 细节 | 决策 |
|------|------|
| TEACH 答错后追问几次再判 `needs_review` | **固定 1 次反问机会**，不再追加，避免无限循环 |
| 「我要改一下目标」入口 | **提供**，但点击触发"废卷重开"确认（清空 concept_list + 回到 aligning），不允许中途无痕换目标 |
| TEACH 中「我不会，再讲讲」 | 视为隐式「回去再聊一聊这块」点击，phase 回到 absorbing 并保留当前题集进度。**MVP 用显式按钮触发**，不做自然语言意图识别 |
| absorbing 已讨论 concept 的 `status` 流转 | 保持 `new`；`status` 字段**只在 TEACH 阶段流转**，抽取阶段不维护额外状态 |

### 8.4 实现期才需要再观察的二阶问题（仅作记录）

实现过程中可能浮现、需要靠真实样本回看的二阶问题，**不进 MVP 范围**：

- **MC 干扰项质量**：干扰项不够"有迷惑性"会导致用户随便选都对，反向稀释验收信号。检测：观察过关率超过 95% 的卷做人工抽查。
- **SA 打分宽严**：LLM-as-judge 可能过于宽松，把"语义相近但漏关键点"也判成 passed。检测：评分输出保留 `reason`，事后审 reason 与 verdict 的一致性。
- **relevance 阈值 0.6 的真实分布**：是否漏掉重要概念、或把跑题塞进了主清单。需要在实际对话样本上调参，所以放在配置里、不写死（已在 §3.5 说明）。
- **概念抽取的去重**：用户多轮讨论同一概念时如何合并而不是重复入清单。简单做法：抽取时把已有 `concept_list` 喂进 prompt 让模型自己判"新增 vs 合并"，留待 Step 2 实现时设计。
- **TEACH 题集时效**：题集一旦生成即锁定，直到用户主动关卷或主动「重生题集」。中断很久后回来不重新生成，避免用户感到"我离开一下题怎么变了"。如果在 absorbing 又追加了新概念，新概念会进入下一轮 TEACH（用户点「再多问 3 个」）而不是顶替当前题集。
- **SA 答案前端校验**：MVP 强制 `≥ 5 个非空白字符` 才允许提交；超过 1000 字给柔性提示但不阻断。极短答案直接拒绝提交，避免触发不必要的 LLM-as-judge 调用。
- **题集生成的 JSON 稳定性**：题集生成走结构化输出（用 `TeachQuestion` 列表作为响应 schema 强约束）；Haiku 若返回不合 schema，retry 1 次仍失败则 fallback 到主模型重生。错误日志要记下 schema violation 的具体字段，作为后续模型选型的数据点。

---

## 九、变更记录

- 2026-05-25（第四轮对齐 / 定稿）：新增 §3.8「数据模型汇总」给出 LearningUnit / LearningObjective / ConceptItem / TangentNote / TeachSession / TeachQuestion 完整 Pydantic 模型 + ChatSession 反向引用 + 持久化策略；修正 C8/§3.5/§3.7 中 tangent 入口的显示时机为「仅在 consolidated 后暴露」以解决与 P3 单卷模式的冲突；§8.4 追加 3 条实现期注意事项（题集时效、SA 前端校验、JSON 稳定性 fallback）。**本版冻结为 Step 1 实现基线**。
- 2026-05-25（第三轮对齐）：收口 P1-P5 与 §8.3 全部实现细节。新增 C9（学习卷状态持久化与可恢复）；§3.4 大幅扩写：明确 Top-3 by relevance、`[SA-Top1, MC-Top2, MC-Top3]` 题型组合、MC 零调用 + SA 用 LLM-as-judge (Haiku) 的评分机制、题集前置生成策略、`generating` 状态加入 `teach_state`；§3.7 新增 TEACH 题目卡片与题集进度小点的渲染规则；§8.2/8.3 转为"已收口"决策表；新增 §8.4 记录将来需要靠真实样本回看的二阶问题。
- 2026-05-25（第二轮对齐）：补 §二.5（C1-C8 八条运行约束）；改 §3.2 阶段出口条件为显式确认；§3.3 加 3 轮反问硬上限；§3.4 加 TEACH 必经性与逆向回退边；§3.5 加 relevance 分流；新增 §3.7「用户感知与 UI 信号面板」；重写 §八，关闭旧 2 项，新增 P1-P5 开放点 + 4 项实现细节。
- 2026-05-25（初稿）：基于当日首轮对齐对话产出。取代旧的 `Chat / Ask / Study` 三模式设计。
