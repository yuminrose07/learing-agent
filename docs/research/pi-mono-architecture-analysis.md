# pi-mono 分层架构调研分析报告

> 调研范围：`/Users/roseannk/pi-mono`  
> 调研日期：2026-05-13  
> 调研方法：源码阅读 + 依赖分析 + 架构推演

---

## 1. 项目概述

pi-mono 是一个面向编码场景的 AI Agent CLI 工具（类似 Claude Code），采用 **TypeScript monorepo** 组织。其核心架构理念是**严格的单向依赖分层**：每一层只依赖更底层的能力，下层完全不知道上层的存在。

当前版本：`0.72.1`

---

## 2. Monorepo 结构与分层架构总览

### 2.1 包结构

```
pi-mono/
├── packages/
│   ├── tui/           # 终端 UI 基础设施（纯渲染层）
│   ├── ai/            # 统一多 Provider LLM API（协议适配层）
│   ├── agent/         # Agent ReACT 运行时（业务逻辑层）
│   ├── coding-agent/  # 编码 Agent 主应用（产品编排层）
│   └── web-ui/        # Web UI 组件（独立渲染层）
```

### 2.2 构建依赖顺序（外化的分层关系）

```
tui → ai → agent → coding-agent → web-ui
```

> 源码位置：`package.json` 中的 build scripts 定义了这一顺序。

### 2.3 包间依赖矩阵

| 包名 | 依赖的下层包 | 被上层包依赖 |
|------|-------------|-------------|
| `tui` | 无 | `coding-agent` |
| `ai` | 无 | `agent`、`coding-agent` |
| `agent` | `ai` | `coding-agent` |
| `coding-agent` | `ai`、`agent`、`tui` | 无（顶层） |
| `web-ui` | 无（独立） | 无（独立） |

---

## 3. Layer 1: `packages/ai` — 统一 LLM Provider API

### 3.1 解决的问题

> 如何屏蔽 OpenAI / Anthropic / Google / Mistral / Bedrock / Cloudflare / Moonshot 等 **30+ Provider** 的协议差异，让上层只写一套代码？

### 3.2 核心抽象：统一消息协议

pi 定义了一套与任何 Provider 无关的消息类型，所有 Provider 的原始响应都被翻译成这套协议。

**源码位置**：`packages/ai/src/types.ts`（约 464 行）

```typescript
// 统一消息类型
interface UserMessage { role: "user"; content: string | (TextContent | ImageContent)[]; timestamp: number }
interface AssistantMessage { role: "assistant"; content: (TextContent | ThinkingContent | ToolCall)[]; api; provider; model; usage; stopReason }
interface ToolResultMessage { role: "toolResult"; toolCallId; toolName; content; isError }
type Message = UserMessage | AssistantMessage | ToolResultMessage;
```

### 3.3 核心抽象：统一流式事件协议

所有 Provider 的流式输出被统一翻译为同一套事件类型。

**源码位置**：`packages/ai/src/types.ts`，第 269~282 行

```typescript
type AssistantMessageEvent =
  | { type: "start"; partial: AssistantMessage }
  | { type: "text_delta"; contentIndex: number; delta: string; partial: AssistantMessage }
  | { type: "thinking_delta"; contentIndex: number; delta: string; partial: AssistantMessage }
  | { type: "toolcall_start"; contentIndex: number; partial: AssistantMessage }
  | { type: "toolcall_delta"; contentIndex: number; delta: string; partial: AssistantMessage }
  | { type: "toolcall_end"; contentIndex: number; toolCall: ToolCall; partial: AssistantMessage }
  | { type: "done"; reason: Extract<StopReason, "stop" | "length" | "toolUse">; message: AssistantMessage }
  | { type: "error"; reason: Extract<StopReason, "aborted" | "error">; error: AssistantMessage };
```

### 3.4 Provider 注册表模式

**源码位置**：`packages/ai/src/api-registry.ts`

```typescript
// 第 23 行
export interface ApiProvider<TApi extends Api = Api, TOptions extends StreamOptions = StreamOptions> {
  api: TApi;
  stream(model: Model<TApi>, context: Context, options?: TOptions): AssistantMessageEventStream;
  streamSimple(model: Model<TApi>, context: Context, options?: SimpleStreamOptions): AssistantMessageEventStream;
  // ...
}

// 第 80 行
export function getApiProvider(api: Api): ApiProviderInternal | undefined {
  return apiProviders.get(api);
}
```

**源码位置**：`packages/ai/src/stream.ts`

```typescript
export function stream<TApi extends Api>(model, context, options): AssistantMessageEventStream {
  const provider = resolveApiProvider(model.api); // 查表
  return provider.stream(model, context, options as StreamOptions);
}

export function streamSimple<TApi extends Api>(model, context, options): AssistantMessageEventStream {
  const provider = resolveApiProvider(model.api);
  return provider.streamSimple(model, context, options);
}
```

### 3.5 为什么下层感知不到上层

`packages/ai` 的 `package.json` **没有任何 workspace 依赖**。它不 import `agent`、`coding-agent` 或 `tui` 的任何代码。TypeScript 编译器会物理阻止反向引用。

---

## 4. Layer 2: `packages/agent` — Agent ReACT 运行时

### 4.1 解决的问题

> 如何将 "LLM 调用 → 接收响应 → 发现 tool calls → 执行工具 → 构造 tool results → 再次调用 LLM" 的 ReACT 循环封装成一个**可复用、可观测、可中断**的运行时？

### 4.2 核心类：`Agent` — 状态机封装

**源码位置**：`packages/agent/src/agent.ts`（约 602 行）

`Agent` 类持有所有运行时状态：

```typescript
// 第 161 行
export class Agent {
  private _state: MutableAgentState;
  private readonly listeners = new Set<(event: AgentEvent, signal: AbortSignal) => Promise<void> | void>();
  private readonly steeringQueue: PendingMessageQueue;
  private readonly followUpQueue: PendingMessageQueue;
  // ...
}
```

状态定义（第 58~92 行）：

```typescript
type MutableAgentState = Omit<AgentState, "isStreaming" | "streamingMessage" | "pendingToolCalls" | "errorMessage"> & {
  isStreaming: boolean;
  streamingMessage?: AgentMessage;
  pendingToolCalls: Set<string>;
  errorMessage?: string;
};
```

### 4.3 核心机制：事件驱动 + 回调注入

Agent 运行时**只发射事件，不操作 UI**。上层通过 `subscribe()` 注册监听器。

**源码位置**：`packages/agent/src/types.ts`，第 377~392 行

```typescript
export type AgentEvent =
  | { type: "agent_start" }
  | { type: "agent_end"; messages: AgentMessage[] }
  | { type: "turn_start" }
  | { type: "turn_end"; message: AgentMessage; toolResults: ToolResultMessage[] }
  | { type: "message_start"; message: AgentMessage }
  | { type: "message_update"; message: AgentMessage; assistantMessageEvent: AssistantMessageEvent }
  | { type: "message_end"; message: AgentMessage }
  | { type: "tool_execution_start"; toolCallId: string; toolName: string; args: any }
  | { type: "tool_execution_update"; toolCallId: string; toolName: string; args: any; partialResult: any }
  | { type: "tool_execution_end"; toolCallId: string; toolName: string; result: any; isError: boolean };
```

**源码位置**：`packages/agent/src/agent.ts`，第 224~227 行

```typescript
subscribe(listener: (event: AgentEvent, signal: AbortSignal) => Promise<void> | void): () => void {
  this.listeners.add(listener);
  return () => this.listeners.delete(listener);
}
```

### 4.4 核心机制：Context Snapshot（上下文快照）

Agent 不直接修改共享状态，而是操作**不可变的上下文快照**。

**源码位置**：`packages/agent/src/agent.ts`，第 415~421 行

```typescript
private createContextSnapshot(): AgentContext {
  return {
    systemPrompt: this._state.systemPrompt,
    messages: this._state.messages.slice(),   // 复制！不是引用
    tools: this._state.tools.slice(),
  };
}
```

调用链：

```typescript
// 第 387~400 行
private async runPromptMessages(messages): Promise<void> {
  await this.runWithLifecycle(async (signal) => {
    await runAgentLoop(
      messages,
      this.createContextSnapshot(),      // 传快照
      this.createLoopConfig(),           // 传配置
      (event) => this.processEvents(event), // 事件回传
      signal,
      this.streamFn,
    );
  });
}
```

### 4.5 核心机制：`AgentLoopConfig` 策略注入

`AgentLoop` 的所有策略行为都通过配置对象注入，Agent 层只调用、不实现。

**源码位置**：`packages/agent/src/types.ts`，第 115~251 行

```typescript
export interface AgentLoopConfig extends SimpleStreamOptions {
  model: Model<any>;
  convertToLlm: (messages: AgentMessage[]) => Message[] | Promise<Message[]>;
  transformContext?: (messages: AgentMessage[], signal?: AbortSignal) => Promise<AgentMessage[]>;
  getApiKey?: (provider: string) => Promise<string | undefined> | string | undefined;
  shouldStopAfterTurn?: (context: ShouldStopAfterTurnContext) => boolean | Promise<boolean>;
  getSteeringMessages?: () => Promise<AgentMessage[]>;
  getFollowUpMessages?: () => Promise<AgentMessage[]>;
  beforeToolCall?: (context: BeforeToolCallContext, signal?: AbortSignal) => Promise<BeforeToolCallResult | undefined>;
  afterToolCall?: (context: AfterToolCallContext, signal?: AbortSignal) => Promise<AfterToolCallResult | undefined>;
  auditLogger?: import("./audit-logger.js").AgentAuditLogger;
}
```

### 4.6 核心函数：`runAgentLoop` — 纯函数式 ReACT 循环

**源码位置**：`packages/agent/src/agent-loop.ts`（约 744 行）

双层循环结构（第 155~246 行）：

```typescript
async function runLoop(currentContext, newMessages, config, signal, emit, streamFn) {
  let firstTurn = true;
  let pendingMessages: AgentMessage[] = (await config.getSteeringMessages?.()) || [];

  // 外层循环：follow-up 消息队列
  while (true) {
    let hasMoreToolCalls = true;

    // 内层循环：ReACT Turn
    while (hasMoreToolCalls || pendingMessages.length > 0) {
      if (!firstTurn) { await emit({ type: "turn_start" }); }
      // 处理 steering 消息
      // 流式调用 LLM
      const message = await streamAssistantResponse(currentContext, config, signal, emit, streamFn);
      // 检查 tool calls
      const toolCalls = message.content.filter((c) => c.type === "toolCall");
      if (toolCalls.length > 0) {
        const executedToolBatch = await executeToolCalls(currentContext, message, config, signal, emit);
        hasMoreToolCalls = !executedToolBatch.terminate;
      }
      // 检查是否停止
      if (await config.shouldStopAfterTurn?.(...)) {
        await emit({ type: "agent_end", messages: newMessages });
        return;
      }
      pendingMessages = (await config.getSteeringMessages?.()) || [];
    }

    // 检查 follow-up
    const followUpMessages = (await config.getFollowUpMessages?.()) || [];
    if (followUpMessages.length > 0) {
      pendingMessages = followUpMessages;
      continue;
    }
    break;
  }
  await emit({ type: "agent_end", messages: newMessages });
}
```

### 4.7 工具抽象

**源码位置**：`packages/agent/src/types.ts`，第 335~358 行

```typescript
export interface AgentTool<TParameters extends TSchema = TSchema, TDetails = any> extends Tool<TParameters> {
  label: string;
  prepareArguments?: (args: unknown) => Static<TParameters>;
  execute: (toolCallId: string, params: Static<TParameters>, signal?: AbortSignal, onUpdate?: AgentToolUpdateCallback<TDetails>) => Promise<AgentToolResult<TDetails>>;
  executionMode?: ToolExecutionMode;
}
```

### 4.8 Steering / Follow-up 消息队列

**源码位置**：`packages/agent/src/agent.ts`，第 116~147 行

```typescript
class PendingMessageQueue {
  private messages: AgentMessage[] = [];
  constructor(public mode: QueueMode) {}
  enqueue(message: AgentMessage): void { this.messages.push(message); }
  hasItems(): boolean { return this.messages.length > 0; }
  drain(): AgentMessage[] {
    if (this.mode === "all") { const drained = this.messages.slice(); this.messages = []; return drained; }
    const first = this.messages[0]; if (!first) return []; this.messages = this.messages.slice(1); return [first];
  }
}
```

---

## 5. Layer 3: `packages/tui` — 终端 UI 基础设施

### 5.1 解决的问题

> 如何在终端中实现一个**跨平台、高性能、组件化**的 UI 系统，支持 Markdown 渲染、图片显示、键盘输入、Overlay 弹窗？

### 5.2 纯渲染基础设施，与业务零耦合

**源码位置**：`packages/tui/src/index.ts`

TUI 导出的全部是纯 UI 组件和能力：

```typescript
export { Box } from "./components/box.js";
export { Editor, type EditorOptions, type EditorTheme } from "./components/editor.js";
export { Image, type ImageOptions, type ImageTheme } from "./components/image.js";
export { Input } from "./components/input.js";
export { Markdown, type MarkdownTheme } from "./components/markdown.js";
export { SelectList, type SelectItem } from "./components/select-list.js";
export { Spacer } from "./components/spacer.js";
export { Text } from "./components/text.js";
export { TUI, Container, type Component, type Focusable } from "./tui.js";
// ... 键盘、图片协议、终端抽象等
```

**源码位置**：`packages/tui/src/tui.ts`（约 416 行）

`TUI` 类管理组件树和渲染循环：

```typescript
export class TUI {
  private components: Component[] = [];
  // 渲染循环、焦点管理、事件分发
}
```

### 5.3 为什么下层感知不到上层

`packages/tui` 的 `package.json` **没有任何 workspace 依赖**。Markdown 组件不知道自己在渲染 AI 的回答——它只接收字符串和 theme 配置，输出带 ANSI 转义码的文本。

---

## 6. Layer 4: `packages/coding-agent` — 编码 Agent 主应用

### 6.1 解决的问题

> 如何将底层能力组装成一个**完整的编码 Agent 产品**？包括会话管理、工具实现、扩展系统、多模式交互、配置管理、持久化。

### 6.2 SDK 入口：`createAgentSession`

**源码位置**：`packages/coding-agent/src/core/sdk.ts`（约 423 行）

这是整个产品的**组装工厂**：

```typescript
// 第 193 行
export async function createAgentSession(options: CreateAgentSessionOptions = {}): Promise<CreateAgentSessionResult> {
  const cwd = options.cwd ?? options.sessionManager?.getCwd() ?? process.cwd();
  const agentDir = options.agentDir ?? getDefaultAgentDir();

  // 1. 创建或复用 AuthStorage 和 ModelRegistry
  const authPath = options.agentDir ? join(agentDir, "auth.json") : undefined;
  const modelsPath = options.agentDir ? join(agentDir, "models.json") : undefined;

  // 2. 解析模型
  const model = options.model ?? findInitialModel(modelRegistry, settingsManager, authStorage, cwd);

  // 3. 创建 Agent（注入 streamSimple、convertToLlm、beforeToolCall 等）
  const agent = new Agent({
    initialState: { systemPrompt, model, thinkingLevel },
    streamFn: streamSimple,
    convertToLlm,
    beforeToolCall: createBeforeToolCall(agentSessionConfig),
    afterToolCall: createAfterToolCall(agentSessionConfig),
    transformContext: createTransformContext(agentSessionConfig),
    // ...
  });

  // 4. 创建 AgentSession
  const agentSession = new AgentSession(agentSessionConfig);

  // 5. 加载扩展
  const extensionsResult = await loadExtensions(...);

  return { session: agentSession, extensionsResult, modelFallbackMessage };
}
```

### 6.3 业务核心：`AgentSession`

**源码位置**：`packages/coding-agent/src/core/agent-session.ts`（约 3194 行）

`AgentSession` 是**业务编排器**，组合了：

```typescript
export interface AgentSessionConfig {
  agent: Agent;                          // ← 来自 agent 包
  sessionManager: SessionManager;        // ← 会话持久化
  settingsManager: SettingsManager;      // ← 配置管理
  resourceLoader: ResourceLoader;        // ← 资源加载
  modelRegistry: ModelRegistry;          // ← 模型发现
  customTools?: ToolDefinition[];        // ← 自定义工具
  extensionRunnerRef?: { current?: ExtensionRunner }; // ← 扩展运行时
  // ...
}
```

### 6.4 消息转换：`convertToLlm`

**源码位置**：`packages/coding-agent/src/core/messages.ts`，第 148 行

coding-agent 注入了自己的 `convertToLlm` 实现，负责将 `AgentMessage[]` 转换为 LLM 可用的 `Message[]`：

```typescript
export function convertToLlm(messages: AgentMessage[]): Message[] {
  // 过滤掉 UI-only 消息，转换 custom 消息类型
}
```

这体现了**层间数据转换**模式：agent 层操作 `AgentMessage`，ai 层操作 `Message`，转换由上层（coding-agent）负责。

### 6.5 扩展系统：`ExtensionRunner`

**源码位置**：`packages/coding-agent/src/core/extensions/runner.ts`，第 224 行

```typescript
export class ExtensionRunner {
  // 管理扩展生命周期、事件分发、工具注册
}
```

**源码位置**：`packages/coding-agent/src/core/extensions/index.ts`

扩展系统定义了丰富的 Hook 和事件类型：

```typescript
export type SessionStartEvent = { type: "session_start"; reason: "new" | "resume" | "fork"; ... };
export type SessionShutdownEvent = { type: "session_shutdown"; reason: ... };
export type MessageStartEvent = { type: "message_start"; message: AgentMessage };
export type MessageUpdateEvent = { type: "message_update"; ... };
export type MessageEndEvent = { type: "message_end"; message: AgentMessage };
export type ToolExecutionStartEvent = { type: "tool_execution_start"; ... };
export type ToolExecutionEndEvent = { type: "tool_execution_end"; ... };
```

### 6.6 会话持久化：`SessionManager`

**源码位置**：`packages/coding-agent/src/core/session-manager.ts`，第 669 行

```typescript
export class SessionManager {
  // 会话的创建、保存、加载、fork、导入导出
  // 持久化格式：JSONL（每行一条消息）
}
```

### 6.7 多模式运行

**源码位置**：`packages/coding-agent/src/main.ts`（约 727 行）

CLI 入口根据参数选择运行模式：

```typescript
// interactive 模式
const interactiveMode = new InteractiveMode(...);
await interactiveMode.run();

// print 模式（非交互式 stdout 输出）
await runPrintMode(...);

// rpc 模式（JSONL，供 IDE 插件集成）
await runRpcMode(...);
```

### 6.8 Interactive Mode：事件到 UI 的翻译层

**源码位置**：`packages/coding-agent/src/modes/interactive/interactive-mode.ts`（约 5458 行）

这是**最上层**的胶水代码，负责将 `AgentEvent` 翻译为 `TUI` 组件更新：

```typescript
agent.subscribe((event, signal) => {
  switch (event.type) {
    case "message_start":
      // 创建 AssistantMessageComponent，添加到消息列表 Container
    case "message_update":
      // 调用 component.setText() 更新流式内容
    case "message_end":
      // 完成渲染，保存到 transcript
    case "tool_execution_start":
      // 创建 ToolExecutionComponent，显示工具调用卡片
    case "tool_execution_end":
      // 更新工具结果状态
  }
});
```

---

## 7. 层间交互机制：为什么下层感知不到上层

pi-mono 能做到"下层感知不到上层，上层使用下层"，依靠以下五大机制：

### 7.1 机制一：物理依赖隔离（Package Boundary）

TypeScript monorepo 的包边界就是**编译期防火墙**。

- `packages/ai/package.json`：**零 workspace 依赖**
- `packages/agent/package.json`：仅依赖 `@mariozechner/pi-ai`
- `packages/tui/package.json`：**零 workspace 依赖**
- `packages/coding-agent/package.json`：依赖所有下层

> 源码位置：各 package 的 `package.json` 文件

### 7.2 机制二：事件 Sink 模式（Event-Driven Decoupling）

Agent Loop 不直接输出到任何地方，而是通过注入的 `emit` 回调发射事件。

> 源码位置：`packages/agent/src/agent-loop.ts`，第 25 行

```typescript
export type AgentEventSink = (event: AgentEvent) => Promise<void> | void;
```

> 源码位置：`packages/agent/src/agent.ts`，第 224~227 行

```typescript
subscribe(listener: (event: AgentEvent, signal: AbortSignal) => Promise<void> | void): () => void {
  this.listeners.add(listener);
  return () => this.listeners.delete(listener);
}
```

AgentLoop 不知道事件被谁消费——可以是 TUI、日志、RPC 通道、测试 mock。

### 7.3 机制三：配置/回调注入（Strategy Pattern）

Agent 层的所有可变策略都通过 `AgentLoopConfig` 注入。

> 源码位置：`packages/agent/src/types.ts`，第 115~251 行

- `convertToLlm`：上层决定消息如何转换
- `transformContext`：上层决定上下文如何压缩
- `beforeToolCall` / `afterToolCall`：上层决定权限和结果修改
- `shouldStopAfterTurn`：上层决定何时停止
- `getSteeringMessages` / `getFollowUpMessages`：上层提供额外消息

Agent 层只调用这些回调，不关心具体实现。

### 7.4 机制四：Context Snapshot（不可变快照）

Agent Loop 内部操作的是**上下文快照的副本**，不是共享状态。

> 源码位置：`packages/agent/src/agent.ts`，第 415~421 行

```typescript
private createContextSnapshot(): AgentContext {
  return {
    systemPrompt: this._state.systemPrompt,
    messages: this._state.messages.slice(),   // 复制
    tools: this._state.tools.slice(),          // 复制
  };
}
```

这保证了：
- Loop 可以被独立测试（传入任意快照）
- 并发安全（Loop 不修改外部状态）
- 可重入（同一 Agent 可以多次运行不同快照）

### 7.5 机制五：TUI 纯数据驱动

TUI 组件只接收原始数据和渲染指令，不感知业务语义。

> 源码位置：`packages/tui/src/components/markdown.ts`（推断）

```typescript
class Markdown extends Component {
  render(): string {
    // 根据 this.text 和 theme 渲染 ANSI 字符串
    // 不知道这是 AI 回答还是帮助文档
  }
}
```

coding-agent 负责翻译：

> 源码位置：`packages/coding-agent/src/modes/interactive/interactive-mode.ts`

```typescript
// AgentEvent → TUI Component 更新
agent.subscribe((event) => {
  if (event.type === "message_update") {
    markdownComponent.setText(extractText(event.message));
  }
});
```

---

## 8. 关键设计模式总结

| 设计模式 | 应用位置 | 作用 |
|---------|---------|------|
| **Provider 注册表** | `packages/ai/src/api-registry.ts` | 动态发现和调用 LLM Provider |
| **统一事件流** | `packages/ai/src/types.ts` | 屏蔽所有 Provider 的流式协议差异 |
| **事件驱动架构** | `packages/agent/src/agent-loop.ts` | Loop 与 UI/日志/RPC 完全解耦 |
| **策略注入** | `packages/agent/src/types.ts` | Agent 层行为由上层配置决定 |
| **快照隔离** | `packages/agent/src/agent.ts` | Loop 内部状态不污染外部 |
| **组合优于继承** | `packages/coding-agent/src/core/sdk.ts` | AgentSession 组合多个子系统 |
| **数据模型分层转换** | `coding-agent → agent → ai` | 每层有自己的数据模型，边界处转换 |

---

## 9. 数据流全景：一次完整对话的流动

```
[User Input via Keyboard]
    │
    ▼
┌─────────────────────────────────────────────┐
│ coding-agent: InteractiveMode               │  ← 捕获输入，构造 UserMessage
│   (modes/interactive/interactive-mode.ts)   │
└────────────┬────────────────────────────────┘
             │ AgentMessage[]
             ▼
┌─────────────────────────────────────────────┐
│ coding-agent: AgentSession.prompt()         │  ← 业务入口
│   (core/agent-session.ts)                   │
└────────────┬────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────┐
│ agent: Agent.prompt()                       │  ← 创建快照
│   (src/agent.ts:326)                        │
│     ↓                                       │
│ agent: runAgentLoop()                       │  ← ReACT 循环
│   (src/agent-loop.ts:95)                    │
│     ↓                                       │
│ agent: config.convertToLlm()                │  ← AgentMessage[] → Message[]
│   (由 coding-agent 注入)                     │
└────────────┬────────────────────────────────┘
             │ Message[]
             ▼
┌─────────────────────────────────────────────┐
│ agent: streamFunction()                     │  ← 调用注入的 streamFn
│   (默认 streamSimple)                        │
└────────────┬────────────────────────────────┘
             │ Model + Context
             ▼
┌─────────────────────────────────────────────┐
│ ai: streamSimple()                          │  ← 查 Provider 注册表
│   (src/stream.ts:43)                        │
│     ↓                                       │
│ ai: resolveApiProvider()                    │  ← 匹配 Provider
│   (src/api-registry.ts:80)                  │
│     ↓                                       │
│ ai: openai-provider.stream()                │  ← 具体 Provider 实现
│   (src/providers/openai-*.ts)               │
│     ↓                                       │
│ [HTTP → OpenAI API → SSE Stream]            │
│     ↓                                       │
│ ai: EventStream<AssistantMessageEvent>      │  ← 统一事件流
│   (src/utils/event-stream.ts:4)             │
└────────────┬────────────────────────────────┘
             │ text_delta / toolcall_delta / done
             ▼
┌─────────────────────────────────────────────┐
│ agent: streamAssistantResponse()            │  ← 消费事件流
│   (src/agent-loop.ts:252)                   │
│     ↓                                       │
│ agent: emit({type: "message_update"})       │  ← 发射 AgentEvent
│   (src/agent-loop.ts)                       │
└────────────┬────────────────────────────────┘
             │ AgentEvent
             ▼
┌─────────────────────────────────────────────┐
│ agent: Agent.processEvents()                │  ← 更新内部状态
│   (src/agent.ts:554)                        │
└────────────┬────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────┐
│ coding-agent: InteractiveMode (listener)    │  ← 订阅者收到事件
│   (modes/interactive/interactive-mode.ts)   │
│     ↓                                       │
│ coding-agent: AssistantMessageComponent     │  ← 更新组件
└────────────┬────────────────────────────────┘
             │ 纯文本数据
             ▼
┌─────────────────────────────────────────────┐
│ tui: Markdown.render()                      │  ← 渲染 ANSI 到终端
│   (components/markdown.ts)                  │
└─────────────────────────────────────────────┘
```

**关键观察**：数据每经过一层，都会被**转换为本层的数据模型**。ai 层将 HTTP 响应转为 `AssistantMessageEvent`，agent 层将其转为 `AgentEvent`，coding-agent 将其转为 TUI 组件更新，tui 将其转为 ANSI 字符串。**每一层只理解自己层的数据**。

---

## 10. 对我们项目的启示

| pi-mono 的做法 | 我们当前的状态 | 建议 |
|----------------|-------------|------|
| `ai` 包完全独立，Provider 通过注册表动态发现 | Provider 在 `learning_agent/provider/` 下，与 Agent 同包 | 考虑将 Provider 层进一步独立，支持外部注册新 Provider |
| `agent` 包是纯运行时，通过 `AgentEventSink` 发射事件 | Agent Loop 直接 `yield ChatChunk` + `event_bus.publish()` | Event Bus 已很好，但可考虑让 Agent Loop 更纯粹（纯函数 + 事件 sink） |
| `AgentLoopConfig` 注入所有策略 | Hook System 已存在，但配置对象可以更结构化 | 参考 `AgentLoopConfig` 设计，将 Agent 运行时的策略参数显式化为配置对象 |
| `AgentContext` 快照模式 | AgentLoopSession 持有运行时状态，直接操作 Session 对象 | 考虑在 Agent Loop 内部使用不可变快照，事件回传后统一更新外部状态 |
| TUI 与业务完全分离 | Web UI 与业务逻辑有一定耦合 | Web 前端组件可以更纯粹，通过 Event Bus 驱动更新 |
| Extension 在 coding-agent 层实现 | Extension 在 core 层 | pi-mono 的 ExtensionRunner 在应用层负责加载用户扩展；我们的设计可以保留 core 层的基础 Hook + Event，将扩展加载机制提升到应用层 |
| `convertToLlm` 由上层注入 | 上下文组装在 Agent Loop 内部完成 | 考虑将上下文组装逻辑抽取为可注入的策略，便于测试和扩展 |

---

## 附录：关键源码索引

| 主题 | 文件路径 | 行号 |
|------|---------|------|
| 统一消息协议 | `packages/ai/src/types.ts` | 全文件 |
| 统一流式事件协议 | `packages/ai/src/types.ts` | 269~282 |
| Provider 注册表接口 | `packages/ai/src/api-registry.ts` | 23 |
| Provider 查表函数 | `packages/ai/src/api-registry.ts` | 80 |
| stream / streamSimple 入口 | `packages/ai/src/stream.ts` | 25~59 |
| EventStream 类 | `packages/ai/src/utils/event-stream.ts` | 4 |
| Agent 类（状态机） | `packages/agent/src/agent.ts` | 161 |
| AgentState 定义 | `packages/agent/src/types.ts` | 291~316 |
| AgentEvent 定义 | `packages/agent/src/types.ts` | 377~392 |
| AgentLoopConfig 定义 | `packages/agent/src/types.ts` | 115~251 |
| AgentTool 定义 | `packages/agent/src/types.ts` | 335~358 |
| PendingMessageQueue | `packages/agent/src/agent.ts` | 116~147 |
| runAgentLoop 函数 | `packages/agent/src/agent-loop.ts` | 95~118 |
| runLoop 主循环 | `packages/agent/src/agent-loop.ts` | 155~246 |
| streamAssistantResponse | `packages/agent/src/agent-loop.ts` | 252~ |
| Context Snapshot | `packages/agent/src/agent.ts` | 415~421 |
| subscribe 方法 | `packages/agent/src/agent.ts` | 224~227 |
| processEvents | `packages/agent/src/agent.ts` | 554~601 |
| TUI 组件导出 | `packages/tui/src/index.ts` | 全文件 |
| TUI 类 | `packages/tui/src/tui.ts` | 全文件 |
| createAgentSession | `packages/coding-agent/src/core/sdk.ts` | 193~ |
| CreateAgentSessionOptions | `packages/coding-agent/src/core/sdk.ts` | 33~80 |
| AgentSession 类 | `packages/coding-agent/src/core/agent-session.ts` | 全文件 |
| AgentSessionConfig | `packages/coding-agent/src/core/agent-session.ts` | 152~182 |
| AgentSessionEvent | `packages/coding-agent/src/core/agent-session.ts` | 124~143 |
| convertToLlm | `packages/coding-agent/src/core/messages.ts` | 148 |
| SessionManager | `packages/coding-agent/src/core/session-manager.ts` | 669 |
| ExtensionRunner | `packages/coding-agent/src/core/extensions/runner.ts` | 224 |
| 扩展事件类型 | `packages/coding-agent/src/core/extensions/index.ts` | 全文件 |
| InteractiveMode | `packages/coding-agent/src/modes/interactive/interactive-mode.ts` | 全文件 |
| CLI 入口 | `packages/coding-agent/src/main.ts` | 全文件 |
