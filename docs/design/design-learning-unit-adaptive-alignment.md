# 技术设计文档：学习卷自适应对齐（Adaptive Alignment）

> **目标**：取消学习卷默认强制 `ASK` 起步，改为“默认先学、必要时再对齐”的自适应方案，降低学习摩擦，同时保留收窄目标与纠偏能力。
> **设计原则**：对齐能力保留，但从“固定阶段”降级为“按需协议”；产品层决定何时触发对齐，Runtime 只消费当前轮的执行配置。
> **文档日期**：2026-05-25
> **实现状态**：方案定稿，待评审，未开始实现。
> **依赖文档**：`design-modes-refactor-chat-vs-learning-unit.md`
> **取代范围**：本文取代 `design-modes-refactor-chat-vs-learning-unit.md` 中“学习卷必须先经过 `aligning -> ASK` 阶段”的约束；其余关于 Learning Unit、TEACH、概念抽取、持久化与前后端契约的设计继续有效。

---

## 一、问题定义

### 1.1 当前方案的问题不在 `ASK` 能力本身，而在“强制前置”

当前学习卷启动逻辑是：

```text
用户进入学习卷
  -> phase = aligning
  -> effective_mode = ASK
  -> 先反问、先确认
  -> 用户确认后才能开始真正学习
```

这套设计有一个正确出发点：学习卷需要目标，不应一上来就漫无边际。

但真实体验暴露出 4 个产品问题：

- **价值到达太慢**：用户已经明确说了“我想学这个项目的 compaction 流程”，系统仍然先追问，第一轮拿不到实质内容。
- **学习心流被打断**：用户进入学习模式，本质期待“开始学”，而不是再次被迫走一轮需求澄清。
- **对齐成本固定收取**：不管目标是否已经足够清晰，系统都先收一笔 `ASK` 成本，这对高频学习型用户尤其刺耳。
- **产品叙事错位**：`ASK` 更像一个能力或协议，而不是学习卷必须显露给用户的第一阶段。

### 1.2 用户真正反感的是“默认被拦住”，不是“系统偶尔收窄目标”

用户体验反馈里最关键的一点，不是“永远不要问问题”，而是：

- 目标已经够清楚时，系统不该再拦。
- 目标略宽时，系统应该先给一个可工作的切口，而不是把用户拽回澄清模式。
- 只有在完全无法开学时，系统才应该短暂拉起对齐动作。

因此，产品决策不应该是“删掉 Ask”，而应该是：

> **保留对齐能力，取消强制 Ask 阶段。**

### 1.3 这次调整的本质

这不是一句 prompt 调整，而是一次产品语义纠偏：

- 旧语义：学习卷是“先对齐，再学习”。
- 新语义：学习卷是“先开始学习；若目标不稳，再在过程中轻量收窄”。

这意味着 `ASK` 的角色要从：

- 顶层可感知阶段

变成：

- 学习卷内部的一种临时协议能力

---

## 二、结论摘要

本次方案采用以下 6 条硬决策：

1. **学习卷不再默认进入 `aligning` 固定阶段。**
2. **学习卷创建后默认直接进入 `absorbing`，首轮优先给用户实质学习内容。**
3. **`ASK` 保留，但只在“需要收窄目标”时按需触发，不再作为固定起步流程。**
4. **对齐状态不再等同于学习卷主阶段，而是成为独立的 `alignment_state`。**
5. **对齐分三档：直接开学、非阻塞建议、短暂阻塞澄清。默认走前两档，第三档必须克制。**
6. **Product/Application 层负责判断是否触发对齐；Runtime 继续只消费 `CHAT / ASK / TEACH` 的单轮配置。**

一句话概括：

> **学习卷的主链是 `学 -> 讲 -> 收束`；对齐只是旁路，不再霸占入口。**

---

## 三、目标与非目标

### 3.1 目标

| 目标 | 说明 |
|------|------|
| **降低启动摩擦** | 用户一进入学习卷，尽快拿到第一段有价值的解释、框架或学习路径 |
| **保留纠偏能力** | 目标过宽、过乱、过散时，系统仍然能把学习目标收窄 |
| **保持分层清晰** | 对齐判断继续放在 Product/Application 层，不把产品语义塞回 Runtime |
| **兼容现有协议** | `ASK` 和 `TEACH` 协议保留，不推翻现有 `TurnExecutionProfile` 与 `mode_service` |
| **便于观测和评测** | 要能量化“少问了多少废问题”“更快进入学习”“是否更容易完成整卷” |

### 3.2 非目标

本次不做以下事情：

- 不删除通用 `Ask` 模式本身。
- 不重写 `TEACH` 设计。
- 不同时重做学习卷的概念抽取、记忆写入、题集生成策略。
- 不做复杂的意图分类器或多模型路由器。
- 不在第一版里引入自动课程规划或多卷编排。

---

## 四、新的产品心智模型

### 4.1 顶层入口不变，学习卷内部语义变化

用户侧仍然只理解两个主入口：

- **闲聊（Chat）**
- **学习卷（Learning Unit）**

变化发生在学习卷内部：

- 旧方案：学习卷启动时必定先进入 `aligning`
- 新方案：学习卷启动时默认进入 `absorbing`，但携带一个独立的“对齐状态”

### 4.2 新的学习卷主链

新的学习卷主链调整为：

```text
[absorbing]  ⇄  [outputting]  ->  [consolidated]
   学             讲 / 验收         收束
```

对齐不再是主链阶段，而是一个横切能力：

```text
alignment_state = idle | suggested | active | resolved | skipped
```

其含义如下：

| 状态 | 含义 | 是否阻塞学习主链 |
|------|------|------------------|
| `idle` | 当前不需要额外对齐 | 否 |
| `suggested` | 系统认为目标可再收窄，但不阻塞 | 否 |
| `active` | 当前轮需要执行一次短暂对齐动作 | 是，但只阻塞当前轮，不应演变成长流程 |
| `resolved` | 对齐已经完成，目标已被收窄或确认 | 否 |
| `skipped` | 用户显式选择“先按当前理解学下去” | 否 |

### 4.3 新模型里的 `ASK` 是“工具”，不是“楼层”

在这个模型里：

- `absorbing / outputting / consolidated` 是学习卷的主阶段。
- `ASK` 是系统在某些轮次里调用的一种协议能力。

也就是说：

- 用户是在“学习卷里”
- 某一轮系统可能“用 Ask 的方式短暂收窄”
- 但用户不需要被拖回一个持续数轮的“对齐楼层”

这比当前固定 `aligning -> ASK` 更符合真实心理模型。

---

## 五、启动策略：三档自适应对齐

### 5.1 核心原则

学习卷创建后，系统先做一次轻量判断：

> 当前输入是否已经足以开始教学？

判断结果不决定“能不能开始学习卷”，而决定“第一轮该怎么开始”。

### 5.2 三档策略

| 档位 | 触发条件 | 系统行为 | 用户感受 |
|------|----------|----------|----------|
| **A. 直接开学** | 目标已足够清晰 | 直接进入 `absorbing`，给出结构化开场学习内容 | “一进来就开始学了” |
| **B. 非阻塞建议** | 目标可学，但略宽或略散 | 先给内容，同时附一个轻量“建议收窄”入口 | “系统没拦我，但提醒我可以更聚焦” |
| **C. 短暂阻塞澄清** | 当前输入不足以形成可执行学习目标 | 当前轮临时走 `ASK`，只做 1 次聚焦澄清 | “确实太模糊了，先补一句就继续” |

优先级规则：

- 默认优先 A。
- 不满足 A 时优先 B。
- 只有 B 也无法成立时，才进入 C。

### 5.3 各档位的具体判定

#### A. 直接开学

满足以下任一组条件时，直接进入学习：

- 输入里已明确一个概念或模块，且有明显学习意图。
- 输入里已经带了材料范围、代码对象或具体文件主题。
- 输入里已明确“我想学什么 + 想学到什么程度”中的至少一项，并且另一项可以由系统合理假设。

示例：

- “带我理解这个项目里 compaction 的完整流程。”
- “我想学 `session_projection` 是怎么靠 JSONL replay 重建会话状态的。”
- “帮我从 Product 和 Runtime 分层的角度，讲清楚 `LearningAgentSystem` 在做什么。”

系统行为：

- 直接给出“学习地图 + 第一段解释 + 建议切口”。
- 顶部显示一个“工作目标卡片”，但标记为“当前理解”而不是“已最终确认”。

#### B. 非阻塞建议

输入已经可以学，但存在明显宽泛风险时，系统不阻塞，只提示：

- 目标太大：如“教我整个项目”
- 目标有主轴，但输出期待不明：如“我想学 compaction”
- 目标混了多个层次：如“顺便也讲下 memory、session、web UI 怎么串”

系统行为：

- 先正常进入 `absorbing`。
- 在目标卡片下方展示一条轻量提示：
  - “当前我会先按 `X` 这个切口展开；如果你想更聚焦，我可以帮你收窄成一卷。”
- 提供按钮：
  - `先按这个方向学`
  - `帮我收窄目标`

这类提示不能抢夺主回答，也不能变成首屏主内容。

#### C. 短暂阻塞澄清

只有以下情形才允许短暂阻塞：

- 用户没有给出可学习对象，只说“教我这个”“讲讲这个项目吧”
- 输入同时包含多个互相竞争的主目标，系统无法判断先学哪个
- 用户要求的学习目标内部明显矛盾
- 系统无法判断当前是想学概念、做代码 walkthrough，还是做任务执行

系统行为约束：

- 只允许**一次**聚焦澄清轮。
- 每次最多问 **1 个主问题 + 2 个可选按钮**。
- 澄清成功后，下一轮必须直接进入 `absorbing`。
- 不允许再展开成旧式多轮 `ask_state` 拉锯战。

一句话原则：

> **阻塞澄清只在“完全无法开学”时触发，而不是在“还能更完美”时触发。**

---

## 六、对话行为设计

### 6.1 进入学习卷后的首轮行为

新的首轮默认模板应改为：

1. 先给出系统当前理解的学习目标。
2. 立即给一段实质内容。
3. 如有必要，再附加轻量收窄建议。

不再采用：

1. 先反问
2. 等确认
3. 再开始讲

### 6.2 首轮输出模板

推荐模板：

```text
我先按这个目标带你学：
<一句话工作目标>

先给你一张图：
<3-5 点学习地图 / 模块关系 / 理解顺序>

我们先从 <切口> 开始：
<第一段有内容的解释>

如果你想更聚焦，我也可以把这卷收窄成：
[按钮或备选方向]
```

重点是“先教，再建议”，不是“先问，再允许继续”。

### 6.3 用户可主动触发对齐

虽然系统不再默认强制 `ASK`，但要给用户明确控制权：

- `帮我收窄目标`
- `换个切口`
- `改一下这卷想学到的程度`
- `先别讲细节，先给我学习路线`

这些显式操作都可以把 `alignment_state` 拉成 `active`，并让下一轮走 `ASK` 协议。

### 6.4 学习中途的自适应纠偏

自适应对齐不只发生在首轮，也可以发生在中途：

- 用户连续两轮追问不同方向，说明目标漂移。
- 系统检测到当前卷里的概念分布过散，说明卷开始失焦。
- 用户明确表达“现在有点乱”“你先帮我收一下目标”。

此时系统可以发起一次非阻塞建议：

- “我们现在已经从 `compaction` 讲到了 `session replay` 和 `memory`，如果你愿意，我可以先把这一卷收回到 `compaction` 主线。”

但默认仍不自动切到阻塞澄清，除非用户点按钮或明确要求。

---

## 七、UI 方案

### 7.1 目标卡片改造

当前主文档里的目标卡片继续保留，但状态要更细：

| 卡片状态 | 文案示例 | 含义 |
|----------|----------|------|
| `working` | 当前先按这个目标学 | 系统基于当前输入形成的工作目标，可继续学习 |
| `refined` | 已收窄目标 | 用户或系统已完成一次对齐收窄 |
| `confirmed` | 已明确学习目标 | 用户显式认可这卷目标 |

MVP 不要求每卷都达到 `confirmed`，只要求它至少有一个可工作的 `working objective`。

### 7.2 非阻塞建议条

在 `suggested` 状态下，卡片下展示一条不打断正文的 suggestion bar：

- 文案示例：
  - “这卷我先按 `compaction 主链` 展开；如果你只想看增量压缩，我可以再收窄。”
- 按钮：
  - `先按这个学`
  - `帮我收窄`

交互要求：

- 默认折叠为一行，不抢正文空间。
- 同一卷同类建议最多出现 2 次，避免 nagging。

### 7.3 阶段进度条调整

原设计中的顶部进度条从：

- `对齐目标 · 学习中 · 复述检验`

调整为：

- `学习中 · 复述检验 · 已收束`

原因：

- “对齐目标”不再是稳定主阶段。
- 用户看见进度条时，应该感知主链，而不是旁路能力。

如确实进入一次阻塞澄清，也不把它单独拉成大阶段，只在目标卡片上短暂显示：

- `正在帮你收窄这卷目标`

### 7.4 学习卷入口文案

入口文案建议从“开始学习”保持不变，但副文案要调整预期：

- 旧隐含预期：先帮你确认目标
- 新隐含预期：先开始学，必要时再帮你收窄

推荐副文案：

> “直接开始学一个概念、模块或设计；如果范围太大，我会帮你逐步收窄。”

---

## 八、数据模型与状态机调整

### 8.1 `LearningUnit.phase` 调整

当前主文档把 `aligning` 当作正式 phase。新方案建议改为：

```python
LearningUnitPhase = Literal[
    "absorbing",
    "outputting",
    "consolidated",
]
```

原因：

- `aligning` 已不再是学习卷主链阶段。
- 把它继续保留在 phase 里，会让前后端和观测层持续误把它当成第一阶段。

### 8.2 新增对齐相关字段

建议在 `LearningUnit` 上增加：

```python
class LearningUnit(BaseModel):
    ...
    alignment_state: Literal[
        "idle",
        "suggested",
        "active",
        "resolved",
        "skipped",
    ] = "idle"
    objective_status: Literal[
        "working",
        "refined",
        "confirmed",
    ] = "working"
    assumption_note: str = ""
    alignment_reason: str = ""
    clarification_count: int = 0
    last_alignment_at: Optional[datetime] = None
```

字段语义：

- `alignment_state`：当前是否在建议或执行对齐
- `objective_status`：目标成熟度，不再靠 phase 侧写
- `assumption_note`：系统当前采用的默认假设，便于前端展示“我先按这个理解”
- `alignment_reason`：为什么建议收窄，例如 `too_broad` / `conflicting_scope`
- `clarification_count`：本卷已触发过几次主动澄清，用于限流
- `last_alignment_at`：方便观测与后续回放

### 8.3 状态转移规则

新的合法转移关系建议为：

```text
phase:
  absorbing -> outputting -> consolidated
  outputting -> absorbing   # TEACH 未通过时回退

alignment_state:
  idle -> suggested -> active -> resolved
  idle -> active -> resolved
  suggested -> skipped
  resolved -> suggested     # 中途再次跑偏时允许再次建议
```

重要约束：

- `alignment_state` 变化不应直接改变 `phase`。
- 只有 `phase` 决定学习卷主链位置。
- `ASK` 的使用只由“本轮是否需要一次对齐动作”决定，不再由 `phase == aligning` 决定。

### 8.4 旧数据迁移

对已有学习卷数据，采用最小扰动迁移：

| 旧状态 | 新状态 |
|--------|--------|
| `phase = aligning` | `phase = absorbing`, `alignment_state = active`, `objective_status = working` |
| `phase = absorbing` | `phase = absorbing`, `alignment_state = idle` |
| `phase = outputting` | 不变 |
| `phase = consolidated` | 不变 |

迁移原则：

- 不回写历史事件。
- 新语义通过追加迁移事件或加载时投影转换表达。
- 继续遵守 append-only JSONL 单事实源约束。<mccoremem id="03g5n3yk063e7m76t9gjg5dis" />

---

## 九、Product / Application 层落点

### 9.1 `_prepare_learning_unit_turn()` 的收口方式

旧逻辑曾是：

- `aligning -> ASK`
- `absorbing -> CHAT`
- `outputting -> TEACH`

2026-05-27 闲聊 / 研学 profile 分离后，新逻辑进一步收口为：

1. 先根据 `phase` 决定主协议：
   - `absorbing -> STUDY`
   - `outputting -> TEACH`
   - `consolidated -> 拒绝继续对话`
2. 再根据 `alignment_state` 和本轮触发器判断是否临时覆写为 `ASK`

伪代码：

```python
if unit.phase == "consolidated":
    reject_read_only()

effective_mode = AgentMode.STUDY if unit.phase == "absorbing" else AgentMode.TEACH

if unit.phase == "absorbing" and should_run_alignment(unit, user_input):
    effective_mode = AgentMode.ASK
```

这保证：

- 主链仍然是学习卷。
- `ASK` 只是本轮协议，不再承担主状态机职责。

### 9.2 `should_run_alignment()` 的判断职责

该判断函数属于 Product/Application 层，不属于 Runtime。

建议输入：

- `LearningUnit`
- `user_input`
- 最近几轮 message 元数据
- 当前 objective / concept 分布

建议输出：

```python
class AlignmentDecision(BaseModel):
    mode: Literal["none", "suggested", "active"]
    reason: Literal[
        "clear_enough",
        "too_broad",
        "goal_drift",
        "conflicting_scope",
        "missing_learnable_target",
    ]
    suggested_objective: str = ""
    assumption_note: str = ""
```

### 9.3 触发限流

为了防止系统重新变回“爱打断的老师”，要加三条护栏：

1. 单卷启动阶段，阻塞澄清最多 1 次。
2. 单卷中途，主动弹出非阻塞建议最多 2 次。
3. 用户显式点过 `先按这个学` 后，本卷接下来至少 3 轮不再主动建议收窄，除非目标彻底跑偏。

---

## 十、`ASK` 协议的新角色

### 10.1 保留 `ASK_PROFILE`，但重命名产品语义

实现上不需要删掉 `ASK_PROFILE`，因为它仍然有价值：

- 做一次短澄清
- 帮用户收窄范围
- 帮用户把“我想学这个”提炼成一句工作目标

但产品语义要改成：

- 旧：学习卷第一阶段
- 新：学习卷中的可选对齐动作

### 10.2 `ASK` 回答模板要同步变轻

旧式 `ASK` 偏“请先确认”。

新式 `ASK` 在学习卷内应改成：

- 先给系统当前假设
- 只问最关键的一句
- 给用户快速选项
- 下一轮默认进入实质讲解

推荐模板：

```text
我现在看到这卷可能有两个方向：
1. <方向 A>
2. <方向 B>

如果你不挑，我会先按 <默认方向> 继续讲。
你也可以直接点一个更想学的方向。
```

这比传统 Ask 更像“帮你收窄”，而不是“要求你补全需求文档”。

### 10.3 通用 Ask 与学习卷 Ask 的边界

需要明确区分两件事：

- **通用 Ask 模式**：用户在普通会话里显式切换到 Ask，做任务澄清
- **学习卷内自适应 Ask**：系统在学习卷中局部使用 Ask 协议做收窄

它们底层可以共用协议，但不应共用同一套产品心智和 UI 文案。

---

## 十一、前端与 API 调整

### 11.1 前端交互新增项

学习卷视图需要新增以下控件：

- 目标卡片状态展示：`working / refined / confirmed`
- 非阻塞建议条
- `先按这个学`
- `帮我收窄`
- `换个目标`
- `继续当前主线`

### 11.2 API 草案

在现有学习卷接口之上，建议增加或细化：

- `POST /learning-units`
  - 创建后默认 `phase=absorbing`
  - 返回 `alignment_state` 与 `objective_status`
- `POST /learning-units/{id}/align`
  - 用户显式要求“帮我收窄”
- `POST /learning-units/{id}/accept-assumption`
  - 用户确认“先按当前理解学”
- `POST /learning-units/{id}/refine-objective`
  - 写入新的工作目标或收窄目标

如果暂时不想增加过多端点，也可先把这些动作折叠进现有 `advance` 接口的 action 参数里，但语义要明确。

### 11.3 SSE / message metadata

学习卷消息建议补充以下 metadata：

- `learning_unit_phase`
- `alignment_state`
- `objective_status`
- `alignment_reason`
- `assumption_note`

这样前端刷新后可以稳定恢复：

- 当前是正常学习
- 还是有一个待处理的收窄建议
- 还是刚经历过一次对齐动作

---

## 十二、观测与评测指标

这次方案的价值不能只靠主观感觉，必须可量化。

### 12.1 关键指标

| 指标 | 含义 | 预期变化 |
|------|------|----------|
| `time_to_first_learning_value` | 从创建学习卷到输出第一段实质学习内容的时延 | 明显下降 |
| `forced_alignment_rate` | 学习卷启动后被阻塞澄清的比例 | 明显下降 |
| `clarification_turns_per_unit` | 每卷平均澄清轮次 | 下降 |
| `unit_completion_rate` | 学习卷走到 `consolidated` 的比例 | 上升 |
| `teach_entry_rate` | 进入 `outputting/TEACH` 的比例 | 上升 |
| `objective_refine_accept_rate` | 用户是否愿意点“帮我收窄” | 作为质量信号，不追求越高越好 |

### 12.2 事件建议

追加以下事件：

- `learning_unit.alignment_suggested`
- `learning_unit.alignment_started`
- `learning_unit.alignment_resolved`
- `learning_unit.alignment_skipped`
- `learning_unit.objective_refined`
- `learning_unit.assumption_accepted`

事件 payload 最少包含：

- `learning_unit_id`
- `phase`
- `alignment_state`
- `reason`
- `clarification_count`
- `objective_status`

### 12.3 主观评测维度

除埋点外，建议做一轮学习体验评测，重点看：

- 用户是否更快进入“被教”的感觉
- 用户是否更少说“你先别问了”
- 系统是否还能在目标失焦时及时收回来
- TEACH 前的概念覆盖是否因少问而变差

---

## 十三、实施计划

### Step 1：状态模型与 Product 调度

- 从 `LearningUnit.phase` 中移除 `aligning`
- 新增 `alignment_state`、`objective_status` 等字段
- 修改 `_prepare_learning_unit_turn()` 与判定逻辑
- 保持 Runtime 不动，只调整 Product 层到 Runtime 的输入

验收：

- 新建学习卷默认直接进入 `absorbing`
- 清晰目标不再自动走 `ASK`
- 模糊目标只允许一次短澄清

### Step 2：前端目标卡片与 suggestion bar

- 进度条去掉“对齐目标”主阶段
- 增加工作目标卡片状态
- 增加非阻塞建议条与按钮
- 刷新后可恢复对齐相关 UI

验收：

- 首轮能直接看到实质学习内容
- 当系统建议收窄时，不会抢占正文

### Step 3：观测与 A/B 验证

- 增加 alignment 事件
- 对比“旧强制 Ask”与“新自适应对齐”的指标
- 补充手测脚本与学习型用例集

验收：

- 有真实数据支持是否继续全面切换

---

## 十四、风险与护栏

### 14.1 风险：直接开学后可能讲偏

缓解方式：

- 首轮显式展示“当前工作目标”
- 允许一键收窄
- 中途支持轻量纠偏建议

### 14.2 风险：系统又偷偷变回频繁打断

缓解方式：

- 以事件和指标约束主动澄清次数
- 明确单卷限流规则
- 提示条默认不超过 2 次

### 14.3 风险：目标状态变复杂，前后端更难对齐

缓解方式：

- 主阶段只保留 3 个
- 对齐状态独立建模
- 用消息 metadata 和事件显式还原 UI，而不是靠 prompt 猜

### 14.4 风险：旧数据与旧文档混淆

缓解方式：

- 本文作为新的设计基线
- 在旧主文档中补充“起始对齐策略以本文为准”的指针
- 迁移按投影/追加事件表达，不做历史回写

---

## 十五、最终建议

从产品角度看，学习模式下的 `Ask` **有必要保留，但没有必要继续作为默认强制前置阶段**。

更准确的结论是：

- **Ask 作为能力，有必要。**
- **Ask 作为学习卷第一阶段，没有必要。**

推荐最终落地口径：

> 学习卷默认直接开始学习；当目标过宽、跑偏或无法开学时，系统再用一次轻量 Ask 帮用户收窄。

这比“先对齐再允许学习”的旧方案，更符合学习型 Agent 的价值交付顺序：

1. 先给用户学习增益
2. 再按需纠偏
3. 最后做输出验收

---

## 十六、与现有主设计文档的关系

以下内容继续沿用现有学习卷主设计文档，不在本方案中推翻：

- Learning Unit 作为顶层容器的产品定位
- `absorbing -> outputting -> consolidated` 的主链价值
- `TEACH` 作为反向问答协议
- 概念抽取、tangent notes、关卷收尾等机制
- Product / Application 编排、Runtime 消费 profile 的分层方向 <mccoremem id="01KRJKA544XWME3RFFKH6QKYKQ|01KRH3PSBX67H36PNR3Q1GBAAX" />

本文件只修正一件事：

- **学习卷入口不再强制 `aligning -> ASK`，改为默认直学、按需对齐。**

2026-05-27 追加收口：

- **学习卷 absorbing 不再借用 `CHAT_PROFILE`，改为默认使用 `STUDY_PROFILE`；只有对齐门本轮临时覆写为 `ASK`。**
- 这使闲聊模式和研学模式可以独立调整提示词、记忆开关、上下文预算和工具策略。
