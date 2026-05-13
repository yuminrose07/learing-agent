# 架构收敛技术实现文档

> 版本：v1.0
> 基于文档：
> - `docs/design/design-current-code-architecture-boundaries.md`
> - `docs/design/design-four-layer-convergence-plan.md`
> - `docs/research/pi-mono-architecture-analysis.md`
> 目标：将当前七层分析架构收敛为四层可执行架构，定义完整的职责、边界与落地规则。

---

## 1. 总体原则

### 1.1 为什么从七层收敛到四层

当前代码的真实主链不是七层，而是四条清晰的主干：

```
Interface        → CLI / Web
Product/App      → LearningAgentSystem / SessionManager / MemoryManager / Extension
Agent Runtime    → AgentLoop / AgentLoopSession / Hook / Event / Tool / Observability
Infrastructure   → Provider / FileStore / Models
```

七层表达在分析现状时有价值，但继续用它指导重构会带来三个问题：

1. **Composition 与 Application 边界模糊**：`LearningAgentSystem` 既做装配又做产品编排，强行拆成两层会导致模块归属反复争论。
2. **Runtime 与 Core Infrastructure 高度协作**：`HookSystem`、`EventBus`、`ToolRegistry` 没有 `AgentLoopSession` 就不构成独立业务层，单列会让人误以为它们是平级业务层。
3. **Domain Services（Memory）被高估主链地位**：`MemoryManager` 已独立存在，但尚未深度进入 `AgentLoopSession` 主执行链。单列顶层会制造"它已是和 Runtime 一样成熟主层"的错误印象。

### 1.2 收敛原则

| 原则 | 说明 |
|------|------|
| **顶层稳定** | 只保留真正对外承担独立职责的边界 |
| **内部清晰** | 顶层减少 ≠ 内部职责混合；层内子域边界依然要写清楚 |
| **按真实接线收敛** | 未进入主链的能力，不因未来重要就提前升格 |
| **对齐 pi 主干** | 顶层少、职责集中、上层依赖下层、下层不感知上层 |

### 1.3 与 pi-mono 的对照关系

```
pi-mono                          本项目（收敛后）
─────────                        ────────────────
packages/tui   ──────────────→   Interface Layer (CLI / Web)
packages/coding-agent  ─────→   Product/Application Layer
packages/agent  ────────────→   Agent Runtime Layer
packages/ai     ────────────→   Infrastructure Layer
```

差异：本项目在 Product/Application 层内多了一个 **Memory 独立子域**，这是产品核心差异点，予以保留。

---

## 2. 四层架构总览

### 2.1 架构骨架

```text
┌─────────────────────────────────────────────────────────────┐
│ Layer 1: Interface Layer                                    │
│   CLI (learning_agent/main.py)                              │
│   Web API (learning_agent/web_server.py)                    │
│   Web Static (web/)                                         │
├─────────────────────────────────────────────────────────────┤
│ Layer 2: Product / Application Layer                        │
│   ├─ Subdomain A: System Composition                        │
│   │    LearningAgentSystem                                  │
│   ├─ Subdomain B: Session / Product State                   │
│   │    SessionManager                                       │
│   ├─ Subdomain C: Memory Domain Service                     │
│   │    MemoryManager / KnowledgeGraph / SpacedRepetition    │
│   └─ Subdomain D: Extension Lifecycle                       │
│        ExtensionManager / built-in extensions               │
├─────────────────────────────────────────────────────────────┤
│ Layer 3: Agent Runtime Layer                                │
│   ├─ Subdomain A: Runtime Orchestrator                      │
│   │    AgentLoop / AgentLoopSession                         │
│   └─ Subdomain B: Runtime Cross-Cutting Support             │
│        HookSystem / EventBus / ToolRegistry / Observability │
├─────────────────────────────────────────────────────────────┤
│ Layer 4: Infrastructure Layer                               │
│   ├─ Subdomain A: LLM Infrastructure                        │
│   │    BaseProvider / OpenAIProvider / ResilientProvider    │
│   ├─ Subdomain B: Storage Infrastructure                    │
│   │    FileStore                                            │
│   └─ Subdomain C: Shared Contracts                          │
│        models.py                                            │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 依赖方向（绝对不可违反）

```text
Interface Layer
    ↓ 依赖
Product/Application Layer
    ↓ 依赖
Agent Runtime Layer
    ↓ 依赖
Infrastructure Layer
```

**反向依赖是绝对禁止的。** 当前代码中的反向穿透（如 Web 直接访问 `_sessions`、`_l1_working`）是已知的违反项，必须逐步收敛。

---

## 3. Layer 1: Interface Layer（接口层）

### 3.1 对应代码

- `learning_agent/main.py` 中的 CLI 交互入口与命令解析
- `learning_agent/web_server.py` 中的 FastAPI 路由与 SSE 流式响应
- `web/` 目录下的静态前端资源

### 3.2 核心职责

| 职责 | 说明 |
|------|------|
| **输入适配** | 接收用户键盘输入、HTTP 请求、前端事件 |
| **输出适配** | 将流式输出转换为 CLI 打印、SSE 数据流、WebSocket 消息 |
| **调用产品层** | 只通过 `LearningAgentSystem` 暴露的稳定接口发起操作 |
| **会话状态展示** | 展示 session 列表、memory 状态、metrics 等（只读展示，不写业务状态） |

### 3.3 层内边界

**允许做的事：**
- 解析命令行参数、HTTP 请求体
- 调用 `system.chat()`、`system.show_memory()`、`system.create_objective()` 等产品级 API
- 将 `AsyncIterable[ChatChunk]` 转换为 SSE 格式或终端打印
- 展示系统状态（只读）

**禁止做的事（硬边界）：**
- ❌ 直接操作 `session_manager._sessions` 内部字典
- ❌ 直接操作 `memory_manager._l1_working` 内部字典
- ❌ 直接调用 `agent_loop.run()` 绕过产品层编排
- ❌ 直接修改 runtime 私有状态（`_chat_only_mode`、锁、trace 等）
- ❌ 直接注册 Hook、订阅 EventBus 来承载主业务流程
- ❌ 持有 Agent Runtime 的瞬态状态

### 3.4 与 pi-mono 的对齐

| pi-mono | 本项目 |
|---------|--------|
| `packages/coding-agent/src/modes/interactive/interactive-mode.ts` 将 AgentEvent 翻译为 TUI 组件更新 | `web_server.py` 的 `_stream_chat_chunks` 将 ChatChunk 翻译为 SSE；`main.py` 的 `interactive_cli` 将 ChatChunk 翻译为终端打印 |
| `packages/coding-agent/src/main.ts` 的 CLI 入口根据参数选择运行模式 | `main.py` 的 `argparse` 区分 CLI / Web 模式 |

**改进方向**：Web 前端组件应进一步纯粹化，通过 Event Bus 驱动更新，而不是直接绑定 runtime 内部字段。

---

## 4. Layer 2: Product / Application Layer（产品/应用层）

这是四层中最关键的一层。它承担的不是单轮 runtime 执行，而是**产品级编排**。

### 4.1 对应代码

- `learning_agent/main.py` 中的 `LearningAgentSystem`
- `learning_agent/session/session_manager.py`
- `learning_agent/memory/memory_manager.py`
- `learning_agent/core/extension_manager.py`
- `learning_agent/extensions/` 的启停装配入口

### 4.2 核心职责

| 职责 | 说明 |
|------|------|
| **系统组装与生命周期** | 按正确顺序初始化所有层，管理启动和关闭生命周期 |
| **Session 生命周期管理** | 创建、追加消息、fork、归档、删除、基础导航、树结构维护 |
| **Memory 领域服务暴露** | 四层记忆的查询、晋升、归档、复习；面向 CLI/Web 提供稳定 API |
| **扩展加载与停用** | 扩展注册、依赖排序、激活/停用；将扩展接入 HookSystem / EventBus / ToolRegistry |
| **面向接口层的稳定 API** | 为 CLI/Web 提供统一入口，隐藏内部实现细节 |
| **请求路由决策** | 决定一次用户请求由哪个 session、哪个 runtime 去处理 |
| **持久化编排** | 在适当时机触发状态保存，但不直接处理文件格式细节 |

### 4.3 子域详细定义

#### 子域 A：System Composition（系统组装）

**核心对象**：`LearningAgentSystem`

**职责**：
- 按正确顺序初始化：FileStore → EventBus → HookSystem → ToolRegistry → Observability → ExtensionManager → MemoryManager → SessionManager → Provider → AgentLoop
- 加载和保存持久化状态（ knowledge graph、session 数据）
- 注册内置扩展并激活
- 订阅可观测性事件
- 为 CLI/Web 提供统一入口：`create_objective()`、`start_session()`、`chat()`、`fork_session()`、`show_memory()`、`confirm_knowledge()`、`show_metrics()`

**边界**：
- ✅ 可以持有系统级单例依赖（FileStore、EventBus、Provider 等）
- ✅ 可以暴露面向接口层的系统 API
- ❌ 不应承载单 session 的执行状态（如 `_chat_only_mode`、turn count、锁）
- ❌ 不应吸收 `AgentLoopSession` 内部的 ReACT 执行逻辑
- ❌ 不应直接操作 Provider 流式输出

**与 pi-mono 的对齐**：
- 对应 `packages/coding-agent/src/core/sdk.ts` 中的 `createAgentSession()` —— 组装工厂
- 对应 `packages/coding-agent/src/core/agent-session.ts` 中的 `AgentSession` —— 业务编排器

#### 子域 B：Session / Product State（会话与产品状态）

**核心对象**：`SessionManager`

**职责**：
- 管理 `LearningSession` 的生命周期（创建、获取、删除）
- 管理树形 session 数据结构（根节点、叶子节点、路径、fork）
- 追加消息到会话树，维护 `current_leaf_id`
- 保存"会话数据"（entries、tree structure），不保存"运行时瞬态状态"
- 提供 `get_message_history()` 供 Runtime 构建上下文

**边界**：
- ✅ 管理会话数据结构和持久化数据
- ❌ 不负责 LLM 调用
- ❌ 不负责 Hook、事件、工具执行
- ❌ 不负责 Memory 推理和知识晋升
- ❌ 不负责 session runtime 并发控制（锁在 Runtime 层）

**与 pi-mono 的对齐**：
- 对应 `packages/coding-agent/src/core/session-manager.ts` —— 会话的创建、保存、加载、fork、导入导出

#### 子域 C：Memory Domain Service（记忆领域服务）

**核心对象**：`MemoryManager`、`KnowledgeGraph`、`SpacedRepetitionEngine`

**职责**：
- 管理四层 Memory 数据结构接口（L0 瞬态、L1 工作记忆、L2 长期记忆、L3 归档）
- 管理知识图谱的节点和边
- 管理间隔重复引擎的调度
- 提供查询、晋升、归档、复习到期获取等能力
- 提供 `relevant_recall()` 供未来 Runtime 调用

**关键定位**：
- Memory **不是**当前对话 runtime 主链的组成部分
- Memory **是**产品层中最重要的独立子域
- Memory **不应**下沉为 `AgentLoopSession` 的内部状态字段

**边界**：
- ✅ 作为独立服务存在，有清晰的 API 边界
- ✅ 后续可通过显式接口接入 Application/Runtime
- ❌ 不应被 `AgentLoopSession` 直接持有为私有状态
- ❌ 不应通过 Hook 偷偷承载主流程 recall/extract/promote

**与 pi-mono 的差异**：
- pi-mono 的 coding-agent 层没有独立的 Memory 子域（它是编码场景，记忆机制不同）
- 这是本项目的核心差异点，**保留 Memory 作为独立子域**

#### 子域 D：Extension Lifecycle（扩展生命周期）

**核心对象**：`ExtensionManager`、`Extension`、`ExtensionContext`

**职责**：
- 维护扩展列表
- 处理依赖解析与拓扑排序
- 控制扩展的激活/停用生命周期
- 在激活时为扩展注入 `HookSystem`、`EventBus`、`ToolRegistry` 的访问能力

**边界**：
- ✅ 负责扩展的注册、排序、激活、停用
- ✅ 将扩展接入基础设施层的 Hook/Event/Tool 系统
- ❌ 不参与具体 session turn 的执行
- ❌ 扩展只能增强 runtime，不能替代 runtime

**与 pi-mono 的对齐**：
- 对应 `packages/coding-agent/src/core/extensions/runner.ts` 中的 `ExtensionRunner`
- **差异点**：pi-mono 的 ExtensionRunner 在应用层负责加载用户扩展；我们的设计保留 core 层的基础 Hook + Event，将扩展加载机制提升到应用层

### 4.4 本层总目标

> **负责产品编排，但不直接执行 Agent runtime。**

---

## 5. Layer 3: Agent Runtime Layer（Agent 运行时层）

这层负责一次对话 turn **真正怎么跑**。

### 5.1 对应代码

- `learning_agent/agent/agent_loop.py` 中的 `AgentLoop` 和 `AgentLoopSession`
- `learning_agent/core/hook_system.py`
- `learning_agent/core/event_bus.py`
- `learning_agent/core/tool_registry.py`
- `learning_agent/core/observability.py`

### 5.2 核心职责

| 职责 | 说明 |
|------|------|
| **per-session runtime 状态持有** | `AgentLoopSession` 持有 `_failure_tracker`、`_chat_only_mode`、锁、trace metadata 等 |
| **状态机流转** | IDLE → BUILDING_CONTEXT → CALLING_LLM → STREAMING → EXECUTING_TOOL → COMPLETED/ERROR |
| **ReACT 循环执行** | 内层循环处理 LLM → Tool → Result → LLM；外层处理会话生命周期 |
| **Hook 触发** | 在标准切点调用 HookSystem：`before_agent_run`、`on_stream_chunk`、`after_response`、`before_tool_execute`、`after_tool_execute` |
| **Provider 调用** | 通过统一接口调用 LLM，消费流式输出 |
| **工具执行** | 解析 tool calls，调用 ToolRegistry，处理结果 |
| **重试、降级、补偿** | Turn 级 Provider 重试、工具执行重试、chat-only 降级、孤儿 tool call 补偿 |
| **运行时事件发射** | 通过 EventBus 发射 `agent.*` 事件和状态快照 |
| **trace / metrics 记录** | 通过 Observability 记录 span、trace、指标 |

### 5.3 子域详细定义

#### 子域 A：Runtime Orchestrator（运行时编排器）

**核心对象**：`AgentLoop`、`AgentLoopSession`

**`AgentLoop` 职责**：
- 持有共享依赖（provider、events、hooks、tools、obs）
- 管理 per-session `AgentLoopSession` 实例的创建、获取、清理
- 提供 `run()` 入口，内部路由到对应的 `AgentLoopSession`
- 维护运行时过期机制（TTL），防止内存泄漏
- 作为工厂/路由器，而非执行主体

**`AgentLoopSession` 职责**：
- 执行单个 session 的完整对话回合
- 持有 per-session 可变状态：
  - `ToolFailureTracker`（工具失败追踪与禁用）
  - `chat-only mode` 与恢复计数
  - `asyncio.Lock`（并发安全）
  - 当前 trace / hook runtime metadata
- 管理状态机流转
- 驱动 Hook 调用、Provider 流式调用、工具执行
- 处理 Ask 对齐模式
- 发射丰富事件（`agent_start/end`、`turn_start/end`、`message_start/update/end`、`tool_execution_start/end`）

**边界**：
- ✅ 可以依赖基础设施层（Provider、Models）
- ✅ 可以依赖 Runtime 横切支撑（Hook、Event、Tool、Observability）
- ✅ 可以读取 Session 数据（通过 `session_manager` 接口）
- ❌ 不应吸收 CLI/Web 逻辑
- ❌ 不应管理 session 树数据结构（只读取，不修改树结构）
- ❌ 不应持有 memory 长期领域状态
- ❌ 不应处理文件持久化格式
- ❌ 不应直接决定 session 生命周期（创建/删除由 Product 层决定）

**与 pi-mono 的对齐**：
- `AgentLoop` 对应 `packages/agent/src/agent.ts` 中的 `Agent` 类（状态机封装 + 监听器管理）
- `AgentLoopSession` 对应 `packages/agent/src/agent-loop.ts` 中的 `runAgentLoop`（纯函数式 ReACT 循环）
- **改进方向**：参考 pi 的 `AgentLoopConfig` 策略注入模式，将 `AgentLoopSession` 中硬编码的策略（如上下文组装、对齐判断、重试逻辑）逐步显式化为可注入配置

#### 子域 B：Runtime Cross-Cutting Support（运行时横切支撑）

**核心对象**：`HookSystem`、`EventBus`、`ToolRegistry`、`ObservabilityCollector`

**`HookSystem` 职责**：
- 提供 5 个稳定切点：
  - `before_agent_run`
  - `before_tool_execute`
  - `after_tool_execute`
  - `on_stream_chunk`
  - `after_response`
- 负责 handler 顺序执行和 typed result 合并
- 隔离 Hook 异常，防止单个 Hook 失败拖垮主流程

**`EventBus` 职责**：
- 提供轻量 pub/sub
- 支持按事件类型订阅和通配符订阅
- 异步广播，订阅者异常隔离
- 当前定位是广播和观测总线，**不是状态真源**

**`ToolRegistry` 职责**：
- 统一维护工具定义和处理器
- 提供工具 schema 生成（OpenAI 格式）
- 执行工具调用，带超时保护
- 同步/异步 handler 统一包装

**`ObservabilityCollector` 职责**：
- 负责 trace、span、metrics、snapshot、log 收集
- 多 session 安全（按 trace_id 隔离 span 栈）
- 通过订阅 EventBus 实现零侵入观测
- 是 runtime 的观测支撑，**不是业务编排中心**

**边界**：
- ✅ 为 runtime 提供横切能力
- ✅ 不拥有业务 transcript
- ✅ 不拥有 session 树数据
- ✅ 不拥有 Provider 状态
- ❌ 不负责主状态机裁决（裁决权在 `AgentLoopSession`）
- ❌ 不应通过"加新 Hook 点"去承载高层业务主流程

**与 pi-mono 的对齐**：
- `HookSystem` + `EventBus` 对应 pi 的 `AgentEvent` + `subscribe()` 事件驱动架构
- `ToolRegistry` 对应 pi 的 `AgentTool` 接口定义
- `ObservabilityCollector` 对应 pi 的 `auditLogger` 注入

### 5.4 本层总目标

> **把 Agent 当成一个可复用、可隔离、可观测的运行时内核。**

---

## 6. Layer 4: Infrastructure Layer（基础设施层）

### 6.1 对应代码

- `learning_agent/provider/`（`base_provider.py`、`openai_provider.py`、`resilient_provider.py`）
- `learning_agent/persistence/file_store.py`
- `learning_agent/models/models.py`

### 6.2 核心职责

| 职责 | 说明 |
|------|------|
| **LLM 统一调用** | 屏蔽 OpenAI / 其他 Provider 的协议差异，输出统一 `ChatChunk` / `ChatParams` |
| **本地文件持久化** | 保存 session、objective、knowledge graph、material 等人类可读数据 |
| **共享数据契约** | 提供跨层共享的 Pydantic 模型，是层与层之间的通用语言 |

### 6.3 子域详细定义

#### 子域 A：LLM Infrastructure

**核心对象**：`BaseProvider`、`OpenAIProvider`、`ResilientProvider`

**职责**：
- 提供统一流式/非流式 LLM 调用接口
- 屏蔽具体 SDK 差异
- 输出统一的 `ChatChunk` / `ChatParams` 契约
- 支持 tool calling、vision、context length 查询等能力声明

**边界**：
- ✅ 回答"如何与模型交互"
- ❌ 不感知 session 树
- ❌ 不感知 Hook、Extension、Memory
- ❌ 不回答"为什么调用模型"

**与 pi-mono 的对齐**：
- 对应 `packages/ai` —— 统一多 Provider LLM API
- **改进方向**：参考 pi 的 Provider 注册表模式（`api-registry.ts`），支持外部动态注册新 Provider，而不是在代码中硬编码 OpenAI

#### 子域 B：Storage Infrastructure

**核心对象**：`FileStore`

**职责**：
- 提供本地文件持久化（JSON、JSONL、Markdown、文本）
- 管理目录结构（sessions、memory、materials、objectives）
- 提供通用读写原语（`write_json`、`read_json`、`append_jsonl`、`write_text` 等）

**边界**：
- ✅ 只做存取，不做业务决策
- ✅ 不感知 runtime 状态机
- ❌ 不应直接被 Hook 或扩展随意写入业务状态
- ❌ 不管理"何时保存"的决策（由 Product 层决定）

#### 子域 C：Shared Contracts

**核心对象**：`models.py` 中的所有 Pydantic 模型

**职责**：
- 提供跨层共享的数据契约
- 是层与层之间的通用语言
- 包含：Message、ChatChunk、ChatParams、Event、ToolCall、ToolDefinition、Trace、TraceSpan、LearningSession、SessionEntry、KnowledgeNode 等

**边界**：
- ✅ 可以被多层引用
- ❌ 不应反向承载业务编排逻辑
- ❌ 不应包含 runtime 状态机转换逻辑

### 6.4 本层总目标

> **提供通用能力，不参与上层业务判断。**

---

## 7. 层间交互契约

### 7.1 调用关系矩阵

| 调用方 ↓ \ 被调用方 → | Interface | Product/App | Agent Runtime | Infrastructure |
|----------------------|-----------|-------------|---------------|----------------|
| **Interface** | — | ✅ 直接调用 | ❌ 禁止穿透 | ❌ 禁止穿透 |
| **Product/App** | ❌ 不依赖 | — | ✅ 直接调用 | ✅ 直接调用 |
| **Agent Runtime** | ❌ 不依赖 | ❌ 不依赖 | — | ✅ 直接调用 |
| **Infrastructure** | ❌ 不依赖 | ❌ 不依赖 | ❌ 不依赖 | — |

### 7.2 数据流向

```text
[User Input]
    │
    ▼
┌─────────────────────────────────────────────┐
│ Interface Layer                             │
│   CLI: input() / Web: HTTP Request          │
└────────────┬────────────────────────────────┘
             │ 调用 Product API
             ▼
┌─────────────────────────────────────────────┐
│ Product / Application Layer                 │
│   LearningAgentSystem.chat()                │
│   SessionManager.append_message()           │
│   MemoryManager.relevant_recall()           │
└────────────┬────────────────────────────────┘
             │ 调用 AgentLoop.run()
             ▼
┌─────────────────────────────────────────────┐
│ Agent Runtime Layer                         │
│   AgentLoop.run() → AgentLoopSession        │
│     → HookSystem.run_before_agent_run()     │
│     → Provider.stream_chat()                │
│     → ToolRegistry.execute()                │
│     → EventBus.publish()                    │
└────────────┬────────────────────────────────┘
             │ 调用 Provider / FileStore / Models
             ▼
┌─────────────────────────────────────────────┐
│ Infrastructure Layer                        │
│   OpenAIProvider.stream_chat()              │
│   FileStore.save_session()                  │
│   models.py (数据契约)                       │
└─────────────────────────────────────────────┘
```

### 7.3 事件流向（反向通知）

Runtime 层通过 `EventBus` 发射事件，上层通过订阅接收通知：

```text
Agent Runtime
    → EventBus.publish("agent.stateChanged")
    → ObservabilityCollector.on_event()    (Infrastructure 层观测)
    → LearningAgentSystem._on_state_snapshot() (Product 层持久化)
    → Web SSE stream                         (Interface 层推送)
```

**关键规则**：事件用于通知、审计、观测、外挂能力联动，**不是状态真源**。状态真源在 Product 层（SessionManager）和 Runtime 层（AgentLoopSession 的瞬态状态）。

---

## 8. 关键设计模式

| 设计模式 | 应用位置 | 作用 | 与 pi-mono 的对齐 |
|---------|---------|------|------------------|
| **Provider 抽象** | `BaseProvider` | 屏蔽 LLM SDK 差异 | `packages/ai` 的 `ApiProvider` 接口 |
| **事件驱动架构** | `EventBus` + `AgentEvent` | Loop 与 UI/日志/观测完全解耦 | `AgentEventSink` + `subscribe()` |
| **策略注入** | `HookSystem` | Runtime 行为由外部 Hook 决定 | `AgentLoopConfig` 配置对象 |
| **工厂/路由器** | `AgentLoop` | 管理 per-session runtime 生命周期 | `Agent` 类管理 `_state` 和 `listeners` |
| **快照隔离** | `AgentStateSnapshot` | 保存全局状态快照供持久化 | `AgentContext` 不可变快照 |
| **组合优于继承** | `LearningAgentSystem` | 组合多个子系统为完整产品 | `createAgentSession` 组装工厂 |
| **数据模型分层** | `models.py` | 每层有自己的数据模型，边界处转换 | `AgentMessage` → `Message` → LLM API |

---

## 9. 物理目录映射

收敛后的四层架构不要求立即重构目录结构，但后续演进时应遵循以下映射：

```text
learning_agent/
├── __init__.py
├── main.py                    ← Layer 2 (LearningAgentSystem) + Layer 1 (CLI 入口)
├── web_server.py              ← Layer 1 (Web API)
├── config.py                  ← 配置（跨层）
│
├── agent/
│   ├── __init__.py
│   └── agent_loop.py          ← Layer 3 (AgentLoop + AgentLoopSession)
│
├── core/                      ← Layer 3 (Runtime Cross-Cutting Support)
│   ├── __init__.py
│   ├── event_bus.py
│   ├── hook_system.py
│   ├── tool_registry.py
│   ├── tool_failure_tracker.py
│   ├── tool_validator.py
│   ├── observability.py
│   └── extension_manager.py   ← Layer 2 (Extension Lifecycle)
│
├── extensions/                ← Layer 2 (内置扩展实现)
│   ├── __init__.py
│   ├── built_in.py
│   ├── code_tools.py
│   ├── context_compressor.py
│   ├── security_audit.py
│   └── tool_guard.py
│
├── session/
│   ├── __init__.py
│   └── session_manager.py     ← Layer 2 (Session / Product State)
│
├── memory/                    ← Layer 2 (Memory Domain Service)
│   ├── __init__.py
│   ├── memory_manager.py
│   ├── knowledge_graph.py
│   └── spaced_repetition.py
│
├── provider/                  ← Layer 4 (LLM Infrastructure)
│   ├── __init__.py
│   ├── base_provider.py
│   ├── openai_provider.py
│   └── resilient_provider.py
│
├── persistence/               ← Layer 4 (Storage Infrastructure)
│   ├── __init__.py
│   └── file_store.py
│
└── models/                    ← Layer 4 (Shared Contracts)
    ├── __init__.py
    └── models.py
```

**说明**：
- `core/extension_manager.py` 虽然物理上在 `core/` 目录，但逻辑上属于 Layer 2（Product/Application）
- `extensions/` 虽然物理上在 `learning_agent/` 下，但逻辑上属于 Layer 2 的扩展实现
- 后续如果目录重构，可将 `extension_manager.py` 移到 `session/` 或新建 `application/` 目录

---

## 10. 落地实施路径

### 第一阶段：统一架构表述（已完成）

- ✅ 所有设计文档改用四层表达
- ✅ 本文档作为技术实现基线

### 第二阶段：收口 Product/Application 层 API（当前阶段）

**目标**：把 Interface 层对内部字段的穿透访问收敛为系统级接口。

**具体任务**：

| 优先级 | 任务 | 涉及文件 | 验收标准 |
|--------|------|---------|---------|
| P0 | 封装 `_sessions` 访问 | `web_server.py`、`main.py` | Web/CLI 不再直接访问 `system.session_manager._sessions` |
| P0 | 封装 `_l1_working` 访问 | `web_server.py`、`main.py` | Web/CLI 不再直接访问 `system.memory_manager._l1_working` |
| P1 | 封装 runtime 状态查询 | `web_server.py` | `/observability/runtimes` 通过 `AgentLoop` 暴露的只读接口获取 |
| P1 | 明确 `LearningAgentSystem` 对外 API | `main.py` | 所有外部调用都通过 `LearningAgentSystem` 的公开方法 |

**参考实现模式**：

```python
# 当前（违反边界）
session = system.session_manager._sessions.get(session_id)

# 目标（符合边界）
session = system.get_session(session_id)  # LearningAgentSystem 提供封装
```

### 第三阶段：继续瘦身 Agent Runtime（下一阶段）

**目标**：把不必在 `AgentLoopSession` 内的职责逐步移回 Product/Application。

**具体任务**：

| 优先级 | 任务 | 说明 |
|--------|------|------|
| P1 | 上下文组装策略注入 | 将 `_build_context_for_turn()` 的逻辑抽取为可注入的 `ContextBuilder` |
| P1 | Ask 对齐模式外置 | 将 Ask 模式的判断逻辑从 `AgentLoopSession` 提升到 Product 层 |
| P2 | 流中断兜底策略配置化 | 将 `_finalize_with_llm()` 的触发条件配置化 |
| P2 | 重试策略配置化 | 将 Turn 级重试、工具重试的策略参数显式化为 `ResilienceConfig` |

### 第四阶段：在 Product/Application 层深化 Memory（未来）

**目标**：先让 Memory 作为独立子域继续成熟，再通过显式接口逐步接入 Runtime。

**原则**：
- 不是先把 Memory 塞进 Runtime
- 而是先把 Memory 做强，再决定 Runtime 如何调用它

**具体任务**：

| 优先级 | 任务 | 说明 |
|--------|------|------|
| P2 | 强化 `relevant_recall()` | 实现基于 embedding 的语义召回 |
| P2 | 自动知识提取 | 在 Product 层实现对话后的知识提取流水线 |
| P3 | Memory 接入 Runtime | 通过 `before_agent_run` Hook 或显式接口注入相关记忆 |

---

## 11. 边界检查清单

在新增或修改模块时，用以下清单自检：

### 11.1 模块归属检查

```text
□ 这个模块处理用户输入或输出？ → Interface Layer
□ 这个模块做系统组装、session 管理、memory 服务、扩展编排？ → Product/Application Layer
□ 这个模块直接影响一次对话 turn 的执行？ → Agent Runtime Layer
□ 这个模块提供通用基础能力（LLM、存储、数据契约）？ → Infrastructure Layer
```

### 11.2 依赖方向检查

```text
□ 下层模块没有 import 上层模块
□ Interface 没有直接操作 Product 层内部私有字段
□ Runtime 没有直接管理 session 生命周期（创建/删除）
□ Infrastructure 没有感知业务语义
```

### 11.3 Runtime 膨胀检查

```text
□ 新能力是否直接影响 turn 执行正确性？
□ 新能力是否与流式、工具、重试、降级、状态机强耦合？
□ 如果以上答案都是"否"，则优先放 Product/Application Layer
```

### 11.4 Memory 边界检查

```text
□ Memory 相关逻辑是否通过显式服务接口调用？
□ 没有给 AgentLoopSession 新增 memory 私有字段？
□ 没有通过 Hook 偷偷承载 memory 主流程？
```

---

## 12. 与 pi-mono 的核心差异与保留空间

| 维度 | pi-mono | 本项目 | 保留理由 |
|------|---------|--------|---------|
| **顶层数量** | 4 层（tui / coding-agent / agent / ai） | 4 层（Interface / Product / Runtime / Infra） | 对齐 |
| **Memory** | 无独立域（编码场景不需要） | Product 层内独立子域 | 学习场景的核心差异 |
| **Provider 注册** | 动态注册表（`api-registry.ts`） | 静态类继承 | 后续应演进为注册表模式 |
| **扩展系统** | 应用层 `ExtensionRunner` | Core 层 `ExtensionManager` + 应用层加载 | 当前设计已可行，可逐步提升加载机制到应用层 |
| **事件系统** | `AgentEvent` 强类型事件流 | `EventBus` 通用 pub/sub + `AgentEvent` 语义层 | 两者互补，保留通用 EventBus 的灵活性 |
| **上下文快照** | `AgentContext` 不可变副本 | `AgentStateSnapshot` 持久化快照 | 已具备快照概念，可进一步向不可变上下文演进 |
| **TUI 分离** | TUI 完全独立包 | Web UI 与业务有一定耦合 | Web 前端应进一步纯粹化 |

---

## 13. 一句话总纲

> **承认当前已经形成的真实骨架，将其收敛为四层可执行架构；Interface 只做适配，Product 负责编排，Runtime 只管执行，Infra 只提供能力；Memory 作为 Product 层的独立子域持续成长，待成熟后再以显式接口接入 Runtime。**
