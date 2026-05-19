# 基于当前实现的架构骨架与职责边界

> 目的：基于当前已落地代码，明确真实生效的分层、职责、依赖方向与禁止下沉的边界。
> 范围：只讨论当前仓库中已经实现并真实接线的能力；未来规划中的 Memory 增强、Review 编排、知识提取自动化等不纳入本文件。
> 使用方式：后续重构时优先以本文件为“现状基线”，避免继续让文档架构和代码架构脱节。

---

## 1. 当前真实骨架

当前代码已经形成的真实骨架，不是早期文档中的“理想七层”，而是下面这条主链：

```text
CLI / Web
    ->
LearningAgentSystem
    ->
SessionManager + AgentLoop
    ->
AgentLoopSession
    ->
Provider / HookSystem / EventBus / ToolRegistry / Observability
    ->
FileStore / Models
```

同时，`MemoryManager` 已经作为独立模块存在，但当前主要承担“系统级 Memory 服务”和“查询/确认接口”，并未深度进入 `AgentLoopSession` 的主执行链。

换句话说，当前系统应被理解为：

```text
Interface Layer
    CLI / Web API

Composition Layer
    LearningAgentSystem

Application Layer
    SessionManager
    AgentLoop

Runtime Layer
    AgentLoopSession

Core Infrastructure Layer
    HookSystem / EventBus / ExtensionManager / ToolRegistry / Observability

Domain Services Layer
    MemoryManager

Infra Layer
    Provider / FileStore / Models
```

---

## 2. 分层职责

### 2.1 Interface Layer

对应代码：

- `learning_agent/learning_agent/main.py`（CLI 入口 + `LearningAgentSystem`）
- `learning_agent/web/web_server.py`（Web API）
- `web/`

职责：

- 接收用户输入或 HTTP 请求。
- 调用 `LearningAgentSystem` 暴露的系统级接口。
- 将 `AgentLoop.run()` 的流式输出转换为 CLI 打印或 SSE 响应。
- 不持有 Agent Runtime 状态。
- 不直接编排 Hook、Tool、Provider、Memory 细节。

边界：

- CLI/Web 只能做“输入适配”和“输出适配”。
- CLI/Web 不能直接操作 `AgentLoopSession` 内部状态。
- CLI/Web 可以查询系统级对象，如 session 列表、memory 状态、objective 数据，但不应承载核心业务编排。

当前现状说明：

- `learning_agent/web/web_server.py` 中曾存在少量直接访问（已收敛） `system.session_manager._sessions`、`system.memory_manager._l1_working` 的代码，这是当前实现中的便利性写法，不应作为后续扩展方向。

### 2.2 Composition Layer

对应代码：

- `learning_agent/learning_agent/main.py` 中的 `LearningAgentSystem`

职责：

- 组装系统依赖。
- 管理启动和关闭生命周期。
- 初始化 `FileStore`、`EventBus`、`HookSystem`、`ToolRegistry`、`ExtensionManager`、`MemoryManager`、`SessionManager`、`Provider`、`AgentLoop`。
- 加载和保存持久化状态。
- 为 CLI/Web 提供统一入口。

边界：

- `LearningAgentSystem` 是系统装配器，不是 ReACT runtime。
- 它可以持有系统级单例依赖，但不应承载单 session 的执行状态。
- 它可以暴露面向接口层的系统 API，但不应吸收 `AgentLoopSession` 内部执行逻辑。

这一层在后续重构中的定位：

- 应继续保留。
- 未来如果参考 `pi` 的 `coding-agent` 思路，本层就是最接近“产品编排层”的位置。

### 2.3 Application Layer

对应代码：

- `learning_agent/session/session_manager.py`
- `learning_agent/agent/agent_loop.py` 中的 `AgentLoop`

职责拆分如下。

### SessionManager

职责：

- 管理 `LearningSession` 的生命周期。
- 管理树形 session 数据结构。
- 负责创建 session、追加消息、fork、归档、删除、基础导航。
- 保存“会话数据”，不保存“运行时瞬态状态”。

不负责：

- 不负责 LLM 调用。
- 不负责 Hook、事件、工具执行。
- 不负责 Memory 推理和知识晋升。
- 不负责 session runtime 并发控制。

### AgentLoop

职责：

- 持有共享依赖：`provider`、`sessions`、`hooks`、`events`、`tools`、`obs`。
- 管理 per-session runtime 的创建、获取、清理。
- 将执行入口路由到 `AgentLoopSession`。
- 作为工厂/路由器，而非执行主体。

不负责：

- 不应再回到“全局单例状态机”的旧模式。
- 不应重新承载 per-session 失败计数、chat-only mode、锁等运行时状态。

结论：

- `SessionManager` 管会话数据。
- `AgentLoop` 管 runtime 实例路由。
- 这两者共同构成当前的 Application 层骨架。

### 2.4 Runtime Layer

对应代码：

- `learning_agent/agent/agent_loop.py` 中的 `AgentLoopSession`

职责：

- 执行单个 session 的完整对话回合。
- 持有 per-session 可变状态：
  - `ToolFailureTracker`
  - `chat-only mode`
  - `chat-only success turns`
  - `asyncio.Lock`
  - 当前 trace / hook runtime metadata
- 管理状态机流转。
- 驱动 Hook 调用。
- 驱动 Provider 流式调用。
- 驱动工具执行、工具重试、降级与补偿。
- 发射 `agent.*` 事件和状态快照。

边界：

- `AgentLoopSession` 是当前系统的 runtime 核心。
- 它可以依赖基础设施层，但不应吸收 CLI/Web、文件存储格式、扩展装配逻辑。
- 它当前已经比较重，但后续应沿着“只保留 runtime 必需职责”的方向收敛。

当前必须承认的现实：

- 当前很多恢复、自愈、观测相关逻辑仍在本层，这符合项目“稳定优先”的现状。
- 因此本层暂时不是 `pi` 那种完全纯净的 Agent Runtime，而是“运行主链 + 稳定性策略”的结合体。

### 2.5 Core Infrastructure Layer

对应代码：

- `learning_agent/agent/event_bus.py`
- `learning_agent/agent/hook_system.py`
- `learning_agent/learning_agent/extension_manager.py`
- `learning_agent/learning_agent/tool_registry.py`
- `learning_agent/agent/observability.py`
- `learning_agent/learning_agent/extensions/built_in.py`

职责：

- 为 runtime 提供横切能力。
- 负责 Hook 分发、事件广播、工具注册、扩展激活、观测记录。
- 不拥有业务 transcript。
- 不拥有 session 树数据。
- 不拥有 Provider 状态。

内部进一步划分如下。

### HookSystem

- 提供 5 个稳定切点：
  - `before_agent_run`
  - `before_tool_execute`
  - `after_tool_execute`
  - `on_stream_chunk`
  - `after_response`
- 负责 handler 顺序执行和 typed result 合并。
- 不负责主状态机裁决。

### EventBus

- 提供轻量 pub/sub。
- 当前定位是广播和观测总线，不是状态真源。
- 事件用于通知、审计、观测、外挂能力联动。

### ExtensionManager

- 负责扩展注册、依赖排序、激活/停用。
- 负责把扩展接入 HookSystem / EventBus / ToolRegistry。
- 不参与具体 session turn 执行。

### ToolRegistry

- 统一维护工具定义和处理器。
- 当前物理位置位于 `learning_agent/learning_agent/tool_registry.py`。
- 当前真实边界中，`ToolRegistry` 主要作为 Product 层内部工具基础设施被组合使用。
- `AgentLoopSession` 不再直接依赖 `ToolRegistry`，而是依赖 `ToolExecutionService` 这类最小能力端口。

### Observability

- 负责指标、事件、trace 收集。
- 是 runtime 的观测支撑，不是业务编排中心。

### Built-in Extensions

- 当前默认启用的是最小运行时扩展集合：
  - 观测
  - fulltrace
  - output prompting
  - code tools
  - tool guard
  - security audit
- 这些扩展当前都属于“增强 runtime”的配套能力，不是上层学习业务本身。

边界规则：

- 基础设施层不拥有会话树。
- 基础设施层不直接决定 session 生命周期。
- 扩展只能增强 runtime，不能替代 runtime。

### 2.6 Domain Services Layer

对应代码：

- `learning_agent/memory/memory_manager.py`
- `learning_agent/memory/knowledge_graph.py`
- `learning_agent/memory/spaced_repetition.py`

当前真实职责：

- 管理四层 Memory 数据结构接口。
- 管理 Knowledge Graph。
- 管理基础 Spaced Repetition。
- 提供查询、晋升、归档、复习到期获取等能力。

当前未深度接线的事实：

- `MemoryManager` 已注入 `AgentLoop`，但当前 `AgentLoopSession` 主执行链并没有真正调用它完成 recall / extract / promote。
- 当前 memory 更多通过系统级接口暴露：
  - CLI 的 `/memory`
  - CLI 的 `/confirm`
  - Web 的 memory 查询与确认接口
  - 启动/关闭时的持久化加载与保存

因此本层的准确定位应是：

- 它已经是独立领域服务。
- 但它还不是当前对话 runtime 的主链组成部分。

边界：

- `MemoryManager` 不应下沉为 `AgentLoopSession` 的内部状态字段。
- `MemoryManager` 应继续作为独立服务存在。
- 后续即便加强 memory，也应该通过显式服务接口接入 Application/Runtime，而不是重新塞进 Hook 或 session 私有状态。

### 2.7 Infra Layer

对应代码：

- `learning_agent/provider/`
- `learning_agent/persistence/file_store.py`
- `learning_agent/ai/models.py`

### Provider

职责：

- 提供统一 LLM 调用接口。
- 屏蔽具体 SDK 差异。
- 输出统一的 `ChatChunk` / `ChatParams` 契约。

边界：

- Provider 不感知 session 树。
- Provider 不感知 Hook、Extension、Memory。
- Provider 只回答“如何与模型交互”，不回答“为什么调用模型”。

### FileStore

职责：

- 提供本地文件持久化。
- 保存 session、objective、knowledge graph、material 等人类可读数据。

边界：

- `FileStore` 只做存取，不做业务决策。
- `FileStore` 不感知 runtime 状态机。
- `FileStore` 不应直接被 Hook 或扩展随意写入业务状态。

### Models

职责：

- 提供跨层共享的数据契约。
- 是层与层之间的通用语言。

边界：

- `models` 可以被多层引用。
- 但 `models` 不应反向承载业务编排逻辑。

---

## 3. 当前推荐的依赖方向

基于现状，推荐把依赖方向理解成：

```text
CLI / Web
    ->
LearningAgentSystem
    ->
SessionManager / AgentLoop / MemoryManager
    ->
AgentLoopSession
    ->
HookSystem / EventBus / ToolRegistry / Observability / Provider
    ->
FileStore / Models
```

补充说明：

- `ExtensionManager` 由 `LearningAgentSystem` 在启动时装配。
- 内置扩展通过 `ExtensionManager` 接入 `HookSystem / EventBus / ToolRegistry`。
- `MemoryManager` 当前是系统级独立服务，而不是 Runtime 的内层依赖主干。

---

## 4. 后续重构时必须坚持的边界

以下规则应作为后续重构的硬边界。

### 4.1 不再让 Session 数据和 Runtime 状态混放

- `SessionManager` 只管理 `LearningSession` 数据。
- `AgentLoopSession` 只管理 per-session runtime 状态。
- 删除 session 时，允许通过回调清理 runtime；但不能把 runtime 字段重新塞回 `LearningSession`。

### 4.2 不再把新能力优先塞进 AgentLoopSession

- `AgentLoopSession` 当前已经足够重。
- 新的产品能力，优先判断是否属于：
  - Composition 层
  - Domain Services 层
  - Core Infrastructure 层
- 只有直接影响 turn 执行正确性的能力，才允许进入 Runtime 层。

### 4.3 Memory 不下沉到 Runtime 内部

- 当前 `MemoryManager` 已经具备独立层形态。
- 后续增强 recall / extraction / review 时，仍应保持其“独立服务”定位。
- Runtime 最多依赖 Memory 的显式接口，不拥有 Memory 的内部状态。

### 4.4 Hook 和 Event 只做增强，不做主编排

- Hook 负责标准切点增强。
- Event 负责广播与观测。
- 不应再通过“加新 Hook 点”去承载高层业务主流程。

### 4.5 Interface 层不再穿透访问内部实现

- Web/CLI 不应继续直接操作：
  - `session_manager._sessions`
  - `memory_manager._l1_working`
  - runtime 私有状态
- 后续应逐步收敛为通过系统级 API 访问。

---

## 5. 当前架构的准确认知

为了避免后续继续“按理想图写代码”，对当前架构需要有一个统一认知：

- 当前系统已经有清晰骨架，但还不是最终理想形态。
- 当前最稳定、最真实的主干是：
  - `LearningAgentSystem` 负责装配
  - `SessionManager` 负责会话树
  - `AgentLoop -> AgentLoopSession` 负责对话 runtime
  - `HookSystem / EventBus / ToolRegistry / Observability` 提供横切支撑
  - `MemoryManager` 已独立，但尚未深度接入主链
- 因此后续重构的目标，不是推翻现有主干，而是沿着这条已经存在的骨架继续收敛职责。

---

## 6. 本文件给重构带来的直接结论

基于当前实现，后续骨架重构应优先做以下事情：

1. 固化 `LearningAgentSystem -> AgentLoop -> AgentLoopSession` 这条真实主链。
2. 固化 `SessionManager` 只管会话数据的边界。
3. 把 `MemoryManager` 明确为独立域服务，而不是 runtime 附属物。
4. 把 Interface 层对内部字段的穿透访问逐步收敛掉。
5. 继续缩减 Runtime 层的非必需职责，但不破坏当前稳定性策略。

这意味着本阶段的重构目标应是：

- 先理顺骨架
- 再逐步瘦身 runtime
- 最后再把 memory 以独立服务方式接深

而不是：

- 一边重写 runtime
- 一边补完未来 memory 产品能力

---

## 7. 一句话总纲

当前项目最适合采用的基线不是“理想七层”，也不是“先等 memory 完成”，而是：

**先承认当前已经形成的真实骨架，再基于这条骨架做职责收敛。**
