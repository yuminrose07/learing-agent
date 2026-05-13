# pi-mono Session 状态隔离机制调研报告

> **调研目标**：pi-mono 如何解决"多 Session 状态互相污染"问题
> **调研范围**：`packages/agent/src/agent.ts`、`packages/agent/src/agent-loop.ts`、`packages/coding-agent/src/core/agent-session.ts`、`packages/coding-agent/src/core/agent-session-runtime.ts`、`packages/coding-agent/src/core/session-manager.ts`
> **调研日期**：2026-05-12

---

## 一、核心结论（TL;DR）

**pi-mono 没有"多 Session 共享一个 Agent 实例"的问题，因为它从根本上就不共享。**

它的架构哲学是：
- **每个 Session 拥有独立的 `Agent` 实例**
- **Host 层只持有当前 Session 的引用**，切换 Session 时彻底销毁旧实例、创建新实例
- **没有全局单例式的 Agent Loop** — 低层循环函数是纯函数（stateless），高层 `Agent` 类是 per-session 的

这与当前 Learning Agent 项目中"`AgentLoop` 作为全局单例、接收不同 `session` 参数"的设计形成根本性差异。

---

## 二、pi-mono 架构分层

```
┌─────────────────────────────────────────────┐
│      AgentSessionRuntime（Host 句柄）        │  ← 1 个进程 1 个
│  ┌───────────────────────────────────────┐  │
│  │        AgentSession（会话层）           │  │  ← 每次切换完全重建
│  │  ┌─────────────────────────────────┐  │  │
│  │  │         Agent（核心循环）        │  │  │  ← 全新实例 per session
│  │  │  - _state: messages, tools...   │  │  │
│  │  │  - steeringQueue / followUpQueue│  │  │
│  │  │  - activeRun + AbortController  │  │  │
│  │  └─────────────────────────────────┘  │  │
│  │  - _extensionRunner（独立实例）        │  │
│  │  - _toolRegistry: Map<string, ...>    │  │
│  │  - _turnIndex, _retryAttempt...       │  │
│  │  - 各种 AbortController（独立）        │  │
│  └───────────────────────────────────────┘  │
│  ┌───────────────────────────────────────┐  │
│  │    AgentSessionServices（基础设施）     │  │  ← 可复用，但无 Session 状态
│  │  - modelRegistry, settingsManager     │  │
│  │  - resourceLoader, authStorage        │  │
│  └───────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
```

### 2.1 三层职责划分

| 层级 | 类 | 职责 | 生命周期 |
|------|-----|------|----------|
| **Loop 层** | `Agent` (`packages/agent/src/agent.ts:161`) | 核心 ReACT 循环、transcript 状态、消息队列 | per-session |
| **Session 层** | `AgentSession` (`packages/coding-agent/src/core/agent-session.ts:249`) | 会话业务逻辑、扩展系统、工具注册表、事件持久化 | per-session |
| **Host 层** | `AgentSessionRuntime` (`packages/coding-agent/src/core/agent-session-runtime.ts:67`) | 持有当前 session 引用，协调切换 | 进程级 |

### 2.2 关键设计：Loop 层是纯函数

`packages/agent/src/agent-loop.ts` 中的核心循环函数是**纯函数**，不持有任何状态：

```typescript
// agent-loop.ts:31-150
export function agentLoop(
    context: AgentLoopContext,
    config: AgentLoopConfig,
): AsyncIterable<AgentLoopEvent> { ... }
```

所有状态都通过 `context` 参数传入，所有变更通过返回值 `AgentLoopEvent` 流式输出。这确保了即使多个 session 并发调用 loop 函数，它们之间也天然隔离（各自传各自的 context）。

---

## 三、五大状态隔离机制详解

### 机制一：Fresh Agent Instance per Session（核心机制）

**位置**：`packages/coding-agent/src/core/sdk.ts:279-320`

```typescript
// sdk.ts:279
let agent: Agent;

agent = new Agent({
    initialState: {
        systemPrompt: "",
        model,
        thinkingLevel,
        tools: [],
    },
    convertToLlm: convertToLlmWithBlockImages,
    streamFn: async (model, context, options) => { /* ... */ },
    sessionId: sessionManager.getSessionId(),
    // ...
});
```

每次创建 Session 时，**全新构造**一个 `Agent` 实例。`Agent` 的构造函数内部创建独立的 `MutableAgentState`：

```typescript
// agent.ts:65-92
function createMutableAgentState(initialState?: Partial<...>): MutableAgentState {
    let tools = initialState?.tools?.slice() ?? [];
    let messages = initialState?.messages?.slice() ?? [];
    return {
        systemPrompt: initialState?.systemPrompt ?? "",
        model: initialState?.model ?? DEFAULT_MODEL,
        thinkingLevel: initialState?.thinkingLevel ?? "off",
        get tools() { return tools; },
        set tools(nextTools: AgentTool[]) { tools = nextTools.slice(); },
        get messages() { return messages; },
        set messages(nextMessages: AgentMessage[]) { messages = nextMessages.slice(); },
        isStreaming: false,
        streamingMessage: undefined,
        pendingToolCalls: new Set<string>(),
        errorMessage: undefined,
    };
}
```

注意：**Setters 做数组拷贝**（`.slice()`），防止外部意外修改导致状态泄漏。

**对比当前项目**：`AgentLoop` 在 `__init__` 中初始化 `_failure_tracker`、`_chat_only_mode`、`_chat_only_success_turns`，全局共享。

---

### 机制二：完全替换式 Session 切换（Teardown → Create → Apply）

**位置**：`packages/coding-agent/src/core/agent-session-runtime.ts:149-198`

当用户执行 `/new`、`/resume`、`/fork`、`/import` 时，整个运行时经历三个阶段：

```typescript
// agent-session-runtime.ts:149
private async teardownCurrent(
    reason: SessionShutdownEvent["reason"],
    targetSessionFile?: string
): Promise<void> {
    await emitSessionShutdownEvent(this.session.extensionRunner, {
        type: "session_shutdown",
        reason,
        targetSessionFile,
    });
    this.beforeSessionInvalidate?.();
    this.session.dispose();  // ← 销毁旧 Agent + ExtensionRunner
}

// agent-session-runtime.ts:175
async switchSession(sessionPath: string, ...): Promise<{ cancelled: boolean }> {
    // ...
    const sessionManager = SessionManager.open(sessionPath, undefined, options?.cwdOverride);
    await this.teardownCurrent("resume", sessionManager.getSessionFile());
    this.apply(
        await this.createRuntime({
            cwd: sessionManager.getCwd(),
            agentDir: this.services.agentDir,
            sessionManager,
            sessionStartEvent: { type: "session_start", reason: "resume", previousSessionFile },
        }),
    );
    // ...
}

// agent-session-runtime.ts:159
private apply(result: CreateAgentSessionRuntimeResult): void {
    this._session = result.session;      // 新 AgentSession
    this._services = result.services;    // 新或复用的基础设施
    this._diagnostics = result.diagnostics;
    this._modelFallbackMessage = result.modelFallbackMessage;
}
```

**关键点**：
1. `teardownCurrent()` 调用 `session.dispose()` 彻底清理旧 session
2. `createRuntime()` 内部全新构造 `Agent` + `AgentSession` + `SessionManager`
3. `apply()` 只是**覆盖引用**，旧对象等待 GC 回收

**对比当前项目**：`AgentLoop.run(session, user_input)` 接收不同的 session 对象但复用同一个 `AgentLoop` 实例，导致状态跨 session 共享。

---

### 机制三：Dispose + Invalidate 资源清理

**位置**：`packages/coding-agent/src/core/agent-session.ts:759-786`

```typescript
// agent-session.ts:759
dispose(): void {
    this._extensionRunner.invalidate(
        "This extension ctx is stale after session replacement or reload. ..."
    );
    this._disconnectFromAgent();
    this._eventListeners = [];
    cleanupSessionResources(this.sessionId);
    // audit logger cleanup ...
}
```

**ExtensionRunner.invalidate()**（`packages/coding-agent/src/core/extensions/runner.ts:469-482`）：

```typescript
invalidate(message = "..."): void {
    if (!this.staleMessage) {
        this.staleMessage = message;
        this.runtime.invalidate(message);
    }
}

private assertActive(): void {
    if (this.staleMessage) {
        throw new Error(this.staleMessage);
    }
}
```

所有 Extension API 调用都经过 `assertActive()` 检查。如果持有旧 session 的 extension context 引用并在切换后调用，会**立即抛错**而非静默操作错误的数据。

**对比当前项目**：无 session 切换时的资源清理机制，旧 session 的 tool_calls、failure counts 等直接留在共享实例中。

---

### 机制四：SessionManager 实例隔离

**位置**：`packages/coding-agent/src/core/session-manager.ts:669-680`

```typescript
export class SessionManager {
    private sessionId: string = "";
    private sessionFile: string | undefined;
    private sessionDir: string;
    private cwd: string;
    private persist: boolean;
    private fileEntries: FileEntry[] = [];
    private byId: Map<string, SessionEntry> = new Map();
    private labelsById: Map<string, string> = new Map();
    private labelTimestampsById: Map<string, string> = new Map();
    private leafId: string | null = null;
}
```

每个 `SessionManager` 实例拥有：
- 独立的 `fileEntries`（消息历史）
- 独立的 `byId`（ID 索引）
- 独立的 `labelsById`（标签索引）
- 独立的 `leafId`（当前叶子节点）

**不存在全局 session 索引或共享的消息存储**。

**对比当前项目**：`SessionManager` 虽然也是独立实例，但 `AgentLoop` 是单例，导致运行时的弹性策略状态（failure tracker、chat-only mode）无法隔离。

---

### 机制五：AbortController 级别隔离

**位置**：`packages/coding-agent/src/core/agent-session.ts:249-320`

`AgentSession` 为每个并发/长时操作创建独立的 `AbortController`，全部作为**实例私有字段**：

```typescript
// agent-session.ts 中的私有字段
private _compactionAbortController: AbortController | undefined;
private _autoCompactionAbortController: AbortController | undefined;
private _branchSummaryAbortController: AbortController | undefined;
private _retryAbortController: AbortController | undefined;
private _bashAbortController: AbortController | undefined;
```

这些控制器随 `AgentSession` 实例一起创建和销毁，**永远不会跨 session 共享**。

**对比当前项目**：当前项目虽然没有 AbortController 层面的问题，但 `_chat_only_mode`、`_failure_tracker` 等运行时控制变量相当于 pi-mono 中本应 per-session 的 `AbortController` 级别状态，却被放在了全局单例中。

---

## 四、一个关键发现：pi-mono 不存在"并发多 Session"场景

在调研过程中注意到：**pi-mono 的 Host 层同一时间只持有一个 active session**。它的设计假设是：

> 一个进程 = 一个用户 = 一个当前会话

因此 pi-mono 中：
- **没有并发锁**（没有 `asyncio.Lock` 或 `Mutex`）
- **没有 per-session 锁字典**
- **没有 "同时运行两个 session" 的代码路径**

用户的 `/new`、`/resume`、`/fork` 都是**同步切换**（先 teardown 旧，再创建新），而非并发叠加。

这意味着 pi-mono 的状态隔离是**通过实例隔离自然实现的**，而非通过锁机制保护的。当前 Learning Agent 项目的设计目标是支持**并发多 session**（如 Web 端同时处理多个用户的请求），这意味着我们不能简单照搬 pi-mono 的"完全替换"方案——如果多个 session 同时活跃，频繁创建/销毁 `AgentLoop` 实例开销较大。

---

## 五、与当前项目的差异矩阵

| 维度 | pi-mono | Learning Agent（当前） |
|------|---------|------------------------|
| **Agent 实例数** | 1 per session（切换时重建） | 1 全局单例 |
| **Session 切换方式** | 同步 teardown → create → apply | `run(session, ...)` 传入不同 session |
| **运行时状态位置** | `Agent._state` + `AgentSession` 私有字段 | `AgentLoop` 实例变量 |
| **并发多 session** | 不支持（设计假设单 active session） | 支持（Web 端需求） |
| **失败追踪** | 无实现（无 tool ban / failure tracker） | `ToolFailureTracker` 全局实例 |
| **降级模式** | 无实现（无 chat-only mode） | `_chat_only_mode` 全局布尔值 |
| **锁机制** | 无 | 无（问题 3） |

---

## 六、对问题 1 的启示

### 6.1 不能简单照搬的方案

由于当前项目需要**并发支持多 session**，pi-mono 的"每次切换完全重建 Agent"方案不适合直接照搬。频繁创建/销毁 `AgentLoop`（尤其是它持有 `provider`、`hook_system`、`event_bus` 等重型依赖）会带来性能问题。

### 6.2 可以借鉴的设计原则

| 原则 | pi-mono 做法 | 当前项目可借鉴的改造 |
|------|-------------|----------------------|
| **状态所属权清晰** | `Agent` 拥有 transcript 状态，`AgentSession` 拥有业务运行时状态 | 将 `_failure_tracker`、`_chat_only_mode`、`_chat_only_success_turns` 从 `AgentLoop` 剥离到 per-session 容器 |
| **Setters 做拷贝** | `createMutableAgentState` 中数组 `.slice()` | `SessionRuntimeState` 的修改方法避免直接暴露内部可变引用 |
| **Dispose 模式** | `AgentSession.dispose()` + `ExtensionRunner.invalidate()` | `AgentLoop` 提供 `clear_session_state(session_id)` 供 SessionManager 在删除 session 时回调 |
| **纯函数 Loop** | `agentLoop()` 无状态，全部通过参数 | 如果未来重构，可将核心循环逻辑抽为接近纯函数的形态 |

### 6.3 推荐的修复方向

基于 pi-mono 的调研，结合当前项目的并发需求，问题 1 的修复应遵循以下设计：

```python
class SessionRuntimeState:
    """每个 Session 独立的运行时弹性策略状态。"""
    chat_only_mode: bool = False
    chat_only_success_turns: int = 0
    failure_tracker: ToolFailureTracker  # 每个 session 一个实例
    # 未来可扩展：turn_index、retry_attempt、overflow_recovery_attempted 等

class AgentLoop:
    def __init__(...):
        # ... 现有依赖注入不变 ...
        self._session_states: dict[str, SessionRuntimeState] = {}
    
    def _get_session_state(self, session_id: str) -> SessionRuntimeState:
        if session_id not in self._session_states:
            self._session_states[session_id] = SessionRuntimeState(
                failure_tracker=ToolFailureTracker(
                    window_turns=self._resilience_config.tool_failure_window_turns,
                    threshold=self._resilience_config.tool_failure_threshold,
                )
            )
        return self._session_states[session_id]
    
    def clear_session_state(self, session_id: str) -> None:
        """Session 被删除时清理，防止内存泄漏。"""
        self._session_states.pop(session_id, None)
```

同时需要修改 `ToolFailureTracker`，使其**按 session 隔离**。最简单的方式是为每个 session 创建一个独立的 `ToolFailureTracker` 实例（如上），而非在全局 tracker 中用 `session_id` 做 key。

---

## 七、关键决策建议

1. **状态容器选型**：`dict[str, SessionRuntimeState]` 放在 `AgentLoop` 内，不进入 `LearningSession` 模型。运行时弹性状态不属于业务领域数据，不应持久化。

2. **生命周期管理**：在 `SessionManager.delete_session()` 中调用 `agent_loop.clear_session_state(session_id)`，避免字典无限增长。

3. **并发安全**：与问题 3（无并发控制）一并修复。每个 `session_id` 配一把 `asyncio.Lock`，`run()` 入口获取锁后再读写 `_session_states`。

4. **Pi-mono 的设计哲学值得长期借鉴**：如果未来项目架构演进，可考虑将 `AgentLoop` 拆分为：
   - 无状态的纯函数核心循环（类似 `agent-loop.ts`）
   - per-session 的 `AgentLoopSession` 包装类（持有 `_state` 和运行时状态）
   - `AgentLoop` 作为工厂/调度器，管理多个 `AgentLoopSession`

---

## 附录：关键源码索引

| 文件 | 行号 | 内容 |
|------|------|------|
| `packages/agent/src/agent.ts` | 58-92 | `MutableAgentState` + `createMutableAgentState` |
| `packages/agent/src/agent.ts` | 161-602 | `Agent` 类定义 |
| `packages/agent/src/agent-loop.ts` | 31-150 | 纯函数循环 |
| `packages/coding-agent/src/core/agent-session.ts` | 249-320 | `AgentSession` 类头 + 私有字段 |
| `packages/coding-agent/src/core/agent-session.ts` | 759-786 | `dispose()` 方法 |
| `packages/coding-agent/src/core/agent-session-runtime.ts` | 67-374 | `AgentSessionRuntime` 类 |
| `packages/coding-agent/src/core/agent-session-runtime.ts` | 149-198 | `teardownCurrent()` + `switchSession()` |
| `packages/coding-agent/src/core/session-manager.ts` | 669-680 | `SessionManager` 实例字段 |
| `packages/coding-agent/src/core/extensions/runner.ts` | 469-482 | `invalidate()` + `assertActive()` |
| `packages/coding-agent/src/core/sdk.ts` | 279-320 | `Agent` 构造调用 |
