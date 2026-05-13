# pi-mono 事件、Hook 与扩展系统架构调研

> 调研时间：2026-05-13
> 源码路径：`/Users/roseannk/pi-mono`
> 核心包：`packages/agent`（核心层）、`packages/coding-agent`（应用层）、`packages/ai`（协议层）

---

## 一、整体架构分层

pi-mono 采用 **Monorepo 洋葱分层**架构，由外到内越来越薄、越来越硬：

```
┌─────────────────────────────────────────┐
│  packages/web-ui    packages/tui        │  ← 交互层（可替换）
├─────────────────────────────────────────┤
│  packages/coding-agent                  │  ← 应用层
│    ├─ extensions/    (事件驱动扩展系统)  │
│    ├─ tools/         (内置工具实现)      │
│    ├─ core/          (session-manager)  │
│    └─ compaction/    (上下文压缩)        │
├─────────────────────────────────────────┤
│  packages/agent                         │  ← 核心层（仅 6 个文件）
│    ├─ agent-loop.ts  (核心循环)          │
│    ├─ types.ts       (扩展配置点定义)    │
│    ├─ agent.ts       (状态管理)          │
│    └─ audit-logger.ts(审计日志)          │
├─────────────────────────────────────────┤
│  packages/ai                            │  ← 协议层
│    ├─ stream/        (流抽象)            │
│    └─ providers/     (LLM Provider 实现) │
└─────────────────────────────────────────┘
```

**关键设计原则**：核心层（`packages/agent`）只做"对话流转"这一件事，完全不感知扩展、UI、权限等应用层概念。

---

## 二、事件系统（Event System）

### 2.1 两层事件

pi-mono 的事件系统分为两个层面：

| 层级 | 事件类型 | 发射方 | 消费者 |
|------|---------|--------|--------|
| **Agent Core** | `AgentEvent` | `agent-loop.ts` | UI 层、审计日志、Session 状态同步 |
| **Coding-Agent** | `ExtensionEvent` | `ExtensionRunner` | 扩展（Extensions） |

### 2.2 Agent Core 事件（单向流）

在 `packages/agent/src/agent-loop.ts` 中，核心循环通过 `emit()` 函数在关键节点发射事件：

```typescript
// 生命周期事件
await emit({ type: "agent_start" });
await emit({ type: "turn_start" });
await emit({ type: "agent_end", messages: newMessages });

// 消息流事件（支持 streaming）
await emit({ type: "message_start", message: prompt });
await emit({ type: "message_update", message: { ...partialMessage }, assistantMessageEvent: event });
await emit({ type: "message_end", message: finalMessage });

// 工具执行事件
await emit({ type: "tool_execution_start", toolCallId, toolName, args });
await emit({ type: "tool_execution_update", toolCallId, toolName, args, partialResult });
await emit({ type: "tool_execution_end", toolCallId, toolName, result, isError });
```

这些事件被收集到 `EventStream` 中：

```typescript
function createAgentStream(): EventStream<AgentEvent, AgentMessage[]> {
    return new EventStream<AgentEvent, AgentMessage[]>(
        (event: AgentEvent) => event.type === "agent_end",
        (event: AgentEvent) => (event.type === "agent_end" ? event.messages : []),
    );
}
```

**核心特点**：
- `emit()` 返回 `Promise<void>`，核心循环不等待外部处理完成（除了 `agent_end` 的 settlement）
- 事件携带的是**数据快照**，外部不能修改
- **不能阻断核心流程**

### 2.3 Extension 事件（可干预）

`packages/coding-agent/src/core/extensions/types.ts` 定义了丰富的事件类型：

```typescript
type ExtensionEvent =
  | ResourcesDiscoverEvent
  | SessionEvent          // session_start, session_before_switch, etc.
  | ContextEvent          // context (可修改 messages)
  | BeforeProviderRequestEvent
  | AfterProviderResponseEvent
  | BeforeAgentStartEvent
  | AgentStartEvent
  | AgentEndEvent
  | TurnStartEvent
  | TurnEndEvent
  | MessageStartEvent
  | MessageUpdateEvent
  | MessageEndEvent
  | ToolExecutionStartEvent
  | ToolExecutionUpdateEvent
  | ToolExecutionEndEvent
  | ModelSelectEvent
  | ThinkingLevelSelectEvent
  | UserBashEvent
  | InputEvent            // input (可 transform 用户输入)
  | ToolCallEvent         // tool_call (可 block)
  | ToolResultEvent;      // tool_result (可修改结果)
```

扩展通过 `api.on(eventType, handler)` 订阅：

```typescript
api.on("tool_call", async (event, ctx) => {
    if (event.toolName === "bash" && event.input.command.includes("rm -rf /")) {
        return { block: true, reason: "太危险了" };
    }
});
```

**与 Core 事件的区别**：部分 ExtensionEvent 可以**返回结果**来修改数据或阻断流程（如 `tool_call` 的 `block`、`tool_result` 的修改）。

---

## 三、Hook 系统（配置注入式）

### 3.1 核心设计：没有显式 Hook 注册表

pi-mono 的 agent-core **没有 `HookSystem` 或注册表**。扩展能力通过 `AgentLoopConfig` 上的**可选回调函数**（配置注入）实现：

```typescript
// packages/agent/src/types.ts
interface AgentLoopConfig {
    // 必须：AgentMessage[] → LLM Message[]
    convertToLlm: (messages: AgentMessage[]) => Message[] | Promise<Message[]>;
    
    // 可选：上下文变换（如 compaction、注入）
    transformContext?: (messages: AgentMessage[], signal?: AbortSignal) => Promise<AgentMessage[]>;
    
    // 可选：工具执行前拦截
    beforeToolCall?: (context: BeforeToolCallContext, signal?: AbortSignal) => Promise<BeforeToolCallResult | undefined>;
    
    // 可选：工具执行后改写结果
    afterToolCall?: (context: AfterToolCallContext, signal?: AbortSignal) => Promise<AfterToolCallResult | undefined>;
    
    // 可选：当前 turn 后是否停止
    shouldStopAfterTurn?: (context: ShouldStopAfterTurnContext) => boolean | Promise<boolean>;
    
    // 可选：中途注入 steering 消息
    getSteeringMessages?: () => Promise<AgentMessage[]>;
    
    // 可选：follow-up 消息
    getFollowUpMessages?: () => Promise<AgentMessage[]>;
    
    // 可选：流函数覆盖
    streamFn?: StreamFn;
}
```

### 3.2 核心循环中的调用点

```typescript
// streamAssistantResponse 中
let messages = context.messages;
if (config.transformContext) {
    messages = await config.transformContext(messages, signal);
}
const llmMessages = await config.convertToLlm(messages);

// executeToolCalls 中（prepareToolCall）
if (config.beforeToolCall) {
    const beforeResult = await config.beforeToolCall({ assistantMessage, toolCall, args, context }, signal);
    if (beforeResult?.block) {
        return { kind: "immediate", result: createErrorToolResult(...), isError: true };
    }
}

// finalizeExecutedToolCall 中
if (config.afterToolCall) {
    const afterResult = await config.afterToolCall({ assistantMessage, toolCall, args, result, isError, context }, signal);
    if (afterResult) {
        result = { content: afterResult.content ?? result.content, ... };
    }
}
```

### 3.3 配置注入 vs 注册式 Hook

| 维度 | pi-mono 配置注入 | 典型注册式 Hook |
|------|-----------------|----------------|
| 扩展时机 | 创建 Agent 时一次性传入 | 运行时动态注册 |
| 核心如何发现 | 直接调用已知函数引用 | 遍历全局注册表 |
| 扩展能否新增 | 创建后不可变更 | 随时注册/注销 |
| 核心层感知 | 只认 `config` 对象 | 内置 `HookSystem` |
| 测试方式 | 直接传入 mock 函数 | mock 整个注册表 |

### 3.4 "桥接"：应用层连接核心层与扩展系统

核心层只有一个 `beforeToolCall` 函数位，但扩展系统有很多扩展。`AgentSession` 在应用层做**桥接**：

```typescript
// packages/coding-agent/src/core/agent-session.ts
private _installAgentToolHooks(): void {
    this.agent.beforeToolCall = async ({ toolCall, args }) => {
        const runner = this._extensionRunner;
        if (!runner.hasHandlers("tool_call")) {
            return undefined;
        }
        
        // 将核心层参数包装为 ExtensionEvent
        const result = await runner.emitToolCall({
            type: "tool_call",
            toolName: toolCall.name,
            toolCallId: toolCall.id,
            input: args as Record<string, unknown>,
        });
        
        // 翻译回核心层能理解的格式
        if (result?.block) {
            return { block: true, reason: result.reason };
        }
        return undefined;
    };
}
```

**桥接的本质**：把多个扩展在 `tool_call` 事件上的 handler 汇总起来，包装成**一个函数**塞给核心层的 `beforeToolCall`。

---

## 四、扩展系统（Extension System）

### 4.1 扩展是应用层概念

`packages/coding-agent` 的扩展系统非常完整，但 **`packages/agent` 完全不感知扩展**。

### 4.2 扩展加载机制

```typescript
// packages/coding-agent/src/core/extensions/loader.ts
const factory = await loadExtensionModule(resolvedPath);  // 用 jiti 动态加载 TS 模块
const extension = createExtension(extensionPath, resolvedPath);
const api = createExtensionAPI(extension, runtime, cwd, eventBus);
await factory(api);  // 调用扩展工厂函数
```

扩展是一个**工厂函数**，接收 `ExtensionAPI`：

```typescript
export default function myExtension(api: ExtensionAPI) {
    api.on("tool_call", handleToolCall);
    api.registerTool(myToolDefinition);
    api.registerCommand("/mycommand", { handler: ... });
    api.registerShortcut("ctrl+x", { handler: ... });
    api.registerFlag("enable_strict_mode", { type: "boolean", default: false });
}
```

### 4.3 扩展能注册什么

| API | 作用 |
|-----|------|
| `api.on(event, handler)` | 订阅生命周期/拦截事件 |
| `api.registerTool(tool)` | 注册 LLM 可调用的工具 |
| `api.registerCommand(name, options)` | 注册斜杠命令 |
| `api.registerShortcut(key, options)` | 注册键盘快捷键 |
| `api.registerFlag(name, options)` | 注册配置开关 |
| `api.registerMessageRenderer(type, renderer)` | 注册自定义消息渲染器 |
| `api.registerProvider(name, config)` | 注册 LLM Provider |

### 4.4 扩展上下文

```typescript
interface ExtensionContext {
    ui: ExtensionUIContext;           // 对话框、通知、widget
    hasUI: boolean;                   // 当前是否有 UI
    cwd: string;
    sessionManager: ReadonlySessionManager;
    modelRegistry: ModelRegistry;
    model: Model | undefined;
    isIdle(): boolean;
    signal: AbortSignal | undefined;
    abort(): void;
    hasPendingMessages(): boolean;
    shutdown(): void;
    getContextUsage(): ContextUsage | undefined;
    compact(options?: CompactOptions): void;
    getSystemPrompt(): string;
}
```

### 4.5 隔离与生命周期

- 每个扩展有独立的路径和 `sourceInfo`
- 扩展 reload 时旧的 context 会被 `invalidate()`，后续调用抛异常
- 错误通过 `emitError` 广播，不拖垮其他扩展
- `ExtensionRunner.emit()` 中每个 handler 有独立的 try-catch

---

## 五、权限系统（应用层实现）

pi-mono 的权限系统不在 core 层，而是由 coding-agent 层通过 `tool_call` / `tool_result` 事件实现。其设计与 Claude Code 的三级决策模型（allow/ask/deny）类似，但实现方式不同：

```typescript
// 扩展在 tool_call 事件中做权限检查
api.on("tool_call", async (event, ctx) => {
    const decision = engine.checkTool(event.toolName, event.input);
    if (decision.behavior === PermissionBehavior.DENY) {
        return { block: true, reason: decision.message };
    } else if (decision.behavior === PermissionBehavior.ASK) {
        return { block: true, reason: decision.message }; // 或显示确认对话框
    }
});
```

---

## 六、核心设计启示

1. **核心层要小且硬**：`packages/agent` 只有 6 个文件，~1500 行，只做消息流转
2. **配置注入优于全局注册**：核心层不遍历注册表，只调用已知的 config 函数
3. **事件与 Hook 严格分离**：Event 单向通知，Config Hook 双向拦截
4. **扩展系统上移**：Extension 是应用层概念，通过桥接间接影响核心
5. **契约先行**：每个 config 函数都有严格的契约注释（must not throw, must return...）
