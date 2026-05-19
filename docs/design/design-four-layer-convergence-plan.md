# 从七层混乱收敛到四层清晰架构的方案

> 基于文档：`docs/design/design-current-code-architecture-boundaries.md`
> 目标：把当前“概念层很多、真实主链更少”的架构表达，收敛成更稳定、更适合后续重构落地的四层方案。
> 范围：只基于当前已实现代码设计收敛方案，不引入未来尚未落地的 Memory 主链能力。

---

## 1. 为什么要从七层收敛到四层

当前问题不在于“层不够多”，而在于“顶层概念分得太细，但真正代码主链没有那么多独立层”。

当前文档里的七层表达是：

```text
Interface
Composition
Application
Runtime
Core Infrastructure
Domain Services
Infra
```

这套表达在分析现状时是有价值的，但如果继续直接拿它指导重构，会带来 4 个问题：

1. `Composition` 和 `Application` 在当前代码里边界接近，容易反复争论某个对象到底算装配还是应用编排。
2. `Runtime` 和 `Core Infrastructure` 在主链里高度协作，分成两个顶层后，容易让人误以为它们是两套平级业务层。
3. `Domain Services` 单独列为顶层后，会让人倾向于把 `Memory` 当成“已经进入主执行链的主层”，但当前实现并不是这样。
4. 顶层层数过多，会导致后续重构时“每一层都想扩张自己”，反而继续制造边界漂移。

所以，这次收敛的目标不是否定现状分析，而是把“分析层级”压缩成“可执行的重构层级”。

---

## 2. 收敛原则

四层收敛必须遵守下面几条原则：

### 2.1 顶层只保留真正稳定的边界

如果某一组模块在当前实现中总是成组协作、共同对外承担一类职责，就应该被收敛到同一顶层。

### 2.2 顶层减少，不等于内部职责混合

从七层收敛到四层，只是减少“顶层表达数量”，不是把内部职责重新搅在一起。

也就是说：

- 顶层可以少
- 层内子域仍然要清楚
- 子域边界依然要写明

### 2.3 只按当前真实接线收敛

如果某个能力还没有真正进入主链，就不能因为未来很重要，就在顶层架构里给它过高地位。

这条原则对 `Memory` 特别重要：

- `Memory` 很重要
- 但当前还不是对话 runtime 主链的一部分
- 所以这次收敛时，应保留它的独立职责，但不应把它误表达成“主执行链中间层”

### 2.4 收敛后要更接近 pi 的表达方式

收敛后的四层要更接近 `pi` 的思路：

- 顶层少
- 每层职责集中
- 上层依赖下层
- 下层不感知上层
- 产品编排层厚，底层运行层薄

---

## 3. 目标四层架构

收敛后的推荐四层如下：

```text
Layer 1: Interface Layer
Layer 2: Product/Application Layer
Layer 3: Agent Runtime Layer
Layer 4: Infrastructure Layer
```

对应当前项目，可表达为：

```text
CLI / Web
    ->
LearningAgentSystem / SessionManager / MemoryManager / Extension lifecycle
    ->
AgentLoop / AgentLoopSession / HookSystem / EventBus / ToolRegistry / Observability
    ->
Provider / FileStore / Models
```

这四层不是“平均切分”，而是按当前真实代码骨架和后续演进成本来定的。

---

## 4. 四层详细定义

### 4.1 Layer 1: Interface Layer

对应代码：

- `learning_agent/learning_agent/main.py` 中的 CLI 交互入口
- `learning_agent/web/web_server.py`（Web API）
- `web/`

职责：

- 接收用户输入和 HTTP 请求
- 返回 CLI 输出和 Web/SSE 输出
- 调用产品层暴露的统一接口
- 不参与 Runtime 编排

禁止放入：

- session runtime 状态
- hook 调度逻辑
- tool 执行逻辑
- memory 内部状态操作

这层的目标很简单：

**只做输入输出适配，不做业务裁决。**

### 4.2 Layer 2: Product/Application Layer

对应代码：

- `learning_agent/learning_agent/main.py` 中的 `LearningAgentSystem`
- `learning_agent/session/session_manager.py`
- `learning_agent/memory/memory_manager.py`
- `learning_agent/learning_agent/extension_manager.py`
- `learning_agent/extensions/` 的启停装配入口

这是四层方案里最关键的一层。

它承担的不是“单轮 runtime 执行”，而是“产品级编排”。

职责：

- 系统组装与生命周期管理
- session 生命周期与树结构管理
- memory 领域服务暴露
- 扩展加载与停用
- 面向 CLI/Web 提供稳定 API
- 决定一次用户请求由哪个 session、哪个 runtime 去处理

这层内部有 3 个子域，但它们不再单独上升为顶层：

#### 子域 A：System Composition

- `LearningAgentSystem`
- 启动、关闭、依赖装配、状态加载与保存

#### 子域 B：Session / Product State

- `SessionManager`
- objective、session、fork、导航、删除

#### 子域 C：Memory Domain Service

- `MemoryManager`
- `KnowledgeGraph`
- `SpacedRepetitionEngine`

这里要特别强调：

**四层方案中，Memory 不再是顶层第五层，但仍然是 Product/Application 层中的独立子域。**

也就是说：

- 它不再和 Runtime 平级对外表达
- 但它也没有被塞回 `AgentLoopSession`
- 它仍然保留独立的职责边界和后续演进空间

禁止放入：

- 单 session 的瞬态运行状态
- LLM 流式处理细节
- 工具执行重试与降级细节
- Hook 执行细节

这一层的总目标是：

**负责产品编排，但不直接执行 Agent runtime。**

### 4.3 Layer 3: Agent Runtime Layer

对应代码：

- `learning_agent/agent/agent_loop.py`
- `learning_agent/agent/hook_system.py`
- `learning_agent/agent/event_bus.py`
- `learning_agent/learning_agent/tool_registry.py`
- `learning_agent/agent/observability.py`

这层负责一次对话 turn 真正怎么跑。

职责：

- per-session runtime 状态持有
- 状态机流转
- Hook 触发
- Provider 调用
- 工具执行
- 重试、降级、补偿
- 运行时事件发射
- trace / metrics 记录

为什么要把 `HookSystem / EventBus / ToolRegistry / Observability` 一起收进这一层？

因为在当前实现里，它们不是独立产品层，而是 runtime 的横切支撑：

- 没有 runtime，它们不单独构成业务层
- 它们的存在是为了支持 turn 执行
- 它们更像 runtime 内核的配套设施

这和之前“Core Infrastructure Layer”单列分析不同。

分析时单列有助于看清职责，收敛时则更适合并入 Runtime 顶层。

这层内部也应保留清楚的子边界：

#### 子域 A：Runtime Orchestrator

- `AgentLoop`
- `AgentLoopSession`

#### 子域 B：Runtime Cross-Cutting Support

- `HookSystem`
- `EventBus`
- `ToolRegistry`
- `Observability`

禁止放入：

- Web / CLI 逻辑
- session 树数据结构管理
- memory 长期领域状态
- 文件持久化格式

这一层的总目标是：

**把 Agent 当成一个可复用、可隔离、可观测的运行时内核。**

### 4.4 Layer 4: Infrastructure Layer

对应代码：

- `learning_agent/provider/`
- `learning_agent/persistence/file_store.py`
- `learning_agent/ai/models.py`

职责：

- 提供下层通用基础能力
- 对外暴露统一协议和持久化读写能力
- 不承担产品语义

内部可分为：

#### 子域 A：LLM Infrastructure

- Provider 抽象
- OpenAIProvider 等具体实现

#### 子域 B：Storage Infrastructure

- `FileStore`

#### 子域 C：Shared Contracts

- `models.py`

禁止放入：

- session 生命周期策略
- runtime 状态机
- memory 业务规则
- extension 编排逻辑

这一层的总目标是：

**提供通用能力，不参与上层业务判断。**

---

## 5. 七层到四层的映射关系

当前七层和目标四层的映射如下：

| 当前表达 | 收敛后去向 | 说明 |
| --- | --- | --- |
| Interface Layer | Interface Layer | 保持不变 |
| Composition Layer | Product/Application Layer | 并入产品编排层 |
| Application Layer | Product/Application Layer | 并入产品编排层 |
| Runtime Layer | Agent Runtime Layer | 保持为运行时主层 |
| Core Infrastructure Layer | Agent Runtime Layer | 作为 runtime 横切支撑并入 |
| Domain Services Layer | Product/Application Layer | 作为产品层内部独立子域保留 |
| Infra Layer | Infrastructure Layer | 保持不变 |

这张映射表很重要，因为它说明：

- 不是所有层都被“删除”
- 有些层只是从“顶层”降为“层内子域”
- 这正是收敛但不混乱的关键

---

## 6. 为什么 Memory 不单列为第五层

这是这次收敛里最容易争议的点，需要单独说明。

### 6.1 当前实现下，单列顶层会高估它的主链地位

当前 `MemoryManager` 已经独立存在，但它主要用于：

- 查询
- 手动确认
- 启动/关闭时持久化

它还没有真正进入 `AgentLoopSession` 主链做：

- recall
- extract
- promote
- review 驱动

因此，如果现在在顶层架构里把 `Memory` 单列成第五层，会制造一种错误印象：

**好像它已经是和 Runtime 一样成熟、一样深度接线的主层。**

这与当前代码事实不一致。

### 6.2 作为 Product/Application 子域，更符合当前状态

当前最合适的表达是：

- `Memory` 是产品能力中最重要的独立域
- 但当前仍由产品层进行暴露和接线
- 还没有资格在顶层上单独成为一条主执行链

所以在四层方案里：

- 它的独立性保留
- 它的实现成熟度也被准确表达

### 6.3 未来如果 Memory 深度成熟，仍可在物理包结构中单独存在

需要区分两件事：

- 顶层概念架构
- 物理代码包结构

即使四层概念架构中，`Memory` 归入 Product/Application Layer，它未来仍然可以在代码结构上保持独立目录，甚至独立包：

```text
memory/
```

这并不矛盾。

也就是说：

- 逻辑上：它属于产品层独立子域
- 物理上：它完全可以独立模块化

---

## 7. 四层方案带来的直接好处

### 7.1 顶层表达更稳定

以后讨论模块归属时，先问它属于哪一层：

- 输入输出
- 产品编排
- 运行时执行
- 基础设施

绝大多数问题都能快速落位。

### 7.2 运行时不再被一切能力吸附

现在项目最大风险是：

- 新需求一来，就往 `AgentLoopSession` 塞

收敛为四层后，新的功能默认先看是否属于：

- Product/Application
- Runtime

这样能有效减少 runtime 膨胀。

### 7.3 Memory 的边界更准确

四层方案不是弱化 `Memory`，而是让它从“概念上很大、接线却不深”的状态，变成：

- 在产品层里明确独立
- 不污染 runtime
- 未来可以持续长大

### 7.4 更接近 pi 的主干表达

收敛后，整体表达会更接近：

```text
Interface
    ->
Product/Application
    ->
Agent Runtime
    ->
Infrastructure
```

这和 `pi` 的：

```text
web/tui
    ->
coding-agent
    ->
agent
    ->
ai
```

已经非常接近，只是你们在 Product/Application 层内多了一个 `Memory` 独立子域。

---

## 8. 重构时的落地规则

四层方案不是只拿来画图的，后续重构时应按下面规则执行。

### 8.1 规则一：先按四层分，再按子域分

判断新旧模块归属时，先看顶层，再看层内子域。

示例：

- `LearningAgentSystem` -> Product/Application -> System Composition
- `MemoryManager` -> Product/Application -> Memory Domain
- `HookSystem` -> Agent Runtime -> Runtime Cross-Cutting Support

### 8.2 规则二：Runtime 只收直接影响 turn 正确性的能力

只有满足以下条件的能力，才允许进入 Runtime：

- 直接影响一次对话 turn 的执行正确性
- 与流式、工具、重试、降级、状态机强耦合

否则优先放 Product/Application。

### 8.3 规则三：Memory 通过服务接口接入，不通过私有状态接入

后续增强 memory 时，应采用：

- Product/Application 持有 Memory 服务
- Runtime 通过显式接口调用

而不是：

- 给 `AgentLoopSession` 加一堆 memory 私有字段
- 通过 Hook 偷偷承载主流程

### 8.4 规则四：Interface 不再穿透内部字段

Web 和 CLI 只能依赖 Product/Application 暴露的接口，不再直接摸：

- `_sessions`
- `_l1_working`
- runtime 私有字段

### 8.5 规则五：Infrastructure 不承载业务判断

Provider、FileStore、Models 继续保持“下层只提供能力，不理解产品语义”。

---

## 9. 迁移顺序建议

四层收敛不是一次性大改，而是建议按下面顺序推进：

### 第一阶段：先统一架构表述

目标：

- 所有设计文档和讨论都改用四层表达
- 七层只保留在“现状分析”文档里，不再作为未来设计主模型

产出：

- 当前文档
- 后续所有设计都以四层为顶层框架

### 第二阶段：收口 Product/Application 层 API

目标：

- 把 CLI/Web 对内部字段的穿透访问逐步改为系统级接口
- 明确 `LearningAgentSystem` 对外暴露的产品 API

### 第三阶段：继续瘦身 Agent Runtime

目标：

- 把不必在 `AgentLoopSession` 内的职责逐步移回 Product/Application
- 保留稳定性必须的 runtime 逻辑

### 第四阶段：在 Product/Application 层深化 Memory

目标：

- 先让 Memory 作为独立子域继续成熟
- 再通过显式接口逐步接入 Runtime

注意：

- 这一步不是先把 Memory 塞进 Runtime
- 而是先把 Memory 做强，再决定 Runtime 如何调用它

---

## 10. 最终结论

从七层收敛到四层，不是为了“少画三层”，而是为了把当前项目从“分析上很细、实现上很散”收敛成一个更适合重构和长期演进的骨架。

最终推荐的四层是：

1. `Interface Layer`
2. `Product/Application Layer`
3. `Agent Runtime Layer`
4. `Infrastructure Layer`

其中最关键的两个判断是：

- `Composition + Application + Domain Services` 收敛为同一个 Product/Application 顶层
- `Runtime + Core Infrastructure` 收敛为同一个 Agent Runtime 顶层

而 `Memory` 的准确定位是：

**它是 Product/Application 层中的独立核心子域，而不是当前阶段单独升格为顶层主链。**

这套方案既保留了你们未来区别于 `pi` 的核心空间，也避免了在 Memory 还未深度落地前，把整个架构继续画得过重、过散。
