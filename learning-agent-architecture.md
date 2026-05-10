# Learning-Agent 架构设计文档 v0.2

> 面向代码实现的高层架构参考。定义系统边界、模块职责、数据流向与核心抽象。
> **核心理念：最小核心，最大扩展。Agent 不可能一开始就造得完美，必须预留演进空间。**

---

## 1. 设计目标

构建一个以个人长期学习为中心的 Agent 系统，核心目标：

- **连续性**：学习过程跨越数周至数年，会话可分叉、可回溯、可关联
- **可扩展**：通过 Hook + 事件驱动架构，让系统能力持续生长，不改动核心即可添加功能
- **关联性**：新知识自动链接已有知识，形成可追溯、可复习的知识网络
- **透明性**：所有记忆与知识以人类可读的形态持久化，用户完全可控

---

## 2. 核心痛点 → 架构回应

| 痛点 | 架构回应 |
|------|---------|
| 无面向学习的记忆系统 | 引入**记忆系统**作为一等公民，基础持久化 + 知识关联 |
| Agent 无法持续进化 | 引入**扩展系统（Hook + 事件驱动）**，功能可插拔、可热更新 |
| 无不同学习模式，总结不关联 | 模式通过**扩展系统**注册，核心不耦合具体模式逻辑 |
| 长对话必须新开，不能延续 | 引入**树形会话空间**，会话是探索树而非线性序列 |
| 难清晰表达，AI 答案偏离 | 澄清协议作为**内置扩展**，通过 Hook 介入主循环 |
| 保存不便，难联系旧知识 | 引入**透明持久化层**，所有知识以文件形态存储，自动建立关联 |

---

## 3. 高层架构

### 3.1 分层视图

```
┌─────────────────────────────────────────────┐
│  Presentation Layer                         │
│  对话界面 / 知识图谱视图 / 仪表盘 / 材料浏览器  │
├─────────────────────────────────────────────┤
│  Application Layer                          │
│  会话管理 / 目标追踪 / 复习调度               │
├─────────────────────────────────────────────┤
│  Intelligence Layer                         │
│  Agent 循环 / 意图解析 / 输出倒逼             │
├─────────────────────────────────────────────┤
│  Extension Layer   ◄── 新增                  │
│  Hook 系统 / 事件总线 / 扩展管理器 / 工具注册   │
├─────────────────────────────────────────────┤
│  Memory Layer                               │
│  记忆管理 / 知识图谱 / 掌握度估计 / 间隔重复     │
├─────────────────────────────────────────────┤
│  Provider Layer    ◄── 新增                  │
│  OpenAI SDK / 流式传输 / 模型适配 / 重试策略    │
├─────────────────────────────────────────────┤
│  Persistence Layer                          │
│  基础文件存储 / 材料摄入 / 索引               │
└─────────────────────────────────────────────┘
```

**层间规则**：
- 上层可调用下层，下层不可感知上层
- **Extension Layer 是系统的"神经系统"**：所有层都可以发布/订阅事件，但只能通过 Hook 介入特定扩展点
- Intelligence Layer 是协调中心，但本身不存储任何持久状态
- **Provider Layer 是唯一的 LLM 调用入口**，上层不直接调用任何 API

### 3.2 核心数据流

```
[用户输入 / 材料 / 复习触发]
            │
            ▼
    [Application Layer]
    路由到对应会话
            │
            ▼
    [Extension Layer: Hook 触发]
    beforeIntentParse → afterIntentParse → beforeContextBuild → ...
            │
            ▼
    [Intelligence Layer]
    组装上下文（记忆 + 会话路径 + 材料片段）
            │
            ▼
    [Provider Layer]
    流式 LLM 调用 ──→ 实时推送 token 到 Presentation
            │
            ▼
    [工具执行] ──→ 通过 Extension Layer 分发到对应扩展
            │         读写 Memory Layer
            │         读写 Persistence Layer
            ▼
    [Extension Layer: 事件发布]
    knowledge.extracted / mastery.updated / session.ended
            │
            ▼
    [Application Layer]
    生成响应 + 更新 UI 状态
```

---

## 4. 核心抽象

### 4.1 Session（会话）

会话不是线性对话，而是一棵**探索树**。

- 每个节点（Entry）有唯一标识和父节点引用
- 用户可以在任意节点创建分支（Fork），发起新的探索路径
- 当前活跃的探索路径称为"当前分支"，由叶子节点标识
- 会话可压缩（Compaction）：将深层旧分支替换为摘要节点
- 会话属于一个学习目标（Objective），跨会话的知识通过 Memory Layer 关联

### 4.2 Memory（记忆）

记忆分四层，每层有不同的生命周期和访问方式：

| 层级 | 名称 | 生命周期 | 作用 |
|------|------|---------|------|
| L0 | Transient Memory | 单轮对话 | 当前 LLM 调用所需的即时上下文 |
| L1 | Working Memory | 当前会话 | 本会话中提取的、尚未确认的知识候选 |
| L2 | Long-term Memory | 跨会话 | 已确认的知识节点、掌握度、关联关系 |
| L3 | Archive Memory | 长期归档 | 已掌握且低频访问的知识，仍参与复习调度 |

**关键规则**：
- 知识从 L1 晋升到 L2 需要确认（可配置为自动或手动）
- L2 是知识图谱的载体，所有跨会话关联发生在这一层
- L3 不是删除，而是"休眠"，间隔重复引擎可在适当时机将其唤醒回 L2

### 4.3 Knowledge Node（知识节点）

知识图谱的顶点，也是记忆的核心单元。

- 每个节点有类型（概念 / 过程 / 原理 / 类比 / 问题 / 代码模式）
- 节点携带来源追溯（来自哪份材料的哪个位置）
- 节点携带掌握度估计（estimated → familiar → understood → mastered）
- 节点之间通过带类型的边连接（前提 / 相关 / 类比 / 扩展 / 矛盾）
- 节点参与间隔重复调度，有自己的复习时间线

### 4.4 Extension（扩展）

**扩展是系统能力增长的基本单元**。每个扩展是一个独立的逻辑包，通过 Hook 注册到系统。

```typescript
interface Extension {
  id: string;
  name: string;
  version: string;
  
  // 生命周期
  activate(context: ExtensionContext): void;
  deactivate(): void;
  
  // Hook 注册（扩展在激活时注册自己关心的扩展点）
  hooks?: {
    'agent.beforeIntentParse'?: (input: UserInput, context: HookContext) => Promise<void | HookResult>;
    'agent.afterResponse'?: (response: AgentResponse, context: HookContext) => Promise<void>;
    'memory.knowledgeExtracted'?: (nodes: KnowledgeNode[], context: HookContext) => Promise<void>;
    'session.forked'?: (event: SessionForkEvent, context: HookContext) => Promise<void>;
    // ... 更多扩展点
  };
  
  // 事件订阅（可选）
  subscriptions?: string[];
}
```

**内置扩展**（核心提供，但逻辑上仍是扩展）：
- `core-intent`：意图解析
- `core-clarification`：澄清协议
- `core-output-prompting`：输出倒逼
- `core-review`：间隔重复调度
- `core-mastery`：掌握度估计

**外部扩展**（后续通过扩展系统加载）：
- 新的学习模式
- 新的工具类型
- 新的材料解析器
- 第三方集成（Anki、Notion 等）

### 4.5 Event（事件）

事件是系统内部通信的"神经脉冲"，**事件驱动让模块解耦**。

```typescript
interface Event {
  type: string;           // 事件类型，如 'knowledge.extracted'
  payload: unknown;       // 事件数据
  source: string;         // 发布者 ID
  timestamp: number;      // 时间戳
  sessionId?: string;     // 关联会话
}
```

核心事件类型：
- `session.*`：session.created / message.added / forked / ended / compacted
- `knowledge.*`：extracted / confirmed / linked / masteryUpdated / reviewDue
- `agent.*`：thinking / toolCalled / responseChunk / responseDone / error
- `material.*`：ingested / chunked / indexed
- `extension.*`：activated / deactivated / hookExecuted / error

### 4.6 Hook Point（扩展点）

Hook 是扩展介入核心流程的"阀门"。每个 Hook 点有明确的输入输出契约。

**当前定义的 Hook 点**：

| Hook 点 | 触发时机 | 扩展能力 |
|---------|---------|---------|
| `agent.beforeIntentParse` | 用户输入后，意图解析前 | 修改/替换输入，注入上下文 |
| `agent.afterIntentParse` | 意图解析后 | 覆盖意图结果，触发澄清 |
| `agent.beforeContextBuild` | 上下文组装前 | 注入额外上下文（如复习提醒） |
| `agent.beforeLLMCall` | LLM 调用前 | 修改消息列表，切换模型参数 |
| `agent.onStreamChunk` | 流式响应收到 chunk 时 | 实时处理 token（如格式化、过滤） |
| `agent.afterResponse` | 完整响应生成后 | 追加内容，触发输出倒逼 |
| `agent.onToolCall` | 工具被调用时 | 拦截、修改参数、记录日志 |
| `agent.afterToolResult` | 工具执行后 | 处理结果，触发后续操作 |
| `memory.beforeStore` | 知识写入前 | 校验、去重、丰富元数据 |
| `memory.afterRecall` | 记忆召回后 | 排序、过滤、注入关联 |
| `session.onFork` | 会话分叉时 | 复制/不复制某些上下文 |
| `session.onEnd` | 会话结束时 | 触发总结、归档、复习安排 |

### 4.7 Provider（模型提供者）

**Provider Layer 是所有 LLM 调用的唯一入口**，封装了不同 API 的差异，提供统一的流式/非流式接口。

```typescript
interface Provider {
  id: string;
  name: string;
  
  // 流式调用（默认）
  streamChat(params: ChatParams): AsyncIterable<ChatChunk>;
  
  // 非流式调用（特殊场景）
  chat(params: ChatParams): Promise<ChatResponse>;
  
  // 能力查询
  supportsToolCalling(): boolean;
  supportsVision(): boolean;
  getMaxContextLength(): number;
}

// 当前先实现 OpenAI SDK Provider
class OpenAIProvider implements Provider {
  // 基于 openai npm 包实现
  // 支持 stream: true 的流式传输
  // 自动处理重试、超时、错误转换
}
```

---

## 5. 模块边界与职责

### 5.1 Presentation Layer

**职责**：渲染 UI，捕获用户输入，展示系统输出。

**流式传输支持**：
- 通过 Event Bus 订阅 `agent.responseChunk` 事件，实时渲染 token
- 支持打字机效果、Markdown 实时解析、代码块高亮
- 流式传输中断（用户点击"停止"时发送取消信号到 Provider）

**不职责**：
- 不直接调用 LLM
- 不直接读写文件系统
- 不维护业务状态

**向上暴露**：用户事件（输入、点击、导航）
**向下消费**：Application Layer 的状态更新、Event Bus 的流式事件

### 5.2 Application Layer

**职责**：编排用户请求到系统能力，管理会话生命周期与学习目标。

**核心子模块**：
- **Session Manager**：会话树的 CRUD、分支、导航、压缩、归档
- **Objective Manager**：学习目标的创建、拆解、进度追踪
- **Review Scheduler**：间隔重复触发的调度与提醒

**不职责**：
- 不直接解析材料内容
- 不执行 LLM 调用（通过 Provider Layer）
- 不估计掌握度（通过 Memory Layer 扩展）

**向上暴露**：会话状态、目标进度、复习提醒
**向下消费**：Intelligence Layer 的 Agent 运行结果、Memory Layer 的知识查询结果

### 5.3 Intelligence Layer

**职责**：执行 Agent 循环，协调 LLM 与工具，解析意图，驱动输出。

**核心子模块**：
- **Agent Loop**：消息流管理、LLM 调用（通过 Provider）、工具分发、结果回流的主循环
- **Intent Parser**：用户输入的意图解析与置信度评估（内置扩展 `core-intent`）
- **Context Builder**：组装 LLM 上下文，触发 `agent.beforeContextBuild` Hook

**流式架构**：
```
用户输入
  → Agent Loop 启动
  → 触发 Hook: agent.beforeLLMCall
  → Provider.streamChat() 返回 AsyncIterable
  → 每收到一个 chunk:
      → 触发 Hook: agent.onStreamChunk
      → 发布 Event: agent.responseChunk
      → 实时推送到 Presentation
  → 流结束
  → 触发 Hook: agent.afterResponse
```

**不职责**：
- 不持久化任何状态（循环结束即清空运行时状态）
- 不直接管理会话树结构
- 不直接操作文件系统
- 不直接调用任何 HTTP API（必须通过 Provider Layer）

**向上暴露**：对话响应（通过事件流）、工具执行结果、状态变更事件
**向下消费**：Provider Layer 的流式响应、Memory Layer 的记忆查询

### 5.4 Extension Layer ⭐ 核心新增

**职责**：管理扩展的生命周期、Hook 调度、事件总线。

**核心子模块**：
- **Extension Manager**：扩展的加载、激活、停用、依赖解析
- **Hook System**：Hook 注册、执行顺序管理、链式调用、短路机制
- **Event Bus**：事件的发布、订阅、广播、历史记录
- **Tool Registry**：工具的注册与发现（扩展可以注册新工具）

**Hook 执行规则**：
- 同个 Hook 点可注册多个处理函数，按优先级排序
- 处理函数可以 `return { modified: true, data: ... }` 来修改数据
- 处理函数可以 `throw HookAbortError` 来中断后续处理（用于澄清循环）
- 所有 Hook 异步执行，支持 `await`

**不职责**：
- 不实现具体业务逻辑（业务逻辑在扩展中）
- 不直接调用 LLM
- 不直接读写持久化存储

**向上暴露**：扩展状态、Hook 执行结果、事件流
**向下消费**：无（中间层，协调所有层）

### 5.5 Memory Layer ⭐ 当前核心

**职责**：管理所有记忆与知识的存储、检索、关联、生命周期。

**核心子模块**：
- **Memory Manager**：四层记忆的分层读写、晋升、降级、淘汰
- **Knowledge Graph**：节点的 CRUD、边的维护、图谱查询、路径发现
- **Mastery Estimator**：基于用户交互更新节点掌握度（内置扩展 `core-mastery`）
- **Spaced Repetition Engine**：计算复习时机，生成复习队列（内置扩展 `core-review`）
- **Relevant Recall**：根据当前话题从 L2/L3 中召回相关记忆

**当前版本只做基础实现**：
- 四层记忆的读写接口
- 知识节点的 CRUD
- 基础关联（标签匹配、文本相似）
- 掌握度的简单更新规则
- 间隔重复的简化算法（SM-2）

**后续通过扩展完善**：
- 高级图谱查询
- 复杂掌握度模型
- FSRS 算法替换
- 向量召回

**不职责**：
- 不调用 LLM
- 不解析原始材料
- 不感知 UI

**向上暴露**：记忆查询结果、知识图谱视图、复习队列、掌握度报告
**向下消费**：Persistence Layer 的文件读写

### 5.6 Provider Layer ⭐ 核心新增

**职责**：封装所有 LLM API 调用，提供统一接口。

**核心子模块**：
- **OpenAI Provider**：基于 `openai` SDK 的实现（当前唯一必需）
- **Stream Adapter**：将不同 SDK 的流式输出统一为 `AsyncIterable<ChatChunk>`
- **Retry & Timeout**：自动重试、指数退避、超时处理
- **Error Translator**：将 SDK 错误转换为统一错误类型

**统一接口**：
```typescript
interface ChatParams {
  model: string;
  messages: Message[];
  temperature?: number;
  tools?: Tool[];
  stream?: boolean;  // 默认 true
}

interface ChatChunk {
  content: string;           // 文本内容（可能为空，如工具调用块）
  toolCall?: ToolCallChunk;  // 工具调用片段
  finishReason?: string;     // 结束原因
}
```

**后续扩展**：
- Anthropic Provider（Claude API）
- Local Provider（Ollama / llama.cpp）
- Azure OpenAI Provider

**不职责**：
- 不做业务语义判断
- 不管理对话历史
- 不做上下文压缩

**向上暴露**：流式/非流式聊天接口、模型能力查询
**向下消费**：无（最底层之一，直接调用外部 API）

### 5.7 Persistence Layer

**职责**：所有数据的物理存储、材料摄入、索引构建。

**当前版本只做基础实现**：
- **File Store**：记忆文件、会话文件、配置文件的读写（Markdown / JSONL）
- **Material Ingestion**：基础文本材料的解析与分块
- **Index Builder**：简单的文本索引（文件名、标签、标题）

**后续通过扩展完善**：
- PDF / 网页 / 视频解析
- 向量索引
- 图谱索引

**不职责**：
- 不做业务语义判断
- 不决定什么该记住、什么该遗忘

**向上暴露**：文件内容、材料片段、索引查询结果
**向下消费**：无（最底层）

---

## 6. 关键流程

### 6.1 会话生命周期

```
创建会话
    │
    ├── 绑定学习目标
    ├── 选择初始学习模式
    └── 加载该目标相关的 L2 记忆摘要
    │
    ▼
对话进行（树生长）
    │
    ├── 用户输入 → 追加为树节点
    ├── 触发 Hook: agent.beforeIntentParse
    ├── Agent 响应 → 通过 Provider 流式生成 → 追加为子节点
    │   └── 每 chunk 触发 Hook: agent.onStreamChunk
    ├── 触发 Hook: agent.afterResponse
    ├── 工具执行 → 追加结果节点
    │   └── 触发 Hook: agent.onToolCall / agent.afterToolResult
    └── 分叉 → 从任意节点创建新分支
    │       └── 触发 Hook: session.onFork
    ▼
会话结束
    │
    ├── 触发 Hook: session.onEnd
    ├── 提取 L1 候选知识 → 用户确认 → 晋升 L2
    ├── 发布 Event: knowledge.confirmed
    ├── 生成会话摘要 → 可能触发 Compaction
    ├── 更新掌握度估计
    ├── 发布 Event: mastery.updated
    └── 安排复习计划
```

### 6.2 知识生命周期

```
材料摄入 / 对话提取
    │
    ▼
候选知识（L1）
    │
    ├── 用户确认 / 自动确认（可配置）
    │
    ▼
正式知识（L2）
    │
    ├── 建立图谱关联
    ├── 标记来源追溯
    └── 初始化掌握度 = estimated
    │
    ▼
交互中掌握度演化
    │
    ├── 用户追问深度 → 提升掌握度
    ├── 输出倒逼质量 → 调整掌握度
    ├── 测验表现 → 大幅调整掌握度
    └── 长期无交互 → 降低掌握度（遗忘模拟）
    │
    ▼
掌握后归档（L3）
    │
    ├── 间隔重复引擎在适当时机唤醒复习
    └── 复习失败 → 降级回 L2
```

### 6.3 Agent 循环（流式版）

```
接收输入（用户消息 / 工具结果 / 复习提醒）
    │
    ▼
触发 Hook: agent.beforeIntentParse
    │
    ▼
意图解析
    │
    ├── 高置信度 → 继续
    └── 低置信度 → 进入澄清协议 → 返回用户 → 循环
    │
    ▼
触发 Hook: agent.afterIntentParse
    │
    ▼
上下文组装
    │
    ├── 当前会话路径（从根到当前叶子）
    ├── 当前学习模式的系统提示与工具集
    ├── Relevant Recall：从 L2/L3 召回相关记忆
    ├── 间隔重复插入：本次需复习的节点
    └── 联想提示：与当前话题相关的邻近节点
    │
    ▼
触发 Hook: agent.beforeContextBuild
    │
    ▼
Provider.streamChat(params)
    │
    ├── 发送请求到 OpenAI API
    ├── 建立 SSE / ReadableStream 连接
    │
    ▼
逐 chunk 处理
    │
    ├── 触发 Hook: agent.onStreamChunk(chunk)
    ├── 发布 Event: agent.responseChunk(chunk)
    ├── 实时推送到 Presentation Layer
    │
    ▼
流结束
    │
    ├── 触发 Hook: agent.afterResponse(fullResponse)
    ├── 输出倒逼检查：用户是否过于被动？
    └── 如果是 → 附加输出要求
```

---

## 7. 关键架构决策

### 7.1 最小核心，最大扩展

**决策**：核心系统只包含：会话管理、基础记忆读写、扩展系统骨架、Provider 层、基础持久化。**所有其他能力都通过扩展系统实现**。

**理由**：
- Agent 不可能一开始就造得完美，必须持续迭代
- 硬编码的功能无法在不改动核心的情况下替换或升级
- Hook + 事件驱动让第三方开发者也能扩展系统
- 学习场景的需求会随使用不断浮现，不能预设所有功能

**当前核心包含**：
- ✅ 会话引擎（树形结构）
- ✅ 基础记忆系统（四层 + 简单知识图谱）
- ✅ 扩展系统（Hook + Event Bus + Extension Manager）
- ✅ Provider 层（OpenAI SDK + 流式传输）
- ✅ 基础持久化（文件存储）

**当前通过扩展实现**：
- ⏳ 意图解析（内置扩展 `core-intent`）
- ⏳ 澄清协议（内置扩展 `core-clarification`）
- ⏳ 输出倒逼（内置扩展 `core-output-prompting`）
- ⏳ 间隔重复（内置扩展 `core-review`）
- ⏳ 掌握度估计（内置扩展 `core-mastery`）
- ⏳ 学习模式体系（模式即扩展）

**未来通过外部扩展实现**：
- 🔮 新材料解析器
- 🔮 新 LLM Provider
- 🔮 第三方工具集成
- 🔮 高级可视化

### 7.2 事件驱动 + Hook 架构

**决策**：系统内部通信采用事件总线，流程介入采用 Hook 点。

**理由**：
- 事件让模块完全解耦，发布者不需要知道谁在订阅
- Hook 让扩展可以精确控制流程中的数据流
- 两者结合：Hook 用于"修改行为"，事件用于"通知状态变化"

### 7.3 Provider 层隔离

**决策**：所有 LLM 调用必须通过 Provider Layer，上层代码不直接依赖任何 SDK。

**理由**：
- 统一流式/非流式接口
- 后续切换模型（如从 GPT-4 到 Claude）只需添加新 Provider
- 便于做重试、降级、负载均衡
- 测试时可注入 Mock Provider

### 7.4 流式传输优先

**决策**：LLM 调用默认流式，非流式作为特殊场景备选。

**理由**：
- 用户体验：实时看到生成内容，减少等待焦虑
- 快速反馈：可以在生成过程中中断或修正
- 现代 LLM API（OpenAI、Claude）都原生支持流式

### 7.5 会话模型：树形而非线性

**决策**：会话采用树形结构（id + parentId），支持任意节点分叉。

**理由**：学习是探索性的，用户常在理解过程中产生分支问题，需要回溯、对比不同理解路径。线性模型强制用户放弃旧路径，不符合学习认知规律。

**代价**：UI 展示复杂度高，Compaction 策略需要特殊处理分支摘要。

### 7.6 记忆模型：分层而非统一

**决策**：记忆分为四层（Transient / Working / Long-term / Archive），每层有独立的晋升/降级规则。

**理由**：学习是一个渐进确认的过程。不是所有对话内容都值得长期保存，也不是所有保存的内容都处于活跃状态。分层让系统能自动管理认知负荷，避免记忆膨胀。

**代价**：层间晋升逻辑需要精心设计，避免用户感到"丢数据"。

### 7.7 知识形态：图谱而非列表

**决策**：长期记忆以知识图谱形态组织，而非时间线列表或简单标签集合。

**理由**：学习的本质是建立概念间的关联。列表只能回答"我学过什么"，图谱能回答"这些知识如何相互支撑"。

**代价**：图谱的构建和维护需要持续的关联提取，早期可能稀疏。

### 7.8 持久化：透明文件化

**决策**：所有用户可见的数据（知识、记忆、会话摘要）以人类可读的文本文件（Markdown / JSONL）持久化。

**理由**：学习数据是用户的资产，必须可迁移、可版本控制、可人工审计。数据库虽然查询高效，但对个人用户是黑盒。

**代价**：复杂查询需要自建索引层，不能依赖 SQL。

---

## 8. 扩展点

以下能力在当前版本中不实现，但架构已预留扩展路径：

- **多人协作学习**：知识图谱的共享与冲突解决需预留接口（通过 Event Bus）
- **外部工具集成**：当前工具集内建，未来通过 Extension Layer 的 Tool Registry 扩展
- **多模态材料**：当前以文本为主，未来通过材料解析器扩展支持 PDF、图像、音频、视频
- **自适应学习路径**：当前目标拆解为静态或半自动，未来可由扩展根据掌握度动态调整路径
- **多模型支持**：当前先支持 OpenAI，未来通过 Provider Layer 添加 Claude、本地模型等
- **向量检索**：当前用标签 + 文本匹配，未来通过扩展添加向量索引

---

## 9. 约束与假设

- **单用户**：当前架构假设为个人使用，不处理并发、权限、数据隔离
- **本地优先**：数据默认存储在本地文件系统，网络仅用于调用 LLM API
- **OpenAI SDK 优先**：Provider Layer 先实现 OpenAI SDK 支持，其他 Provider 后续添加
- **流式优先**：所有 LLM 交互默认流式，非流式仅用于特殊场景
- **扩展即功能**：核心只提供骨架，功能通过扩展实现
- **用户最终确认**：关键操作（如知识晋升、模式切换、分叉删除）默认需要用户确认

---

> 本文档定义了系统的骨架。**核心原则是：先让骨架跑起来，再通过扩展长出血肉。**
> 
> 下一步：实现 Extension Layer + Provider Layer 的基础框架，让第一个内置扩展（如 `core-intent`）能成功注册并介入 Agent 循环。
