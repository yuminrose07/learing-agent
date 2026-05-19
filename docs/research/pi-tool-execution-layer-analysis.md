# pi 工具调用分层架构调研报告

> 调研目标：确定 pi 执行工具调用发生在哪一层，以及层间职责如何划分。  
> 调研范围：`/Users/roseannk/pi-mono`  
> 调研日期：2026-05-13  
> 核心文件：`packages/agent/src/agent-loop.ts`、`packages/coding-agent/src/core/tools/*.ts`

---

## 1. 结论摘要

pi 的工具调用架构分为**两个严格分离的层面**：

| 层面 | 所在包 | 核心职责 |
|------|--------|---------|
| **工具调度编排层** | `packages/agent` | 发现 tool calls、参数校验、串行/并行调度、Hook 拦截、结果组装、事件发射 |
| **工具具体实现层** | `packages/coding-agent` | 定义 `read`/`write`/`bash`/`edit`/`find`/`grep`/`ls` 等工具的 `execute()` 方法 |

**核心设计原则**：`agent` 层是纯 ReACT 运行时，只操作 `AgentTool` 接口；具体工具的实现由 `coding-agent` 层注入。这种分离使得 `agent` 层可被任何需要 ReACT 循环的场景复用（不限于编码）。

---

## 2. 调度编排层：`packages/agent`

### 2.1 核心入口

**源码位置**：`packages/agent/src/agent-loop.ts`，第 350 行

```typescript
async function executeToolCalls(
    currentContext: AgentContext,
    assistantMessage: AssistantMessage,
    config: AgentLoopConfig,
    signal: AbortSignal | undefined,
    emit: AgentEventSink,
): Promise<ExecutedToolCallBatch> {
    const toolCalls = assistantMessage.content.filter((c) => c.type === "toolCall");
    const hasSequentialToolCall = toolCalls.some(
        (tc) => currentContext.tools?.find((t) => t.name === tc.name)?.executionMode === "sequential",
    );
    if (config.toolExecution === "sequential" || hasSequentialToolCall) {
        return executeToolCallsSequential(...);
    }
    return executeToolCallsParallel(...);
}
```

### 2.2 串行执行路径

**源码位置**：`packages/agent/src/agent-loop.ts`，第 372~422 行

```typescript
async function executeToolCallsSequential(...) {
    for (const toolCall of toolCalls) {
        // 1. 发射 tool_execution_start 事件
        await emit({ type: "tool_execution_start", toolCallId, toolName, args });

        // 2. 准备工具调用（参数校验 + beforeToolCall Hook）
        const preparation = await prepareToolCall(currentContext, assistantMessage, toolCall, config, signal);

        // 3. 执行（或立即返回错误）
        let finalized: FinalizedToolCallOutcome;
        if (preparation.kind === "immediate") {
            finalized = { toolCall, result: preparation.result, isError: preparation.isError };
        } else {
            const executed = await executePreparedToolCall(preparation, signal, emit, config.auditLogger);
            finalized = await finalizeExecutedToolCall(currentContext, assistantMessage, preparation, executed, config, signal);
        }

        // 4. 发射 tool_execution_end + tool_result 消息
        await emitToolExecutionEnd(finalized, emit);
        const toolResultMessage = createToolResultMessage(finalized);
        await emitToolResultMessage(toolResultMessage, emit);
    }

    return { messages, terminate: shouldTerminateToolBatch(finalizedCalls) };
}
```

### 2.3 并行执行路径

**源码位置**：`packages/agent/src/agent-loop.ts`，第 424~483 行

并行模式的关键差异：
- 预检（prepareToolCall）仍然**串行**进行（按 assistant message 中的顺序）
- 实际 `execute()` 调用**并发**执行（通过 `Promise.all`）
- `tool_execution_end` 事件按**完成顺序**发射
- `tool_result` 消息按**原始顺序**组装后回传

```typescript
async function executeToolCallsParallel(...) {
    for (const toolCall of toolCalls) {
        // 串行预检
        const preparation = await prepareToolCall(...);
        if (preparation.kind === "immediate") {
            // 立即返回错误结果
            await emitToolExecutionEnd(finalized, emit);
            finalizedCalls.push(finalized);
            continue;
        }
        // 将实际执行封装为闭包，稍后并发调用
        finalizedCalls.push(async () => {
            const executed = await executePreparedToolCall(preparation, signal, emit, config.auditLogger);
            const finalized = await finalizeExecutedToolCall(...);
            await emitToolExecutionEnd(finalized, emit);
            return finalized;
        });
    }

    // 并发执行所有非立即返回的工具调用
    const orderedFinalizedCalls = await Promise.all(
        finalizedCalls.map((entry) => (typeof entry === "function" ? entry() : Promise.resolve(entry)))
    );

    // 按原始顺序发射 tool_result 消息
    for (const finalized of orderedFinalizedCalls) {
        const toolResultMessage = createToolResultMessage(finalized);
        await emitToolResultMessage(toolResultMessage, emit);
        messages.push(toolResultMessage);
    }
}
```

### 2.4 工具准备阶段：`prepareToolCall`

**源码位置**：`packages/agent/src/agent-loop.ts`，第 553~600 行

```typescript
async function prepareToolCall(...) {
    // 1. 在 context.tools 中查找对应工具
    const tool = currentContext.tools?.find((t) => t.name === toolCall.name);
    if (!tool) {
        return { kind: "immediate", result: createErrorToolResult(`Tool ${toolCall.name} not found`), isError: true };
    }

    // 2. 参数预处理（prepareArguments shim）
    const preparedToolCall = prepareToolCallArguments(tool, toolCall, config.auditLogger);

    // 3. 参数校验（Typebox schema validation）
    const validatedArgs = validateToolArguments(tool, preparedToolCall);

    // 4. beforeToolCall Hook（上层可拦截）
    if (config.beforeToolCall) {
        const beforeResult = await config.beforeToolCall({ assistantMessage, toolCall, args: validatedArgs, context: currentContext }, signal);
        if (beforeResult?.block) {
            return { kind: "immediate", result: createErrorToolResult(beforeResult.reason || "blocked"), isError: true };
        }
    }

    return { kind: "prepared", toolCall, tool, args: validatedArgs };
}
```

### 2.5 实际执行阶段：`executePreparedToolCall`

**源码位置**：`packages/agent/src/agent-loop.ts`，第 605~650 行（约）

```typescript
async function executePreparedToolCall(preparation, signal, emit, auditLogger) {
    const { tool, args } = preparation;
    // 调用工具注入的 execute 方法
    // 具体实现在 packages/coding-agent/src/core/tools/ 中定义
    const result = await tool.execute(toolCallId, args, signal, (partialResult) => {
        // 流式更新回调：发射 tool_execution_update 事件
        emit({ type: "tool_execution_update", toolCallId, toolName, args, partialResult });
    });
    return { result, isError: false };
}
```

### 2.6 结果收尾阶段：`finalizeExecutedToolCall`

**源码位置**：`packages/agent/src/agent-loop.ts`，约第 660 行附近

```typescript
async function finalizeExecutedToolCall(currentContext, assistantMessage, preparation, executed, config, signal) {
    // 1. 调用 afterToolCall Hook（上层可修改结果）
    if (config.afterToolCall) {
        const afterResult = await config.afterToolCall({...}, signal);
        if (afterResult) {
            // 应用修改：content / details / isError / terminate
        }
    }
    return { toolCall: preparation.toolCall, result: executed.result, isError: executed.isError };
}
```

### 2.7 `AgentTool` 接口定义

**源码位置**：`packages/agent/src/types.ts`，第 335~358 行

```typescript
export interface AgentTool<TParameters extends TSchema = TSchema, TDetails = any> extends Tool<TParameters> {
    /** 人类可读的标签，用于 UI 显示 */
    label: string;

    /** 参数预处理 shim（在 schema 校验前执行） */
    prepareArguments?: (args: unknown) => Static<TParameters>;

    /** 执行工具调用。失败时 throw，不将错误编码到 content 中 */
    execute: (
        toolCallId: string,
        params: Static<TParameters>,
        signal?: AbortSignal,
        onUpdate?: AgentToolUpdateCallback<TDetails>,
    ) => Promise<AgentToolResult<TDetails>>;

    /** 执行模式覆盖：sequential（串行）或 parallel（可并发） */
    executionMode?: ToolExecutionMode;
}
```

---

## 3. 工具实现层：`packages/coding-agent`

### 3.1 工具工厂函数列表

所有具体工具的实现在 `packages/coding-agent/src/core/tools/` 目录下，每个工具暴露一个工厂函数返回 `AgentTool` 实例。

| 工具 | 文件 | 工厂函数 | 定义行号 |
|------|------|---------|---------|
| `read` | `tools/read.ts` | `createReadTool(cwd)` | 360 |
| `write` | `tools/write.ts` | `createWriteTool(cwd)` | 279 |
| `edit` | `tools/edit.ts` | `createEditTool(cwd)` | 487 |
| `bash` | `tools/bash.ts` | `createBashTool(cwd)` | 438 |
| `find` | `tools/find.ts` | `createFindTool(cwd)` | ~对应行 |
| `grep` | `tools/grep.ts` | `createGrepTool(cwd)` | ~对应行 |
| `ls` | `tools/ls.ts` | `createLsTool(cwd)` | ~对应行 |

### 3.2 `read` 工具示例

**源码位置**：`packages/coding-agent/src/core/tools/read.ts`，第 360 行

```typescript
export function createReadTool(cwd: string, options?: ReadToolOptions): AgentTool<typeof readSchema> {
    return {
        name: "read",
        label: "Read file",
        parameters: readSchema,
        async execute(toolCallId, params, signal, onUpdate) {
            // 实际文件读取逻辑
            const content = await fs.readFile(resolvePath(cwd, params.file), "utf-8");
            return {
                content: [{ type: "text", text: content }],
                details: { path: params.file, size: content.length },
            };
        },
    };
}
```

### 3.3 `bash` 工具示例

**源码位置**：`packages/coding-agent/src/core/tools/bash.ts`，第 438 行

```typescript
export function createBashTool(cwd: string, options?: BashToolOptions): AgentTool<typeof bashSchema> {
    return {
        name: "bash",
        label: "Run command",
        parameters: bashSchema,
        executionMode: "sequential",  // bash 必须串行执行
        async execute(toolCallId, params, signal, onUpdate) {
            // 实际命令执行逻辑
            const result = await execCommand(params.command, { cwd, signal });
            return {
                content: [{ type: "text", text: result.stdout }],
                details: { exitCode: result.exitCode, stderr: result.stderr },
            };
        },
    };
}
```

### 3.4 工具注册与注入

工具不是在 `agent` 层硬编码的，而是在 `coding-agent` 层创建后注入到 `Agent` 中。

**源码位置**：`packages/coding-agent/src/core/sdk.ts`，第 193 行附近

```typescript
export async function createAgentSession(options = {}): Promise<CreateAgentSessionResult> {
    // ...

    // 1. 创建所有具体工具实例
    const allTools = [
        createReadTool(cwd),
        createWriteTool(cwd),
        createEditTool(cwd),
        createBashTool(cwd),
        createFindTool(cwd),
        createGrepTool(cwd),
        createLsTool(cwd),
        ...(options.customTools ?? []),
    ];

    // 2. 创建 Agent，将工具作为初始状态注入
    const agent = new Agent({
        initialState: {
            systemPrompt,
            model,
            thinkingLevel,
            tools: allTools,  // ← 工具在此注入
        },
        streamFn: streamSimple,
        convertToLlm,
        beforeToolCall: createBeforeToolCall(agentSessionConfig),  // 权限控制 Hook
        afterToolCall: createAfterToolCall(agentSessionConfig),    // 结果后处理 Hook
        transformContext: createTransformContext(agentSessionConfig),
        // ...
    });

    // ...
}
```

### 3.5 工具定义的另一种形态：`ToolDefinition`

除了 `AgentTool`（直接包含 `execute` 方法），`coding-agent` 还定义了一套 `ToolDefinition` 类型，用于扩展系统的工具注册。

**源码位置**：`packages/coding-agent/src/core/tools/index.ts`，第 156 行

```typescript
export function createAllToolDefinitions(cwd: string, options?: ToolsOptions): Record<ToolName, ToolDef> {
    // 返回工具定义映射，供扩展系统注册
}
```

---

## 4. 工具执行完整流程

以下是一次 tool call 从发现到结果回传的完整流程：

```
[LLM 返回 AssistantMessage，包含 tool calls]
    │
    ▼
┌─────────────────────────────────────────────┐
│ agent-loop.ts: runLoop()                    │
│   发现 message.content 中有 toolCall 类型    │
│   调用 executeToolCalls()                    │
└────────────┬────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────┐
│ agent-loop.ts: executeToolCalls()           │
│   判断串行/并行模式                           │
│   调用 executeToolCallsSequential/Parallel   │
└────────────┬────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────┐
│ agent-loop.ts: prepareToolCall()            │
│   1. 在 context.tools 中按 name 查找工具      │
│   2. prepareToolCallArguments()             │
│   3. validateToolArguments()                │
│   4. config.beforeToolCall() Hook           │
│   5. 返回 {kind: "prepared", tool, args}     │
└────────────┬────────────────────────────────┘
             │ PreparedToolCall
             ▼
┌─────────────────────────────────────────────┐
│ agent-loop.ts: executePreparedToolCall()    │
│   调用 tool.execute(toolCallId, args, ...)  │
│   ← 这是跨越层边界的关键调用                  │
└────────────┬────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────┐
│ coding-agent: createReadTool()/createBashTool│
│   执行实际业务逻辑（读文件/执行命令）          │
│   返回 AgentToolResult {content, details}    │
└────────────┬────────────────────────────────┘
             │ AgentToolResult
             ▼
┌─────────────────────────────────────────────┐
│ agent-loop.ts: finalizeExecutedToolCall()   │
│   1. config.afterToolCall() Hook            │
│   2. 构造 FinalizedToolCallOutcome           │
└────────────┬────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────┐
│ agent-loop.ts: createToolResultMessage()    │
│   构造 ToolResultMessage                     │
│   emit({type: "tool_execution_end", ...})    │
│   emit({type: "message_end", message})       │
└────────────┬────────────────────────────────┘
             │ ToolResultMessage
             ▼
┌─────────────────────────────────────────────┐
│ agent-loop.ts: runLoop()                    │
│   将 ToolResultMessage 追加到 currentContext  │
│   进入下一轮 ReACT（如果有更多 tool calls）   │
└─────────────────────────────────────────────┘
```

---

## 5. 设计模式分析

### 5.1 接口隔离

`agent` 层只依赖 `AgentTool` 接口（定义在 `packages/agent/src/types.ts`），不依赖任何具体工具的实现。这符合**依赖倒置原则**（DIP）：高层模块不应依赖低层模块，两者都应依赖抽象。

### 5.2 工厂模式 + 依赖注入

`coding-agent` 层通过工厂函数创建工具实例，在 `createAgentSession()` 中注入到 `Agent` 的初始状态。`Agent` 运行时从 `AgentContext.tools` 数组中按 `name` 查找并调用。

### 5.3 策略模式：串行 vs 并行

工具执行模式（`sequential` / `parallel`）通过 `AgentLoopConfig.toolExecution` 配置，以及每个工具的 `executionMode` 属性共同决定。调度逻辑完全在 `agent` 层，具体工具只需声明自己的偏好。

### 5.4 Hook 拦截机制

`beforeToolCall` 和 `afterToolCall` 是 `AgentLoopConfig` 中的可选回调，由 `coding-agent` 层注入。这使得 `coding-agent` 可以在不修改 `agent` 层代码的情况下：
- 拦截危险操作（如删除文件前确认）
- 修改工具结果（如格式化输出）
- 记录审计日志

---

## 6. 启示

### 6.1 与我们的项目对比

| 维度 | pi | 我们的项目 |
|------|-----|-----------|
| 工具接口 | `AgentTool`（含 `label`、`execute`、`executionMode`） | `ToolDefinition`（含 `handler`） |
| 调度层 | `agent` 包独立 | `AgentLoop` 在 `learning_agent/agent/` 下 |
| 实现层 | `coding-agent` 包 | `learning_agent/extensions/` |
| 串行/并行 | 内置支持，按工具声明决定 | 当前未明确区分 |
| 流式工具更新 | `onUpdate` 回调 | 未实现 |
| before/after Hook | `AgentLoopConfig` 注入 | `HookSystem.before_tool_execute` / `after_tool_execute` |

### 6.2 值得借鉴的点

1. **调度层与实现层分离**：`agent` 层只做 ReACT 循环调度，工具实现完全外置。我们的 `AgentLoop` 可以进一步瘦身，将工具执行的具体调度逻辑（如串行/并行判断）收拢到一个专门的调度器中。

2. **`executionMode` 声明**：每个工具可以声明自己是否可以并行执行（如 `bash` 必须串行，`read` 可以并行）。我们的工具注册表可以增加此属性。

3. **流式工具更新**：`AgentTool.execute` 接收 `onUpdate` 回调，允许工具在执行过程中发射进度更新（如长时间运行的 bash 命令）。我们的 `ToolCall` 模型可以扩展此能力。

4. **参数预处理 `prepareArguments`**：在 schema validation 之前对参数做兼容性处理。我们的 `ToolDefinition` 可以增加此钩子。

---

## 附录：关键源码索引

| 主题 | 文件路径 | 行号 |
|------|---------|------|
| 工具调度主入口 | `packages/agent/src/agent-loop.ts` | 350 |
| 串行执行 | `packages/agent/src/agent-loop.ts` | 372 |
| 并行执行 | `packages/agent/src/agent-loop.ts` | 424 |
| 工具准备 | `packages/agent/src/agent-loop.ts` | 553 |
| 实际执行 | `packages/agent/src/agent-loop.ts` | 605 |
| 结果收尾 | `packages/agent/src/agent-loop.ts` | ~660 |
| `AgentTool` 接口 | `packages/agent/src/types.ts` | 335 |
| `AgentToolResult` 接口 | `packages/agent/src/types.ts` | 319 |
| `ToolExecutionMode` 类型 | `packages/agent/src/types.ts` | 36 |
| `beforeToolCall` Hook 定义 | `packages/agent/src/types.ts` | 233 |
| `afterToolCall` Hook 定义 | `packages/agent/src/types.ts` | 247 |
| `read` 工具实现 | `packages/coding-agent/src/core/tools/read.ts` | 360 |
| `write` 工具实现 | `packages/coding-agent/src/core/tools/write.ts` | 279 |
| `edit` 工具实现 | `packages/coding-agent/src/core/tools/edit.ts` | 487 |
| `bash` 工具实现 | `packages/coding-agent/src/core/tools/bash.ts` | 438 |
| 工具注册入口 | `packages/coding-agent/src/core/tools/index.ts` | 156 |
| Agent 创建与工具注入 | `packages/coding-agent/src/core/sdk.ts` | 193 |
