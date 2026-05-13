# 技术设计文档：AgentLoopSession 架构重构（方案 A）

> **目标**：解决实例级状态污染（问题 1）、并发控制缺失（问题 3）、Span 泄漏（问题 12）
> **设计原则**：参考 pi-mono 的分层隔离哲学，将 per-session 运行时状态从全局单例中彻底剥离
> **文档日期**：2026-05-12

---

## 一、背景与目标

### 1.1 当前问题

`AgentLoop` 作为全局单例，以下关键状态均为**实例级变量**，而非按 `session_id` 隔离：

```python
# learning_agent/agent/agent_loop.py:111-117
self._failure_tracker = ToolFailureTracker(...)
self._chat_only_mode = False
self._chat_only_success_turns = 0
```

导致：
- Session A 的工具连续失败 → `_chat_only_mode = True` → Session B 也无法使用工具
- Session A 中 `tool_x` 被 ban → Session B 调用同一工具也被拒绝
- `_chat_only_success_turns` 被所有 Session 共享，恢复逻辑错乱
- `turn_count` 跨 Session 污染 `ToolFailureTracker` 的滑动窗口计算

### 1.2 设计目标

| 目标 | 说明 |
|------|------|
| **状态隔离** | 每个 session 的运行时状态（failure tracker、chat-only mode、恢复计数）完全独立 |
| **并发安全** | 同 session 的多次调用串行化，不同 session 的调用可并行 |
| **生命周期管理** | session 删除时，其运行时状态被精确回收，防止内存泄漏 |
| **架构对齐 pi-mono** | 借鉴 pi-mono 的"per-session 实例隔离"哲学，适配到并发多 session 场景 |

---

## 二、pi-mono 架构深度分析

pi-mono 的架构核心可以用一句话概括：**状态向下流动，每层只管理自己的状态，上层通过"销毁→重建"实现隔离。**

### 2.1 整体分层架构

```
┌─────────────────────────────────────────────┐
│  Host Layer: AgentSessionRuntime            │  ← 进程级单例，只持有"当前 session"引用
│  - 管理 session 切换（/new, /resume, /fork）  │
│  - 通过工厂模式重建完整运行时                 │
└─────────────────────────────────────────────┘
                    │
                    ▼ teardown → create → apply
┌─────────────────────────────────────────────┐
│  Session Layer: AgentSession                │  ← per-session，被 Host 持有引用
│  - 包装 Agent 实例                           │
│  - 管理扩展系统（ExtensionRunner）           │
│  - 管理持久化、compaction、retry、bash       │
│  - 事件订阅与转发                            │
└─────────────────────────────────────────────┘
                    │
                    ▼ 事件驱动状态同步
┌─────────────────────────────────────────────┐
│  Loop Layer: Agent                          │  ← per-session，被 AgentSession 持有
│  - MutableAgentState（transcript + 运行时）  │
│  - PendingMessageQueue（steering/followUp）  │
│  - activeRun（单次运行并发守卫）             │
│  - 事件监听器集合                            │
└─────────────────────────────────────────────┘
                    │
                    ▼ 纯函数调用
┌─────────────────────────────────────────────┐
│  Pure Loop: agent-loop.ts                   │  ← 完全无状态
│  - 接收 AgentContext + AgentLoopConfig       │
│  - 返回 AgentEvent 流 + 新消息列表           │
│  - 不持有任何持久状态                        │
└─────────────────────────────────────────────┘
```

### 2.2 关键设计决策详解

#### 决策 1：Loop 层是纯函数（agent-loop.ts）

**文件**：`packages/agent/src/agent-loop.ts:31-224`

```typescript
// 纯函数入口：接收完整上下文快照，返回事件流
export function agentLoop(
    prompts: AgentMessage[],
    context: AgentContext,        // { systemPrompt, messages, tools }
    config: AgentLoopConfig,       // { model, callbacks, execution mode }
    signal?: AbortSignal,
    streamFn?: StreamFn,
): EventStream<AgentEvent, AgentMessage[]>;

// 继续模式：从已有 transcript 继续
export function agentLoopContinue(
    context: AgentContext,
    config: AgentLoopConfig,
    signal?: AbortSignal,
    streamFn?: StreamFn,
): EventStream<AgentEvent, AgentMessage[]>;
```

**为什么这是关键设计？**

纯函数 Loop 意味着：
1. **没有隐藏的共享状态** — 所有输入通过参数显式传入
2. **可测试性极高** — 给定相同的 context + config，输出是确定的
3. **并发天然安全** — 多个 session 同时调用各自传各自的 context，互不干扰
4. **Caller 拥有状态所有权** — `Agent` 负责将事件还原为自己的 `_state`

**状态流**：
```
Agent._state (mutable) 
    │ 调用前：创建 context 快照（浅拷贝 messages/tools）
    ▼
agentLoop(context, config)
    │ 内部：mutate local context.messages（推入新消息）
    ▼
emit events → Agent.processEvents(event)
    │ 事件还原：更新 _state.messages, _state.pendingToolCalls 等
    ▼
Agent._state (已更新)
```

#### 决策 2：Agent 是"状态包装器 + 运行守卫"（agent.ts）

**文件**：`packages/agent/src/agent.ts:194-602`

`Agent` 类不做 LLM 协议细节，它只做三件事：

**（1）持有 MutableAgentState**

```typescript
// agent.ts:58-92
type MutableAgentState = Omit<AgentState, "isStreaming" | "streamingMessage" | "pendingToolCalls" | "errorMessage"> & {
    isStreaming: boolean;
    streamingMessage?: AgentMessage;
    pendingToolCalls: Set<string>;
    errorMessage?: string;
};

function createMutableAgentState(initialState?: Partial<...>): MutableAgentState {
    let tools = initialState?.tools?.slice() ?? [];
    let messages = initialState?.messages?.slice() ?? [];
    return {
        systemPrompt: initialState?.systemPrompt ?? "",
        model: initialState?.model ?? DEFAULT_MODEL,
        thinkingLevel: initialState?.thinkingLevel ?? "off",
        get tools() { return tools; },
        set tools(nextTools: AgentTool<any>[]) {
            tools = nextTools.slice();   // ← 防御性拷贝
        },
        get messages() { return messages; },
        set messages(nextMessages: AgentMessage[]) {
            messages = nextMessages.slice(); // ← 防御性拷贝
        },
        isStreaming: false,
        streamingMessage: undefined,
        pendingToolCalls: new Set<string>(),
        errorMessage: undefined,
    };
}
```

**关键设计：getter/setter + 防御性拷贝**
- 外部读取 `_state.tools` 得到的是内部数组引用（可变）
- 但写入时会 `.slice()` 创建新数组，防止外部直接 `push()` 污染内部状态
- `pendingToolCalls` 是 `Set<string>`，每次更新都创建新 Set（immutable update pattern）

**（2）单次运行并发守卫（activeRun）**

```typescript
// agent.ts:497-520
private async runWithLifecycle(
    executor: (signal: AbortSignal) => Promise<void>
): Promise<void> {
    if (this.activeRun) {
        throw new Error("Agent is already processing.");
    }
    const abortController = new AbortController();
    let resolvePromise = () => {};
    const promise = new Promise<void>((resolve) => {
        resolvePromise = resolve;
    });
    this.activeRun = { promise, resolve: resolvePromise, abortController };
    this._state.isStreaming = true;
    this._state.streamingMessage = undefined;
    this._state.errorMessage = undefined;
    try {
        await executor(abortController.signal);
    } catch (error) {
        await this.handleRunFailure(error, abortController.signal.aborted);
    } finally {
        this.finishRun();
    }
}
```

**关键设计：`activeRun` 守卫**
- 如果当前有 `activeRun`（Promise 未完成），新的 `prompt()` / `continue()` 调用会**立即抛错**
- 这是 pi-mono 的并发策略：**同 Agent 实例内不允许并发运行**
- 注意：pi-mono 的设计假设是"单 active session"，所以不需要更细粒度的锁

**（3）事件还原状态（processEvents）**

```typescript
// agent.ts:554-594
private async processEvents(event: AgentEvent): Promise<void> {
    switch (event.type) {
        case "message_start":
            this._state.streamingMessage = event.message;
            break;
        case "message_end":
            this._state.streamingMessage = undefined;
            this._state.messages.push(event.message);
            break;
        case "tool_execution_start": {
            const pending = new Set(this._state.pendingToolCalls);
            pending.add(event.toolCallId);
            this._state.pendingToolCalls = pending;
            break;
        }
        case "tool_execution_end": {
            const pending = new Set(this._state.pendingToolCalls);
            pending.delete(event.toolCallId);
            this._state.pendingToolCalls = pending;
            break;
        }
        // ...
    }
    // 转发给外部监听器
    for (const listener of this.listeners) {
        await listener(event, signal);
    }
}
```

**关键设计：事件驱动的状态同步**
- 纯函数 Loop 发出事件，`Agent` 根据事件类型更新自己的 `_state`
- 这种"事件溯源"模式让状态变更有清晰的审计轨迹
- 外部监听器（如 AgentSession）也能通过同样的事件流同步自己的 shadow state

#### 决策 3：AgentSession 是"会话业务逻辑层"（agent-session.ts）

**文件**：`packages/coding-agent/src/core/agent-session.ts:249-786`

`AgentSession` 包装一个 `Agent` 实例，并叠加会话级的业务状态：

```typescript
// 核心字段（agent-session.ts:249-319）
export class AgentSession {
    readonly agent: Agent;                          // ← 持有的 Agent 实例
    readonly sessionManager: SessionManager;        // ← 独立的消息树存储
    readonly settingsManager: SettingsManager;

    // 事件系统
    private _unsubscribeAgent?: () => void;         // ← Agent 事件退订句柄
    private _eventListeners: AgentSessionEventListener[] = [];
    private _agentEventQueue: Promise<void> = Promise.resolve();

    // 消息队列 Shadow Copy（UI 显示用）
    private _steeringMessages: string[] = [];
    private _followUpMessages: string[] = [];

    // Compaction 状态
    private _compactionAbortController: AbortController | undefined;
    private _autoCompactionAbortController: AbortController | undefined;
    private _overflowRecoveryAttempted = false;

    // Retry 状态
    private _retryAbortController: AbortController | undefined;
    private _retryAttempt = 0;
    private _retryPromise: Promise<void> | undefined;

    // Bash 执行状态
    private _bashAbortController: AbortController | undefined;
    private _pendingBashMessages: BashExecutionMessage[] = [];

    // 扩展系统
    private _extensionRunner!: ExtensionRunner;
    private _turnIndex = 0;

    // 工具注册表（per-session）
    private _toolRegistry: Map<string, AgentTool> = new Map();
    private _toolDefinitions: Map<string, ToolDefinitionEntry> = new Map();
}
```

**关键设计：Shadow Queue + Promise Chain**

1. **Shadow Queue**：`Agent` 有自己的 `steeringQueue` / `followUpQueue`（`PendingMessageQueue` 类），`AgentSession` 维护自己的 `_steeringMessages` / `_followUpMessages` 字符串数组作为 UI 显示的 shadow copy。两者通过事件同步。

2. **Promise Chain 串行化**：
```typescript
// agent-session.ts:462-473
private _handleAgentEvent = (event: AgentEvent): void => {
    this._createRetryPromiseForAgentEnd(event);
    this._agentEventQueue = this._agentEventQueue.then(
        () => this._processAgentEvent(event),
        () => this._processAgentEvent(event),
    );
    this._agentEventQueue.catch(() => {});
};
```

这里用 Promise chain 保证：即使 `Agent.emit()` 同步调用监听器，所有副作用（持久化、扩展 Hook、compaction）也是**串行执行**的。这是处理异步事件的经典模式。

**关键设计：dispose() 资源清理**

```typescript
// agent-session.ts:759-786
dispose(): void {
    this._extensionRunner.invalidate(
        "This extension ctx is stale after session replacement or reload..."
    );
    this._disconnectFromAgent();
    this._eventListeners = [];
    cleanupSessionResources(this.sessionId);
    // audit logger finalization ...
}
```

- `invalidate()` 让 ExtensionRunner 进入"过期"状态，后续任何 API 调用抛错
- `_disconnectFromAgent()` 取消 Agent 事件订阅
- 清理 session 资源、关闭审计日志

#### 决策 4：AgentSessionRuntime 是"运行时生命周期管理器"（agent-session-runtime.ts）

**文件**：`packages/coding-agent/src/core/agent-session-runtime.ts:67-400`

`AgentSessionRuntime` 是 Host 层的单例句柄，负责：**持有当前 session + 协调切换**。

```typescript
// agent-session-runtime.ts:67-77
export class AgentSessionRuntime {
    private rebindSession?: (session: AgentSession) => Promise<void>;
    private beforeSessionInvalidate?: () => void;

    constructor(
        private _session: AgentSession,
        private _services: AgentSessionServices,
        private readonly createRuntime: CreateAgentSessionRuntimeFactory,
        private _diagnostics: AgentSessionRuntimeDiagnostic[] = [],
        private _modelFallbackMessage?: string,
    ) {}
}
```

**核心方法：teardownCurrent() + switchSession()**

```typescript
// agent-session-runtime.ts:149-157
private async teardownCurrent(
    reason: SessionShutdownEvent["reason"],
    targetSessionFile?: string
): Promise<void> {
    await emitSessionShutdownEvent(this.session.extensionRunner, {
        type: "session_shutdown", reason, targetSessionFile,
    });
    this.beforeSessionInvalidate?.();
    this.session.dispose();  // ← 关键：先销毁旧 session
}

// agent-session-runtime.ts:175-198
async switchSession(sessionPath: string, ...): Promise<{ cancelled: boolean }> {
    const previousSessionFile = this.session.sessionFile;
    const sessionManager = SessionManager.open(sessionPath, undefined, options?.cwdOverride);
    await this.teardownCurrent("resume", sessionManager.getSessionFile());
    this.apply(
        await this.createRuntime({              // ← 工厂重建完整运行时
            cwd: sessionManager.getCwd(),
            agentDir: this.services.agentDir,
            sessionManager,
            sessionStartEvent: { type: "session_start", reason: "resume", previousSessionFile },
        }),
    );
    await this.finishSessionReplacement(options?.withSession);
    return { cancelled: false };
}

// agent-session-runtime.ts:159-164
private apply(result: CreateAgentSessionRuntimeResult): void {
    this._session = result.session;      // ← 覆盖引用，无合并逻辑
    this._services = result.services;
    this._diagnostics = result.diagnostics;
    this._modelFallbackMessage = result.modelFallbackMessage;
}
```

**关键设计："先销毁，后重建"的原子替换**

1. **没有 merge 逻辑** — `apply()` 只是简单覆盖私有字段
2. **工厂模式** — `createRuntime` 工厂函数确保每次创建新 session 时，初始化逻辑完全一致
3. **明确的 teardown 阶段** — 旧 session 的扩展、事件订阅、审计日志在替换前被彻底清理

**为什么这个模式能防止状态污染？**

因为旧 session 的 `Agent`、`_toolRegistry`、`_eventListeners` 等对象在 `dispose()` 后被解除引用，等待 GC。新 session 拿到的是**全新构造的对象树**，不存在任何共享的可变状态。

#### 决策 5：SessionManager 是"树形结构化存储"（session-manager.ts）

**文件**：`packages/coding-agent/src/core/session-manager.ts:669-1288`

```typescript
export class SessionManager {
    private sessionId: string = "";
    private sessionFile: string | undefined;
    private sessionDir: string;
    private cwd: string;
    private persist: boolean;
    private flushed: boolean = false;
    private fileEntries: FileEntry[] = [];
    private byId: Map<string, SessionEntry> = new Map();
    private labelsById: Map<string, string> = new Map();
    private labelTimestampsById: Map<string, string> = new Map();
    private leafId: string | null = null;
}
```

**关键设计：**
- 每个 `SessionManager` 实例有独立的 `fileEntries` 和索引 `Map`
- 会话是**树形结构**（通过 `parentId` 链接），支持分支（fork）
- `leafId` 追踪当前活跃分支的末端
- 持久化格式是 JSONL（append-only），与当前项目的 JSON 文件不同

#### 决策 6：ExtensionRunner.invalidate() 防止过期引用泄漏

**文件**：`packages/coding-agent/src/core/extensions/runner.ts:469-482`

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

每个 Extension API 调用前都检查 `assertActive()`。如果 session 已切换，持有旧 `ExtensionRunner` 引用的代码会**立即收到明确错误**，而不是静默操作错误的数据。

### 2.3 pi-mono 状态隔离总结

| 层级 | 状态类型 | 隔离机制 | 生命周期 |
|------|---------|---------|----------|
| `agent-loop.ts` | 无（纯函数） | 天然隔离 | 每次调用新建局部变量 |
| `Agent` | transcript + 运行时 | per-instance `MutableAgentState` | 与 AgentSession 同生命周期 |
| `AgentSession` | 业务逻辑 + 扩展 | per-instance 私有字段 | teardown → dispose → GC |
| `AgentSessionRuntime` | 当前 session 引用 | apply() 原子替换 | 进程级，但只持有一个引用 |
| `SessionManager` | 消息树 | per-instance 私有 Map | 与 AgentSession 同生命周期 |

**关键洞察**：pi-mono 没有"并发多 session"的需求（CLI/TUI 单 active session），所以它的隔离是通过**"物理销毁旧对象 + 创建新对象"**实现的，而不是通过锁或字典。

---

## 三、我们的适配方案：AgentLoopSession

### 3.1 核心设计决策

**决策：在保持 pi-mono 哲学的同时，适配并发多 session 场景。**

pi-mono 的哲学是"per-session 实例隔离"。我们的适配策略是：

> **`AgentLoop` 退化为"工厂 + 路由 + 共享依赖持有者"，真正的 ReACT 循环逻辑和 per-session 状态下沉到 `AgentLoopSession`。**

```
┌─────────────────────────────────────────────────────────────┐
│                        AgentLoop                            │
│                    （工厂 / 路由 / 共享依赖）                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  共享依赖（所有 session 共用）                         │    │
│  │  ├── provider, events, hooks, tools, memory, obs    │    │
│  │  ├── session_manager, file_store                    │    │
│  │  ├── max_react_turns, resilience_config             │    │
│  │  └── _validator (ToolInputValidator)                │    │
│  └─────────────────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  _session_runtimes: dict[str, AgentLoopSession]     │    │
│  │  _session_last_accessed: dict[str, float]           │    │
│  │  _session_runtime_ttl: int                          │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
        ┌─────────┐    ┌─────────┐    ┌─────────┐
        │ AgentLoopSession │    │ AgentLoopSession │    │ AgentLoopSession │
        │  (sess-A)        │    │  (sess-B)        │    │  (sess-C)        │
        │  ├── state (IDLE)│    │  ├── state       │    │  ├── state       │
        │  ├── _failure_tracker   │    │  ├── _failure_tracker   │    │  ├── _failure_tracker   │
        │  ├── _chat_only_mode    │    │  ├── _chat_only_mode    │    │  ├── _chat_only_mode    │
        │  ├── _chat_only_success │    │  ├── _chat_only_success │    │  ├── _chat_only_success │
        │  ├── lock (asyncio.Lock)│    │  ├── lock               │    │  ├── lock               │
        │  └── run_turn()         │    │  └── run_turn()         │    │  └── run_turn()         │
        └─────────┘    └─────────┘    └─────────┘
```

### 3.2 与 pi-mono 的对齐点

| pi-mono 设计 | 我们的对齐方案 | 说明 |
|-------------|--------------|------|
| `Agent` 持有 `MutableAgentState` | `AgentLoopSession` 持有运行时状态 | 状态所属权清晰 |
| `Agent.activeRun` 单次运行守卫 | `AgentLoopSession.lock` (asyncio.Lock) | 同 session 串行化 |
| `AgentSession.dispose()` 清理资源 | `AgentLoop.clear_session_runtime()` | 生命周期管理 |
| `AgentSession` 持有独立 `Agent` | `AgentLoop` 持有 `dict[str, AgentLoopSession]` | 并发场景适配 |
| `processEvents()` 事件还原状态 | `run_turn()` 内部直接 mutate state | 同步事件模式 |
| `PendingMessageQueue` | 暂不需要（未来可扩展） | 当前无 steering/followUp 需求 |
| `ExtensionRunner.invalidate()` | 暂不需要 | 当前扩展系统未实现 per-session 隔离需求 |

### 3.3 类定义

#### AgentLoopSession

```python
class AgentLoopSession:
    """
    单个 Session 的运行时上下文。
    
    职责：
    1. 执行完整的 ReACT turn（原来 AgentLoop.run() 的主体逻辑）
    2. 持有所有 per-session 可变状态
    3. 保证同 session 的并发安全（通过 asyncio.Lock）
    4. 状态机管理（IDLE → BUILDING_CONTEXT → CALLING_LLM → ...）
    
    设计对齐 pi-mono Agent 类：
    - 类似 Agent._state 持有 transcript 相关状态
    - 类似 Agent.activeRun 守卫单次运行
    - 通过事件与外部（AgentLoop / EventBus）通信
    """

    def __init__(
        self,
        session_id: str,
        agent_loop: AgentLoop,  # 反向引用获取共享依赖
        resilience_config: ResilienceConfig,
    ):
        self.session_id = session_id
        self.agent_loop = agent_loop

        # ── 运行时弹性策略状态（从 AgentLoop 迁移）──
        self._failure_tracker = ToolFailureTracker(
            window_turns=resilience_config.tool_failure_window_turns,
            threshold=resilience_config.tool_failure_threshold,
        )
        self._chat_only_mode = False
        self._chat_only_success_turns = 0

        # ── 状态机 ──
        self.state = AgentState.IDLE

        # ── 并发控制 ──
        self.lock = asyncio.Lock()

    # ── 公共接口 ──

    async def run_turn(
        self,
        session: LearningSession,
        user_input: str,
        ask_mode: bool = False,
    ) -> AsyncIterable[ChatChunk]:
        """
        执行一轮完整的 ReACT 循环。
        调用方（AgentLoop.run()）已持有 self.lock，此处无需再获取。
        """
        # ... 原来 AgentLoop.run() 的核心逻辑迁移至此 ...
        pass

    # ── 状态管理 ──

    def _set_state(self, new_state: AgentState) -> None:
        """状态转换，触发可观测事件。"""
        old_state = self.state
        self.state = new_state
        # 通过 agent_loop.events 发布事件
        asyncio.get_event_loop().create_task(
            self.agent_loop.events.publish(
                Event(
                    type="agent.stateChanged",
                    payload={
                        "old_state": old_state.value,
                        "new_state": new_state.value,
                        "session_id": self.session_id,
                    },
                    source="agent_loop",
                    session_id=self.session_id,
                )
            )
        )

    def clear(self) -> None:
        """清理运行时状态（用于 session 被删除时）。"""
        self._failure_tracker.reset()
        self._chat_only_mode = False
        self._chat_only_success_turns = 0
        self.state = AgentState.IDLE
```

#### AgentLoop（重构后）

```python
class AgentLoop:
    """
    Agent 主循环（ReACT 架构）。
    
    重构后职责：
    1. 持有共享依赖（provider, events, hooks, tools 等）
    2. 管理 per-session 运行时实例的创建和回收
    3. 提供 run() 入口，内部路由到对应的 AgentLoopSession
    4. 维护运行时过期机制，防止内存泄漏
    
    设计对齐 pi-mono AgentSessionRuntime：
    - 类似 Runtime 持有当前 session 引用（但我们持有多个）
    - 类似 Runtime 通过工厂/字典管理 session 生命周期
    """

    def __init__(
        self,
        provider: BaseProvider,
        memory_manager: MemoryManager,
        session_manager: SessionManager,
        hook_system: HookSystem,
        event_bus: EventBus,
        tool_registry: ToolRegistry,
        observability: Optional[ObservabilityCollector] = None,
        max_react_turns: int = 10,
        resilience_config: Optional[ResilienceConfig] = None,
        session_runtime_ttl: int = 3600,  # 新增：运行时过期时间（秒）
    ):
        # 共享依赖
        self.provider = provider
        self.memory = memory_manager
        self.sessions = session_manager
        self.hooks = hook_system
        self.events = event_bus
        self.tools = tool_registry
        self.obs = observability

        # 全局配置
        self.max_react_turns = max_react_turns
        self._resilience_config = resilience_config or ResilienceConfig()
        self._validator = ToolInputValidator(tool_registry)

        # per-session 运行时容器
        self._session_runtimes: dict[str, AgentLoopSession] = {}
        self._session_last_accessed: dict[str, float] = {}
        self._session_runtime_ttl = session_runtime_ttl

    # ── 公共接口 ──

    async def run(
        self,
        session: LearningSession,
        user_input: str,
        ask_mode: bool = False,
    ) -> AsyncIterable[ChatChunk]:
        """
        执行一轮 Agent 循环。
        获取或创建 per-session 运行时，委托执行。
        """
        runtime = self._get_or_create_runtime(session.id)
        async with runtime.lock:
            async for chunk in runtime.run_turn(session, user_input, ask_mode):
                yield chunk

    def clear_session_runtime(self, session_id: str) -> None:
        """
        清理指定 session 的运行时状态。
        由 SessionManager 在删除 session 时回调。
        """
        runtime = self._session_runtimes.pop(session_id, None)
        if runtime:
            runtime.clear()
        self._session_last_accessed.pop(session_id, None)
        logger.info(f"[AgentLoop] Session runtime cleared: {session_id}")

    def clear_all_runtimes(self) -> None:
        """清理所有运行时（系统启动/关闭时调用）。"""
        for runtime in self._session_runtimes.values():
            runtime.clear()
        self._session_runtimes.clear()
        self._session_last_accessed.clear()

    # ── 内部方法 ──

    def _get_or_create_runtime(self, session_id: str) -> AgentLoopSession:
        """获取或创建 session 运行时，同时清理过期实例。"""
        self._cleanup_expired_runtimes()

        if session_id not in self._session_runtimes:
            self._session_runtimes[session_id] = AgentLoopSession(
                session_id=session_id,
                agent_loop=self,
                resilience_config=self._resilience_config,
            )
            logger.info(f"[AgentLoop] Session runtime created: {session_id}")

        self._session_last_accessed[session_id] = time.time()
        return self._session_runtimes[session_id]

    def _cleanup_expired_runtimes(self) -> None:
        """清理超过 TTL 未访问的运行时实例。"""
        if self._session_runtime_ttl <= 0:
            return
        now = time.time()
        expired = [
            sid for sid, last in self._session_last_accessed.items()
            if now - last > self._session_runtime_ttl
        ]
        for sid in expired:
            self.clear_session_runtime(sid)
            logger.debug(f"[AgentLoop] Expired session runtime cleaned: {sid}")
```

### 3.4 AgentLoopSession 与 pi-mono Agent 的类比

| pi-mono `Agent` | 我们的 `AgentLoopSession` | 差异说明 |
|----------------|------------------------|---------|
| `_state: MutableAgentState` | `state: AgentState` + `_failure_tracker` + `_chat_only_mode` | 我们的状态更简单（无 message queue） |
| `activeRun: {promise, abortController}` | `lock: asyncio.Lock` | Python async 生态用 Lock 替代 Promise |
| `steeringQueue / followUpQueue` | （暂无） | 当前无消息注入需求，未来可扩展 |
| `listeners: Set<...>` | 通过 `agent_loop.events` 发布事件 | 事件总线解耦 |
| `prompt() / continue()` | `run_turn()` | 功能等价，命名不同 |
| `processEvents()` | 事件直接在 `run_turn()` 中处理 | 同步模式，无需事件还原 |

### 3.5 状态隔离验证

重构后，以下场景的状态行为：

**场景 1：Session A 工具失败，Session B 不受影响**

```python
# Session A 第一次聊天
async for chunk in agent_loop.run(session_a, "查天气"):
    # 天气工具失败
    # AgentLoopSession-A._failure_tracker 记录失败
    # AgentLoopSession-A._chat_only_mode = True
    pass

# Session B 第一次聊天
async for chunk in agent_loop.run(session_b, "查股价"):
    # AgentLoopSession-B 是新创建的
    # _failure_tracker 为空，_chat_only_mode = False
    # 股价工具正常调用
    pass
```

**场景 2：Session A 的 turn_count 不污染 Session B 的滑动窗口**

```python
# AgentLoopSession-A 运行到 turn 3，tool_x 失败
# 记录：(tool_x, turn=3, reason="execution_error")

# AgentLoopSession-B 运行到 turn 1，调用 tool_x
# 检查 is_banned(tool_x, current_turn=1)
# 只扫描 AgentLoopSession-B._failure_tracker 中的记录（为空）
# 结果：未被 ban ✓
```

**场景 3：并发安全**

```python
# 协程 1：Session A 正在聊天
async with agent_loop_session_a.lock:
    # 执行中...

# 协程 2：同时 Session A 收到第二条消息
async with agent_loop_session_a.lock:
    # 等待协程 1 释放锁后执行 ✓

# 协程 3：同时 Session B 收到消息
async with agent_loop_session_b.lock:
    # 与 Session A 的锁无关，可并行执行 ✓
```

---

## 四、代码迁移清单

### 4.1 迁移概览

| 源位置 | 目标位置 | 内容 | 优先级 |
|--------|---------|------|--------|
| `AgentLoop.__init__` | `AgentLoop.__init__` | 移除 per-session 状态初始化，改为 dict 容器 | P0 |
| `AgentLoop.__init__` | `AgentLoopSession.__init__` | `_failure_tracker`、`_chat_only_mode`、`_chat_only_success_turns` | P0 |
| `AgentLoop.run()` | `AgentLoopSession.run_turn()` | 核心 ReACT 循环逻辑（约 400 行） | P0 |
| `AgentLoop.run()` | `AgentLoop.run()` | 路由逻辑：获取 runtime + 获取锁 + 委托 | P0 |
| `AgentLoop._set_state()` | `AgentLoopSession._set_state()` | 状态转换 + 事件发布 | P1 |
| `AgentLoop._build_context_for_turn()` | `AgentLoopSession._build_context_for_turn()` | 上下文组装 | P1 |
| `AgentLoop._stream_chat_with_retry()` | `AgentLoopSession._stream_chat_with_retry()` | LLM 流式调用 + 重试 | P1 |
| `AgentLoop._execute_tool_calls()` | `AgentLoopSession._execute_tool_calls()` | 工具执行 | P1 |
| `AgentLoop._finalize_with_llm()` | `AgentLoopSession._finalize_with_llm()` | 收尾 LLM | P1 |
| `AgentLoop._run_alignment_turn()` | `AgentLoopSession._run_alignment_turn()` | Ask 对齐 | P1 |
| `AgentLoop._save_state_snapshot()` | `AgentLoopSession._save_state_snapshot()` | 状态快照 | P2 |
| `AgentLoop._emit_agent_event()` | `AgentLoopSession._emit_agent_event()` | 事件发射 | P1 |
| `AgentLoop._compress_context()` | `AgentLoopSession._compress_context()` | 上下文压缩 | P2 |

### 4.2 依赖注入调整

当前 `AgentLoop` 的方法通过 `self.provider`、`self.events` 等访问共享依赖。迁移后，`AgentLoopSession` 需要通过反向引用访问：

```python
# 迁移前（AgentLoop 内部）
async for chunk in self.provider.stream_chat(params):
    ...
await self.events.publish(Event(...))

# 迁移后（AgentLoopSession 内部）
async for chunk in self.agent_loop.provider.stream_chat(params):
    ...
await self.agent_loop.events.publish(Event(...))
```

这是一个**机械替换**，风险极低。

### 4.3 新增方法

```python
# AgentLoop 新增
AgentLoop._get_or_create_runtime(session_id) -> AgentLoopSession
AgentLoop._cleanup_expired_runtimes() -> None
AgentLoop.clear_session_runtime(session_id) -> None
AgentLoop.clear_all_runtimes() -> None

# AgentLoopSession 新增
AgentLoopSession.__init__(session_id, agent_loop, resilience_config)
AgentLoopSession.run_turn(session, user_input, ask_mode) -> AsyncIterable[ChatChunk]
AgentLoopSession._set_state(new_state) -> None
AgentLoopSession.clear() -> None
```

---

## 五、实施步骤

### Step 1：创建 AgentLoopSession 类（最小可行拆分）

**范围**：只迁移 per-session 状态，不迁移核心循环逻辑。

1. 新建 `AgentLoopSession` 类
2. 将 `_failure_tracker`、`_chat_only_mode`、`_chat_only_success_turns`、`state`、`lock` 移入
3. `AgentLoop` 中改为 `dict[str, AgentLoopSession]` 管理
4. `AgentLoop.run()` 中：获取 runtime → `async with runtime.lock:` → 执行原有逻辑

**验证**：
- 单元测试：创建两个 session，让 A 进入 chat-only mode，验证 B 未受影响
- 单元测试：并发调用同 session，验证串行执行
- 单元测试：并发调用不同 session，验证并行执行

### Step 2：方法迁移（完整重构）

**范围**：将 `AgentLoop` 的私有方法逐步迁移到 `AgentLoopSession`。

1. 迁移 `_set_state()`、`_emit_agent_event()`
2. 迁移 `_build_context_for_turn()`、`_stream_chat_with_retry()`
3. 迁移 `_execute_tool_calls()`、`_finalize_with_llm()`
4. 迁移 `_run_alignment_turn()`、`_save_state_snapshot()`
5. 最终 `AgentLoop` 只剩公共接口和工厂方法

**验证**：
- 所有现有测试通过
- 新增 AgentLoopSession 的独立单元测试

### Step 3：生命周期集成

**范围**：Session 删除时清理运行时。

1. `SessionManager.delete_session()` 中调用 `agent_loop.clear_session_runtime(session_id)`
2. `web_server.py` 的 `DELETE /sessions` 端点同步调整
3. FastAPI `lifespan` 中增加 `agent_loop.clear_all_runtimes()`

**验证**：
- 创建 session → 聊天 → 删除 session → 验证 `_session_runtimes` 中无残留

---

## 六、风险评估

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| 方法迁移中遗漏 self 引用 | 中 | 运行时 AttributeError | 代码审查 + 静态检查（mypy） |
| 锁粒度导致同 session 并发请求排队 | 高（预期） | 延迟增加 | 这是设计预期行为；未来可考虑消息队列替代 |
| 过期清理误删活跃 runtime | 低 | 运行时状态丢失 | TTL 设置保守（1小时），活跃 session 会刷新访问时间 |
| 内存泄漏（_session_runtimes 无限增长） | 低 | OOM | TTL 机制 + delete 回调双重保险 |
| 事件发布中的 session_id 丢失 | 中 | 观测数据错乱 | 代码审查，确保所有 Event 构造传入 session_id |

---

## 七、验证方案

### 7.1 单元测试

```python
# tests/test_agent_loop_session.py

@pytest.mark.asyncio
async def test_session_state_isolation():
    """验证两个 session 的运行时状态互相隔离。"""
    agent_loop = create_test_agent_loop()
    session_a = create_test_session("sess-a")
    session_b = create_test_session("sess-b")

    # 让 session A 进入 chat-only mode
    runtime_a = agent_loop._get_or_create_runtime("sess-a")
    runtime_a._chat_only_mode = True

    # session B 的运行时不受影响
    runtime_b = agent_loop._get_or_create_runtime("sess-b")
    assert runtime_b._chat_only_mode is False

@pytest.mark.asyncio
async def test_concurrent_same_session_serializes():
    """验证同 session 的并发调用被串行化。"""
    agent_loop = create_test_agent_loop()
    session = create_test_session("sess-a")

    results = []

    async def run1():
        async for chunk in agent_loop.run(session, "msg1"):
            results.append(("run1", chunk.content))

    async def run2():
        async for chunk in agent_loop.run(session, "msg2"):
            results.append(("run2", chunk.content))

    await asyncio.gather(run1(), run2())

    # 结果应该完全串行：run1 的所有 chunk 在前，run2 的所有 chunk 在后
    # 或反之，但绝不交错
    run1_indices = [i for i, (name, _) in enumerate(results) if name == "run1"]
    run2_indices = [i for i, (name, _) in enumerate(results) if name == "run2"]
    assert max(run1_indices) < min(run2_indices) or max(run2_indices) < min(run1_indices)

@pytest.mark.asyncio
async def test_concurrent_different_session_parallel():
    """验证不同 session 的调用可并行。"""
    # 使用 mock provider 记录并发数
    ...

@pytest.mark.asyncio
async def test_runtime_cleanup_on_delete():
    """验证 session 删除时运行时被清理。"""
    agent_loop = create_test_agent_loop()
    agent_loop._get_or_create_runtime("sess-a")
    assert "sess-a" in agent_loop._session_runtimes

    agent_loop.clear_session_runtime("sess-a")
    assert "sess-a" not in agent_loop._session_runtimes
    assert "sess-a" not in agent_loop._session_last_accessed
```

### 7.2 集成测试

```python
# 端到端测试：模拟 Web 端"新对话"流程
@pytest.mark.asyncio
async def test_web_new_chat_flow():
    system = create_test_system()
    
    # 1. 创建 session A
    session_a = system.session_manager.create_session(title="Chat A")
    
    # 2. 与 A 聊天
    chunks = []
    async for chunk in system.agent_loop.run(session_a, "Hello A"):
        chunks.append(chunk)
    
    # 3. 创建 session B
    session_b = system.session_manager.create_session(title="Chat B")
    
    # 4. 与 B 聊天（同时 A 的运行时在内存中）
    chunks_b = []
    async for chunk in system.agent_loop.run(session_b, "Hello B"):
        chunks_b.append(chunk)
    
    # 5. 删除 B
    system.session_manager.delete_session(session_b.id)
    system.agent_loop.clear_session_runtime(session_b.id)
    
    # 6. 验证 A 仍可聊天
    chunks_a2 = []
    async for chunk in system.agent_loop.run(session_a, "Hello A again"):
        chunks_a2.append(chunk)
    
    assert len(chunks_a2) > 0
```

---

## 八、关键决策记录

1. **为什么不把状态放入 `LearningSession` 模型？**
   - 运行时弹性状态（failure tracker、chat-only mode）不属于业务领域数据
   - 不应被持久化到 JSON 文件中（服务重启后应重置）
   - 保持 `LearningSession` 作为纯数据模型的简洁性

2. **为什么每个 session 一个 `ToolFailureTracker` 实例，而不是全局 tracker 加 session_id key？**
   - 更简单：无需修改 `ToolFailureTracker` 的内部数据结构
   - 更安全：完全隔离，不存在 key 碰撞或忘记加 key 的风险
   - 更符合 pi-mono 哲学：状态物理隔离，而非逻辑隔离

3. **为什么用 `asyncio.Lock` 而不是 pi-mono 的 `activeRun` Promise 守卫？**
   - Python 的 async 生态中，`asyncio.Lock` 是标准的并发控制原语
   - `activeRun` 模式（抛错拒绝并发调用）在 Web 场景下不友好——用户快速双击应该排队而非报错
   - 锁模式允许我们未来扩展为"消息队列"（steeringQueue 的等价物）

4. **TTL 过期机制是否必要？**
   - 对于长时间运行的 Web 服务，是必需的。防止用户创建 session 后不再使用，但运行时一直驻留内存
   - TTL 默认 1 小时，可配置。活跃 session 每次聊天会刷新访问时间
   - 被清理的运行时下次聊天时会自动重建，状态重置（这是预期行为）

---

## 附录 A：pi-mono 关键源码索引

| 文件 | 行号 | 内容 | 对应我们的设计 |
|------|------|------|-------------|
| `packages/agent/src/agent.ts` | 58-92 | `MutableAgentState` + `createMutableAgentState` | `AgentLoopSession` 状态设计参考 |
| `packages/agent/src/agent.ts` | 194-212 | `Agent` 构造函数 | `AgentLoopSession.__init__` |
| `packages/agent/src/agent.ts` | 497-520 | `runWithLifecycle()` + `activeRun` | `AgentLoopSession.lock` |
| `packages/agent/src/agent.ts` | 554-594 | `processEvents()` | `run_turn()` 内部事件处理 |
| `packages/agent/src/agent-loop.ts` | 31-93 | 纯函数入口 | 未来可进一步抽离纯函数循环 |
| `packages/coding-agent/src/core/agent-session.ts` | 249-320 | `AgentSession` 私有字段 | `AgentLoopSession` 字段设计参考 |
| `packages/coding-agent/src/core/agent-session.ts` | 462-473 | `_handleAgentEvent()` Promise chain | 事件处理串行化参考 |
| `packages/coding-agent/src/core/agent-session.ts` | 759-786 | `dispose()` | `AgentLoopSession.clear()` |
| `packages/coding-agent/src/core/agent-session-runtime.ts` | 149-157 | `teardownCurrent()` | `AgentLoop.clear_session_runtime()` |
| `packages/coding-agent/src/core/agent-session-runtime.ts` | 159-164 | `apply()` | `_session_runtimes[sid] = new AgentLoopSession()` |
| `packages/coding-agent/src/core/agent-session-runtime.ts` | 175-198 | `switchSession()` | session 生命周期管理参考 |
| `packages/coding-agent/src/core/extensions/runner.ts` | 469-482 | `invalidate()` + `assertActive()` | 扩展系统过期引用防护参考 |
