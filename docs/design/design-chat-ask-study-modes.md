# 技术设计文档：Chat / Ask / Study 三模式落地方案

> **目标**：在保持当前 Runtime 最小内核稳定的前提下，为系统引入 `Chat`、`Ask`、`Study` 三种产品模式，用于区分轻量对话、前置对齐与深度学习三类交互目标。
> **设计原则**：模式属于 Product / Application 层编排，不属于 Runtime 核心状态机；先做可验证的显式模式切换，再逐步演进到更智能的模式建议机制。
> **文档日期**：2026-05-14
> **实现状态**：设计草案，尚未落地。

---

## 一、背景与问题定义

### 1.1 当前问题

当前系统的默认行为本质上只有一种“通用回答模式”，其问题在于：

- 用户只是想快速问一句时，系统不一定足够轻量。
- 用户问题模糊、约束不清时，系统容易直接作答，导致反复澄清、浪费 token，并污染上下文。
- 用户进入深度学习场景时，系统又缺少明确的“重模式”入口，无法自然启用 memory、材料精读、知识提取等更重能力。

这些问题的根源不是“当前 prompt 写得不够长”，而是：

- 当前系统没有显式区分不同交互目标。
- 不同目标下的默认策略、能力开关、上下文预算和回答风格混在了一起。

### 1.2 模式拆分的必要性

从产品语义上看，至少存在三种不同的任务类型：

| 模式 | 核心目标 | 典型诉求 |
|------|----------|----------|
| `Chat` | 快速、低摩擦、低成本回答 | 普通问答、随手咨询、轻量解释 |
| `Ask` | 先对齐问题，再开始正式回答 | 需求澄清、范围确认、复杂任务前置拆解 |
| `Study` | 深度学习、长期推进、知识沉淀 | 读材料、做笔记、概念对比、学习型 coding |

如果继续用一个默认模式覆盖三类目标，会持续出现以下问题：

- `Chat` 场景被过度“流程化”。
- `Ask` 场景缺少明确入口，系统容易误答。
- `Study` 场景缺少独立策略编排，memory / tools / prompt 无法协同。

### 1.3 当前阶段的现实约束

虽然长期看 `Ask` 可以演进为“系统建议进入的前置流程”，但当前阶段不宜一次实现全部需求。

因此本设计采用**分阶段策略**：

- `Chat` 作为唯一默认模式。
- `Study` 显式开启。
- `Ask` 前期显式开启，后期再加入“系统建议进入 Ask”的能力。

这样可以先验证模式价值，再决定是否增加自动判断逻辑。

---

## 二、设计目标与非目标

### 2.1 设计目标

| 目标 | 说明 |
|------|------|
| **目标分离** | 将轻量对话、前置对齐、深度学习三类交互目标显式分开 |
| **最小侵入** | 不重写当前 `AgentLoopSession` 主执行链 |
| **分层清晰** | 模式逻辑留在 Product / Application 层编排，不侵入 Runtime 核心 |
| **能力可控** | 不同模式下可控制 prompt、tools、memory、context 策略 |
| **可渐进演进** | 前期显式切换，后期支持系统建议模式 |

### 2.2 非目标

本设计**不在第一期内**解决以下问题：

- 不实现自动模式判断器。
- 不将 `Ask` 直接做成强制性默认流程。
- 不在第一期重做 memory 主链深度接线。
- 不为三模式分别复制三套独立 Runtime。
- 不在第一期实现复杂的学习计划编排、自动课程推进或知识图谱重构。

---

## 三、总体方案

### 3.1 顶层方案

三模式采用“**产品层模式编排 + 运行时参数化执行**”的方式落地：

```text
User / UI / API
    ->
Mode Switch / Session Mode State
    ->
ModeManager / ModeProfile Resolver
    ->
LearningAgentSystem / Product Layer 组装本轮运行配置
    ->
AgentLoop / AgentLoopSession
```

即：

- Runtime 继续负责“执行一轮 turn”。
- Product 层负责“这一轮用哪种模式运行”。
- 模式差异通过 profile 注入，而不是在 Runtime 中塞大量 if / else。

### 3.2 模式的本质

模式并不是“换一个名字”，而是每一轮执行时的一组策略配置，至少包括：

- 系统提示词
- 是否开启 memory recall / write
- 哪些工具默认可见
- 回答风格
- 上下文预算
- 是否需要用户确认后才能继续

因此，模式应被建模为**策略集合（Mode Profile）**，而不是简单字符串。

### 3.3 角色扮演层的收口方式

当产品需要加入稳定的角色扮演风格时，不应直接把角色设定替代模式本身，而应拆成两层：

- **模式职责层**：决定当前轮次的目标、工具、memory、上下文预算与是否需要确认。
- **人设表达层**：决定称呼、语气、表达节奏与轻量风格差异。

这样做的原因是：

- `Ask` 的“先对齐后执行”不能因为人设变化而失效。
- `Study` 的“重学习、重结构化讲解”不能被风格化 prompt 稀释。
- `Chat` 可以在不破坏默认轻模式语义的前提下，为不同妃子提供轮次级随机切换。

若后续采用后宫角色设定，推荐映射如下：

| 模式 | 人设映射 | 说明 |
|------|----------|------|
| `Chat` | 3 位不同性格的妃子按轮次随机 | 只改变轻量表达风格，不改变默认快答语义 |
| `Ask` | 贵妃 | 强调先揣摩意图、先确认再行动 |
| `Study` | 皇后 | 强调统筹、结构化讲解与长期学习推进 |

实现上应将 persona 元数据与 mode 元数据一并透传，供历史消息渲染、前端展示和调试使用。

---

## 四、模式定义

### 4.1 Chat 模式

**定位**：默认模式，轻量、直接、低摩擦。

**目标**：

- 尽快回应用户。
- 不强制进入澄清流程。
- 保持较轻的上下文负担。

**默认行为**：

- 不要求前置确认。
- 默认不主动写入 memory。
- 工具按需使用，但不鼓励重型链路。
- 输出以“直接回答 + 必要解释”为主。

**适用场景**：

- 普通问答
- 轻量咨询
- 简短代码问题
- 非长期任务

### 4.2 Ask 模式

**定位**：前置对齐模式，用于“先对齐，再开始”。

**目标**：

- 降低用户意图误判。
- 避免反复补充导致的 token 浪费与上下文污染。
- 在正式执行前，把目标、范围、约束讲清楚。

**默认行为**：

- 首轮只输出：目标理解、处理方式、待确认点。
- 不直接进入完整 ReAct 执行链。
- 用户确认后退出 Ask，转入 `Chat` 或 `Study`。
- 默认不写入 memory，避免未确认信息变成脏记忆。

**适用场景**：

- 模糊需求
- 多约束任务
- 长任务起始阶段
- 需要先确认范围与产物形态的工作

### 4.3 Study 模式

**定位**：深度学习模式，用于长期推进与知识沉淀。

**目标**：

- 提升学习增益，而不是只给一个答案。
- 启用更重的学习型策略，例如结构化讲解、材料精读、知识提取、memory。
- 支撑读文档、总结、对比、复盘、学习型 coding。

**默认行为**：

- 开启 memory recall。
- 允许 memory write / extract。
- 更积极使用 `grep`、`read_file offset/limit` 等能力。
- 输出偏“结论 + 原理 + 例子 + 回顾/下一步”。

**适用场景**：

- 读设计文档
- 学习代码架构
- 做概念对比
- 复盘与总结
- 结合代码或材料的深度学习任务

---

## 五、模式默认值与开启方式

### 5.1 第一阶段推荐策略

| 模式 | 是否默认 | 是否显式开启 | 第一阶段是否自动建议 |
|------|----------|--------------|----------------------|
| `Chat` | 是 | 是 | 否 |
| `Ask` | 否 | 是 | 否 |
| `Study` | 否 | 是 | 否 |

推荐规则：

- `Chat` 是唯一默认模式。
- `Ask` 前期只做显式开启。
- `Study` 只做显式开启。

### 5.2 后续演进策略

第二阶段引入：

- `Chat` 中检测到高歧义任务时，系统可**建议**进入 `Ask`
- `Chat` 中检测到长期学习任务时，系统可**建议**进入 `Study`

第三阶段再视验证结果决定是否需要：

- 自动触发 Ask
- 自动建议 Study
- 模式切换历史对后续 prompt 的影响

**注意**：建议先做“建议进入 Ask”，不要直接做“强制自动切换 Ask”。

---

## 六、架构落点

### 6.1 分层归属

模式应明确落在 **Product / Application 层**，而不是 Runtime 内核：

| 层 | 是否承担模式逻辑 | 说明 |
|----|------------------|------|
| Interface | 部分承担 | 接收用户切换命令或 UI 模式按钮 |
| Product / Application | 是 | 决定 session 当前模式，解析 profile，组装本轮配置 |
| Agent Runtime | 否 | 只消费本轮配置并执行 turn |
| Infrastructure | 否 | 只提供持久化、provider、存储等基础能力 |

### 6.2 为什么不能把模式塞进 Runtime

如果把模式逻辑直接写进 `AgentLoopSession`，会有几个问题：

- Runtime 被迫理解大量产品语义。
- `if session.mode == ...` 会逐步污染主执行链。
- 后续新增模式或子流程时，Runtime 会持续膨胀。

因此推荐的方式是：

- Runtime 保持“最小通用执行器”
- Product 层把模式解释为一组 turn 配置

---

## 七、核心数据结构设计

### 7.1 枚举定义

```python
class AgentMode(str, Enum):
    CHAT = "chat"
    ASK = "ask"
    STUDY = "study"
```

### 7.2 Session 状态

建议在 `LearningSession` 上增加：

```python
class LearningSession(BaseModel):
    ...
    mode: AgentMode = AgentMode.CHAT
    mode_metadata: dict[str, Any] = Field(default_factory=dict)
```

其中：

- `mode`：当前 session 的主模式
- `mode_metadata`：模式运行中的辅助状态，例如：
  - Ask 的确认状态
  - 上次模式切换来源
  - Study 模式的策略细节

### 7.3 ModeProfile

```python
class ModeProfile(BaseModel):
    mode: AgentMode
    system_prompt: str
    tools_enabled: list[str] = Field(default_factory=list)
    memory_read: bool = False
    memory_write: bool = False
    ask_confirmation_required: bool = False
    context_budget: str = "normal"      # light | normal | heavy
    response_style: str = "direct"      # direct | align | tutor
```

### 7.4 Turn 配置

Product 层在每轮执行前生成：

```python
class TurnExecutionProfile(BaseModel):
    mode: AgentMode
    system_prompt: str
    visible_tools: list[str]
    memory_read: bool
    memory_write: bool
    response_style: str
    ask_confirmation_required: bool
```

这个对象是 Runtime 的直接输入，Runtime 不需要知道三模式的业务语义。

---

## 八、各模式的配置建议

### 8.1 Chat Profile

```python
ModeProfile(
    mode=AgentMode.CHAT,
    system_prompt="轻量对话型默认提示词",
    tools_enabled=["read_file", "grep", "bash", "write_file", "edit_file"],
    memory_read=False,
    memory_write=False,
    ask_confirmation_required=False,
    context_budget="light",
    response_style="direct",
)
```

说明：

- 可以保留工具，但不鼓励重链路。
- Prompt 中应强调直接、简洁、按需澄清。

### 8.2 Ask Profile

```python
ModeProfile(
    mode=AgentMode.ASK,
    system_prompt="前置对齐型提示词",
    tools_enabled=[],
    memory_read=False,
    memory_write=False,
    ask_confirmation_required=True,
    context_budget="light",
    response_style="align",
)
```

说明：

- Ask 是对齐阶段，不是执行阶段；本期不开放任何工具，避免 Ask 退化为"重分析模式"。
- Ask 的价值在于澄清，需要看代码或文件的任务应在确认后切到 Chat 或 Study 模式执行。
- 执行路径走 `SINGLE_PASS`，本就不会向 LLM 透传 `tools` 字段；这也与"对齐一次、立即收口"的语义保持一致。

### 8.3 Study Profile

```python
ModeProfile(
    mode=AgentMode.STUDY,
    system_prompt="学习强化型提示词",
    tools_enabled=["read_file", "grep", "bash", "write_file", "edit_file"],
    memory_read=True,
    memory_write=True,
    ask_confirmation_required=False,
    context_budget="heavy",
    response_style="tutor",
)
```

说明：

- Study 模式可以使用完整工具链，但应以“学习增益优先”为原则，而不是默认重执行。
- Prompt 中要强调结构化解释、对比、总结、回顾。

---

## 九、模式切换与执行流程

### 9.1 Chat 模式执行

```text
用户输入
    ->
Session.mode == CHAT
    ->
Product 层解析 Chat Profile
    ->
组装 TurnExecutionProfile
    ->
AgentLoop 执行
```

### 9.2 Ask 模式执行

```text
用户显式进入 Ask
    ->
Session.mode == ASK
    ->
Product 层使用 Ask Profile
    ->
输出“目标理解 + 处理方式 + 待确认点”
    ->
等待用户确认
    ->
用户确认后：
    - 切回 Chat
    或
    - 切入 Study
    ->
进入正式回答
```

### 9.3 Study 模式执行

```text
用户显式进入 Study
    ->
Session.mode == STUDY
    ->
Product 层启用 Study Profile
    ->
开启 memory / 学习型回答策略
    ->
AgentLoop 执行
```

### 9.4 Ask 的关键规则

Ask 模式的关键不是“单独 prompt”，而是：

- 其输出不能直接等价于正式回答
- 它必须形成一个确认闭环

因此建议：

- Ask 阶段仅记录必要的对齐信息
- 用户确认后，将“确认后的问题描述”作为正式输入继续执行
- 未确认的 Ask 内容不进入 memory

---

## 十、Prompt 策略

### 10.1 设计原则

不要把三种模式都塞进一个巨大的默认 prompt 中，建议采用：

```text
Base Prompt
    + Chat Prompt Suffix
    + Ask Prompt Suffix
    + Study Prompt Suffix
```

其中：

- `Base Prompt`：定义稳定人格与基础行为
- `Chat Prompt`：强调直接、轻量、按需澄清
- `Ask Prompt`：强调先对齐再继续
- `Study Prompt`：强调学习导向、结构化解释、知识积累

### 10.2 各模式 Prompt 侧重点

**Chat**

- 直接回答
- 适度简洁
- 仅在必要时澄清

**Ask**

- 重述用户目标
- 给出计划草图
- 明确询问确认项
- 不输出完整答案

**Study**

- 解释概念而不只是给结论
- 鼓励对比、例子、总结、回顾
- 更积极使用材料搜索与分页阅读

---

## 十一、Tools 与 Memory 策略

### 11.1 Tools 策略

| 模式 | 工具策略 |
|------|----------|
| `Chat` | 工具可见，但按需使用，避免不必要重链路 |
| `Ask` | 无工具；需查代码或文件的任务请用户确认后切到 Chat / Study |
| `Study` | 启用完整学习工具链，尤其是 `grep + read_file(offset/limit)` |

### 11.2 Memory 策略

| 模式 | Memory Recall | Memory Write | 说明 |
|------|---------------|--------------|------|
| `Chat` | 关闭或极弱 | 关闭或极弱 | 保持轻量，避免脏记忆 |
| `Ask` | 关闭 | 关闭 | 未确认内容不应写入长期记忆 |
| `Study` | 开启 | 开启 | 支撑长期学习与知识沉淀 |

### 11.3 当前约束

考虑到当前项目中 Memory 尚未深度接入主执行链，第一期的 `Study` 模式可以先完成：

- profile 设计
- 预留 recall / write 开关
- Prompt 与策略对齐

再逐步推进更深的 Memory 接线。

---

## 十二、分期实施建议

### Phase 1：模式骨架（优先级：高）

1. 增加 `AgentMode`
2. 在 `LearningSession` 上增加 `mode`
3. 增加 `ModeProfile` 与 profile 注册中心
4. 增加模式切换入口（CLI / Web / API）
5. 三套独立 prompt 草案

### Phase 2：Ask / Study 最小落地（优先级：高）

1. Ask 显式开启
2. Ask 确认闭环
3. Study 显式开启
4. Study 的 memory / tools / prompt 开关接入

### Phase 3：体验增强（优先级：中）

1. 模式切换事件可观测
2. `Ask -> Chat` / `Ask -> Study` 平滑切换
3. Study 模式的学习型输出模板优化
4. Prompt 与 context build 更细粒度分化

### Phase 4：系统建议模式（优先级：中）

1. 在 Chat 中识别模糊任务
2. 系统建议进入 Ask
3. 在 Chat 中识别长期学习任务
4. 系统建议进入 Study

**注意**：这一阶段先做“建议”，不做“强制自动切换”。

---

## 十三、风险与规避方案

### 13.1 主要风险

| 风险 | 说明 | 缓解方式 |
|------|------|----------|
| Ask 过重 | 用户觉得开启 Ask 后仍然太复杂 | Ask 前期限制工具和输出格式 |
| Study 过大 | 一开始把太多能力都塞进 Study | 先做最小学习增强，而非全功能 |
| Runtime 被污染 | 模式逻辑进入 Runtime 主链 | 模式解析只留在 Product 层 |
| 模式切换污染上下文 | Ask 未确认内容进入正式上下文或 memory | Ask 独立对齐态，确认后再转正式输入 |
| 用户不愿显式切换 | 模式入口使用率低 | 后期加入系统建议机制 |

### 13.2 最重要的规避原则

- 不把 `Ask` 当成默认模式
- 不把 `Study` 一开始做成全能力重模式
- 不把模式业务逻辑写死在 `AgentLoopSession` 中

---

## 十四、推荐结论

综合产品体验、实现成本和当前架构约束，推荐如下：

### 14.1 默认策略

- `Chat` 是唯一默认模式
- `Ask` 前期显式开启
- `Study` 显式开启

### 14.2 实现策略

- 模式状态放在 `LearningSession`
- 模式配置由 `ModeProfile` 统一管理
- Product 层负责按模式组装 turn 执行配置
- Runtime 只消费配置并执行，不理解复杂模式业务

### 14.3 演进策略

- 第一阶段先验证 Ask 与 Study 的真实价值
- 第二阶段增加“系统建议进入 Ask / Study”
- 第三阶段再评估是否需要自动触发

---

## 十五、附录：模式关系的正确理解

从产品语义上说，三模式可以对外平级展示；
但从实现语义上说，它们并不完全对称：

- `Chat` 是基础默认模式
- `Study` 是增强型深度模式
- `Ask` 更像“前置对齐流程”对应的显式模式入口

因此，第一期设计应优先保证：

- `Chat` 轻量稳定
- `Ask` 闭环清晰
- `Study` 具备独立策略但不过重

这比一开始追求“自动模式判断”更稳健。

---

*文档完成。后续如需继续推进，可在此基础上补充数据结构变更清单、接口定义和具体任务拆分。*
