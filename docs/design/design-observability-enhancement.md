# 技术设计文档：观测页面增强方案

> **目标**：解决 Span 泄漏（问题 12）、全局 trace 栈错乱（多 Session 并发），并增强可观测性以支持 AgentLoopSession 架构的调试与监控
> **范围**：`learning_agent/agent/observability.py`、`learning_agent/web_server.py`、`web/observability.html`、`web/static/observability.js`
> **文档日期**：2026-05-12

---

## 一、当前观测能力分析

### 1.1 架构概览

```
┌─────────────────────────────────────────────┐
│  ObservabilityCollector                     │
│  ├── data_dir: ".observability"             │
│  ├── metrics: MetricsStore                  │
│  ├── _traces: dict[str, Trace]              │
│  ├── _snapshots: list[Snapshot]             │
│  ├── _logs: list[dict]                      │
│  ├── _active_trace: Optional[Trace]         │  ← 【全局单例】
│  └── _active_span_stack: list[TraceSpan]    │  ← 【全局单例】⚠️
└─────────────────────────────────────────────┘
         │
         ▼ 订阅 EventBus 所有事件
┌─────────────────────────────────────────────┐
│  持久化文件                                 │
│  ├── events.jsonl    —— 所有事件日志        │
│  ├── errors.jsonl    —— 错误/警告事件       │
│  ├── audit.jsonl     —— 审计日志            │
│  ├── trace_{id}.json —— 单个 trace 详情     │
│  ├── snap_{id}.json  —— 状态快照            │
│  └── flow_{sess}.json —— session 流程数据   │
└─────────────────────────────────────────────┘
```

### 1.2 当前 API 端点

| 端点 | 功能 | 数据来源 |
|------|------|---------|
| `GET /observability/errors` | 聚合错误（events + trace span errors + audit） | events.jsonl + trace_*.json + audit.jsonl |
| `GET /observability/flows/{session_id}` | Session 流程数据 | flow_{session_id}.json |
| `GET /observability/traces` | Trace 列表 | trace_*.json |
| `GET /observability/traces/{trace_id}` | Trace 详情 | trace_{trace_id}.json |
| `GET /observability/events` | 事件日志 | events.jsonl |
| `GET /observability/metrics` | 指标摘要 | MetricsStore (内存) |
| `GET /observability/logs` | Trace errors + event logs | trace_*.json + events.jsonl |

### 1.3 当前问题诊断

**问题 12（Span 泄漏）—— 根因分析：**

```python
# observability.py:92-98
def start_trace(self, session_id=None, objective_id=None) -> Trace:
    trace = Trace(session_id=session_id, objective_id=objective_id)
    self._traces[trace.trace_id] = trace
    self._active_trace = trace
    self._active_span_stack.clear()  # ← 【问题】清除全局 span 栈
    return trace
```

**问题场景：**

```
协程 A: run(Session A)
    ├── start_trace(sess-a) → _active_trace = trace-a
    ├── start_span("agent.loop") → _active_span_stack = [span-a1]
    ├── 执行中...
    │
协程 B: run(Session B)  ← 并发进入！
    ├── start_trace(sess-b) → _active_trace = trace-b
    ├── self._active_span_stack.clear()  ← 【清除了 A 的 span！】
    ├── start_span("agent.loop") → _active_span_stack = [span-b1]
    │
协程 A: 继续执行
    ├── current_span() → 返回 span-b1（错误！应该是 span-a1）
    ├── end_span(span-a1) → span-a1 不在栈中（泄漏！）
```

**影响：**
1. Session A 的 span 树被 Session B 的 `start_trace()` 破坏
2. `end_span()` 无法找到正确的 span，导致 span 永不结束、内存泄漏
3. Trace 文件中的 span 树结构完全错乱

**另一个泄漏路径（HookAbortError）：**

```python
# agent_loop.py 中
# _execute_tool_calls() 创建了 tool_span
# 如果 Hook 抛出 HookAbortError，tool_span 从未被 end_span()
# 因为异常直接传播到 run() 外层，那里只关闭 root_span
```

---

## 二、设计目标

| 目标 | 说明 |
|------|------|
| **Trace 隔离** | 每个 session 的 trace 和 span 栈独立，并发不会互相破坏 |
| **Span 生命周期安全** | 所有 span 创建都有对应的结束，异常路径通过 try/finally 保证 |
| **运行时状态可观测** | 可以实时查看每个 session 的 AgentLoopSession 状态 |
| **前端面板增强** | Observability 页面支持按 session 过滤、实时刷新、运行时监控 |

---

## 三、核心方案：Trace 上下文按 Session 隔离

### 3.1 设计原理

pi-mono 的 `Agent` 类中，trace/span 的生命周期与 `activeRun` 绑定——同 Agent 实例内不会并发运行，所以全局 `_active_trace` 和 `_active_span_stack` 是安全的。

但我们的场景是**多 Session 并发**，因此必须将 trace 上下文按 `session_id`（或 `trace_id`）隔离。

### 3.2 改造方案

**当前问题字段**：
```python
self._active_trace: Optional[Trace] = None
self._active_span_stack: list[TraceSpan] = []
```

**改造后**：
```python
# 按 trace_id 隔离的 span 栈
self._span_stacks: dict[str, list[TraceSpan]] = {}

# _active_trace 保留（用于兼容现有代码），但增加 session → trace 的映射
self._session_trace_map: dict[str, str] = {}  # session_id -> trace_id
```

**关键设计决策**：

1. **不再清除全局 span 栈** — 改为按 `trace_id` 隔离的独立栈
2. **`start_trace()` 时建立 session → trace 映射** — 后续该 session 的所有 span 操作都在自己的栈上
3. **`end_trace()` 时清理对应栈** — 防止内存泄漏
4. **兼容现有单 trace 场景** — `_active_trace` 仍指向最后开始的 trace，保证无 session 上下文调用时的行为

### 3.3 类定义（改造后）

```python
class ObservabilityCollector:
    """
    可观测性收集器（多 Session 安全版本）。
    
    核心变更：
    1. _active_span_stack → _span_stacks: dict[str, list[TraceSpan]]
    2. 增加 _session_trace_map 用于 session → trace 路由
    3. start_trace/end_trace 增加 session_id 参数
    4. 所有 span 操作默认使用当前 trace 的栈，支持显式指定 trace_id
    """

    def __init__(self, data_dir: str = ".observability"):
        self.data_dir = data_dir
        self.metrics = MetricsStore()
        self._traces: dict[str, Trace] = {}
        self._snapshots: list[Snapshot] = []
        self._logs: list[dict[str, Any]] = []
        
        # 【改造】按 trace_id 隔离的 span 栈
        self._span_stacks: dict[str, list[TraceSpan]] = {}
        
        # 【改造】session → trace 映射
        self._session_trace_map: dict[str, str] = {}
        
        # 【保留兼容】最后激活的 trace
        self._active_trace: Optional[Trace] = None
        
        os.makedirs(data_dir, exist_ok=True)

    # ─── Trace 管理 ───

    def start_trace(
        self,
        session_id: Optional[str] = None,
        objective_id: Optional[str] = None,
    ) -> Trace:
        trace = Trace(session_id=session_id, objective_id=objective_id)
        self._traces[trace.trace_id] = trace
        self._active_trace = trace
        
        # 【改造】为该 trace 创建独立的 span 栈
        self._span_stacks[trace.trace_id] = []
        
        # 【改造】建立 session → trace 映射
        if session_id:
            # 如果该 session 已有活跃 trace，先结束它
            old_trace_id = self._session_trace_map.get(session_id)
            if old_trace_id and old_trace_id in self._traces:
                old_trace = self._traces[old_trace_id]
                if not old_trace.end_time:
                    old_trace.end()
                    self._persist_trace(old_trace)
                    # 清理旧栈
                    self._span_stacks.pop(old_trace_id, None)
            
            self._session_trace_map[session_id] = trace.trace_id
        
        logger.info(f"[Observability] Trace started: {trace.trace_id} (session={session_id})")
        return trace

    def end_trace(self, trace_id: Optional[str] = None) -> Optional[Trace]:
        target_trace_id = trace_id or (self._active_trace.trace_id if self._active_trace else None)
        if not target_trace_id:
            return None
        
        trace = self._traces.get(target_trace_id)
        if not trace:
            return None
        
        trace.end()
        
        # 【改造】清理该 trace 的 span 栈
        self._span_stacks.pop(target_trace_id, None)
        
        # 【改造】清理 session → trace 映射（反向查找）
        sessions_to_remove = [
            sid for sid, tid in self._session_trace_map.items()
            if tid == target_trace_id
        ]
        for sid in sessions_to_remove:
            del self._session_trace_map[sid]
        
        if self._active_trace and self._active_trace.trace_id == target_trace_id:
            self._active_trace = None
        
        self._persist_trace(trace)
        return trace

    # ─── Span 管理 ───

    def start_span(
        self,
        name: str,
        parent: Optional[TraceSpan] = None,
        trace_id: Optional[str] = None,
    ) -> TraceSpan:
        # 确定使用哪个 trace
        target_trace_id = trace_id or (self._active_trace.trace_id if self._active_trace else None)
        if not target_trace_id:
            # 兜底：创建一个新的 trace
            trace = self.start_trace()
            target_trace_id = trace.trace_id
        
        trace = self._traces[target_trace_id]
        span = trace.start_span(name, parent)
        
        # 【改造】推入该 trace 的独立栈
        stack = self._span_stacks.setdefault(target_trace_id, [])
        stack.append(span)
        
        return span

    def end_span(
        self,
        span: Optional[TraceSpan] = None,
        trace_id: Optional[str] = None,
    ) -> None:
        # 确定使用哪个 trace
        target_trace_id = trace_id
        target_span = span
        
        if not target_trace_id and not target_span:
            # 默认使用 active_trace
            target_trace_id = self._active_trace.trace_id if self._active_trace else None
        
        if not target_trace_id and target_span:
            # 从 span 反查 trace（需要 Trace 模型支持）
            # 简化：遍历所有 trace 查找
            for tid, trace in self._traces.items():
                if target_span in trace.spans:
                    target_trace_id = tid
                    break
        
        if not target_trace_id:
            logger.warning("[Observability] end_span called without trace context")
            return
        
        stack = self._span_stacks.get(target_trace_id, [])
        
        if target_span:
            target_span.end()
            if target_span in stack:
                stack.remove(target_span)
        elif stack:
            # 默认结束栈顶 span
            top = stack.pop()
            top.end()

    def current_span(self, trace_id: Optional[str] = None) -> Optional[TraceSpan]:
        target_trace_id = trace_id or (self._active_trace.trace_id if self._active_trace else None)
        if not target_trace_id:
            return None
        stack = self._span_stacks.get(target_trace_id, [])
        return stack[-1] if stack else None

    # ─── 按 Session 查询 ───

    def get_trace_by_session(self, session_id: str) -> Optional[Trace]:
        """获取指定 session 的当前活跃 trace。"""
        trace_id = self._session_trace_map.get(session_id)
        if trace_id:
            return self._traces.get(trace_id)
        return None

    def get_spans_by_session(self, session_id: str) -> list[TraceSpan]:
        """获取指定 session 的当前 span 栈。"""
        trace_id = self._session_trace_map.get(session_id)
        if trace_id:
            return list(self._span_stacks.get(trace_id, []))
        return []

    def clear_session_traces(self, session_id: str) -> None:
        """清理指定 session 的所有 trace 和 span 数据。"""
        trace_id = self._session_trace_map.pop(session_id, None)
        if trace_id:
            self._span_stacks.pop(trace_id, None)
            trace = self._traces.pop(trace_id, None)
            if trace and self._active_trace and self._active_trace.trace_id == trace_id:
                self._active_trace = None
```

### 3.4 AgentLoop 中的调用方式变更

**当前代码**：
```python
# agent_loop.py:231-233
trace = self.obs.start_trace(session_id=session.id, objective_id=session.objective_id)
root_span = self.obs.start_span("agent.loop")

# agent_loop.py:412
top_span = self.obs.current_span()

# agent_loop.py:620-621
self.obs.end_span(root_span)
self.obs.end_trace()
```

**改造后**（AgentLoopSession 中）：
```python
# AgentLoopSession.run_turn() 中
trace = self.agent_loop.obs.start_trace(
    session_id=self.session_id,
    objective_id=session.objective_id,
)
root_span = self.agent_loop.obs.start_span("agent.loop")

# 所有 start_span/end_span/current_span 调用自动路由到正确的 trace
# 因为 start_trace 建立了 session_id → trace_id 映射
```

**关键保证**：
- Session A 的 `start_span()` → 推入 `_span_stacks[trace-a-id]`
- Session B 的 `start_trace()` → 创建新 trace-b，推入 `_span_stacks[trace-b-id]`
- Session A 的 `current_span()` → 从 `_session_trace_map[sess-a]` 找到 trace-a-id → 返回 `_span_stacks[trace-a-id][-1]`
- **两个 session 的 span 栈完全独立**

### 3.5 Span 生命周期安全（HookAbortError 路径）

**当前问题**：
```python
# agent_loop.py（当前）
# _execute_tool_calls() 中
tool_span = self.obs.start_span("tool.exec", parent=root_span)
# ... 如果这里抛出 HookAbortError ...
# tool_span 从未被 end_span()
```

**修复**（在 AgentLoopSession 中）：
```python
# AgentLoopSession._execute_tool_calls() 中
tool_span = self.agent_loop.obs.start_span("tool.exec", parent=root_span)
try:
    # ... hook 执行和工具调用 ...
    pass
finally:
    self.agent_loop.obs.end_span(tool_span)
```

**所有 span 创建点都必须用 try/finally 保护**：

| Span 名称 | 创建位置 | 保护方式 |
|-----------|---------|---------|
| `agent.loop` | `run_turn()` 开头 | try/finally 在 `run_turn()` 末尾 |
| `context.build` | `_build_context_for_turn()` | try/finally 包裹 |
| `llm.stream` | `_stream_chat_with_retry()` 前 | try/finally 在 stream 结束后 |
| `ask.alignment` | `_run_alignment_turn()` | try/finally 包裹 |
| `tool.exec` | `_execute_tool_calls()` 中 | try/finally 每个 tool call |

---

## 四、运行时状态观测增强

### 4.1 新增后端 API

**`GET /observability/runtimes`**（已在 Web 适配文档中定义，此处补充观测细节）：

```python
@app.get("/observability/runtimes")
async def get_runtimes() -> dict[str, Any]:
    system = _get_system()
    agent_loop = system.agent_loop
    obs = system.observability
    
    runtimes = []
    for sid, runtime in agent_loop._session_runtimes.items():
        # 获取该 session 的 trace 和 span 信息
        trace = obs.get_trace_by_session(sid)
        spans = obs.get_spans_by_session(sid) if trace else []
        
        runtimes.append({
            "session_id": sid,
            "state": runtime.state.value,
            "chat_only_mode": runtime._chat_only_mode,
            "chat_only_success_turns": runtime._chat_only_success_turns,
            "failure_tracker": {
                "tracked_tools": list(runtime._failure_tracker._counts.keys()),
                "banned_tools": [
                    tool_id for tool_id in runtime._failure_tracker._counts.keys()
                    if runtime._failure_tracker.is_banned(tool_id, 0)
                ],
            },
            "lock_acquired": runtime.lock.locked(),  # 是否正在执行
            "last_accessed": agent_loop._session_last_accessed.get(sid),
            "trace": {
                "trace_id": trace.trace_id if trace else None,
                "span_count": len(trace.spans) if trace else 0,
                "active_spans": [s.name for s in spans],
                "duration_ms": trace.duration_ms if trace else None,
            } if trace else None,
        })
    
    return {
        "active_runtime_count": len(agent_loop._session_runtimes),
        "total_session_count": len(system.session_manager._sessions),
        "runtimes": runtimes,
    }
```

### 4.2 新增指标

在 `MetricsStore` 中新增 per-session 指标：

```python
# 在 ObservabilityCollector._update_metrics_from_event() 中新增
elif et == "agent.stateChanged":
    session_id = event.session_id or "unknown"
    self.metrics.gauge_set(
        "agent.state",
        1,
        labels={"session": session_id, "state": event.payload.get("new_state", "unknown")},
    )

# 在 AgentLoop.clear_session_runtime() 中触发事件
# 或通过直接调用的方式新增指标：
def record_runtime_created(self, session_id: str) -> None:
    self.metrics.counter_inc("agent.runtime.created", 1, {"session": session_id})

def record_runtime_cleared(self, session_id: str) -> None:
    self.metrics.counter_inc("agent.runtime.cleared", 1, {"session": session_id})
```

---

## 五、前端 Observability 面板增强

### 5.1 当前面板功能

```
Observability Panel
├── Traces 标签
│   └── 列表：trace_id, session_id, timestamp, duration, span_count
├── Events 标签
│   └── 列表：timestamp, type, source, session_id
├── Metrics 标签
│   └── Counter / Gauge / Histogram 摘要
├── Errors 标签
│   └── 聚合错误列表
└── Logs 标签
    └── trace_errors + event_logs
```

### 5.2 增强后面板功能

```
Observability Panel
├── Runtimes 标签（新增）
│   ├── 总览卡片：活跃运行时数 / 总会话数
│   ├── 运行时列表（实时刷新）
│   │   ├── Session ID
│   │   ├── State（IDLE / STREAMING / EXECUTING_TOOL 等）
│   │   ├── Chat-Only Mode（⚠️ true 时高亮）
│   │   ├── Lock Status（🔒 表示正在执行）
│   │   ├── Active Spans
│   │   └── Banned Tools
│   └── 操作按钮：Reset Runtime
├── Traces 标签
│   └── 【增强】按 Session 过滤
│   └── 【增强】显示 trace 中的 span 树（可展开）
├── Events 标签
│   └── 【增强】按 Session 过滤
│   └── 【增强】实时 SSE 推送（可选）
├── Metrics 标签
│   └── 【增强】按 Session 分组的图表
├── Errors 标签
│   └── 【增强】按 Session 聚合
└── Logs 标签
    └── 【增强】与 Events 合并，支持过滤
```

### 5.3 新增 "Runtimes" 标签页实现

**HTML 结构**：
```html
<!-- observability.html 新增 -->
<div id="tab-runtimes" class="tab-content">
    <div class="runtime-summary">
        <div class="card">
            <div class="card-title">Active Runtimes</div>
            <div class="card-value" id="runtime-count">-</div>
        </div>
        <div class="card">
            <div class="card-title">Total Sessions</div>
            <div class="card-value" id="session-count">-</div>
        </div>
    </div>
    <table class="data-table" id="runtimes-table">
        <thead>
            <tr>
                <th>Session ID</th>
                <th>State</th>
                <th>Chat-Only</th>
                <th>Lock</th>
                <th>Active Spans</th>
                <th>Banned Tools</th>
                <th>Actions</th>
            </tr>
        </thead>
        <tbody></tbody>
    </table>
</div>
```

**JavaScript 实现**：
```javascript
// observability.js 新增

let runtimeRefreshInterval = null;

async function loadRuntimes() {
    try {
        const data = await api('GET', '/observability/runtimes');
        document.getElementById('runtime-count').textContent = data.active_runtime_count;
        document.getElementById('session-count').textContent = data.total_session_count;
        
        const tbody = document.querySelector('#runtimes-table tbody');
        tbody.innerHTML = data.runtimes.map(r => `
            <tr>
                <td><code>${r.session_id}</code></td>
                <td><span class="badge badge-${r.state}">${r.state}</span></td>
                <td>${r.chat_only_mode ? '<span class="badge badge-warn">⚠️ true</span>' : 'false'}</td>
                <td>${r.lock_acquired ? '🔒 locked' : 'unlocked'}</td>
                <td>${r.trace ? r.trace.active_spans.join(', ') || 'none' : 'N/A'}</td>
                <td>${r.failure_tracker.banned_tools.join(', ') || 'none'}</td>
                <td>
                    <button onclick="resetRuntime('${r.session_id}')">Reset</button>
                </td>
            </tr>
        `).join('');
    } catch (err) {
        console.error('Failed to load runtimes:', err);
    }
}

async function resetRuntime(sessionId) {
    if (!confirm(`Reset runtime for session ${sessionId}?`)) return;
    try {
        await api('POST', `/sessions/${sessionId}/reset-runtime`);
        showToast(`Runtime reset for ${sessionId}`);
        loadRuntimes();
    } catch (err) {
        showToast(`Reset failed: ${err.message}`, 'error');
    }
}

function startRuntimeRefresh() {
    if (runtimeRefreshInterval) clearInterval(runtimeRefreshInterval);
    runtimeRefreshInterval = setInterval(loadRuntimes, 5000);  // 每 5 秒刷新
}

function stopRuntimeRefresh() {
    if (runtimeRefreshInterval) {
        clearInterval(runtimeRefreshInterval);
        runtimeRefreshInterval = null;
    }
}

// 在 tab 切换时控制刷新
document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        if (btn.dataset.tab === 'runtimes') {
            startRuntimeRefresh();
        } else {
            stopRuntimeRefresh();
        }
    });
});
```

### 5.4 Traces 标签增强：按 Session 过滤 + Span 树

```javascript
// observability.js 增强

async function loadTraces(sessionFilter = '') {
    const data = await api('GET', '/observability/traces');
    const filtered = sessionFilter 
        ? data.filter(t => t.session_id === sessionFilter)
        : data;
    
    // 渲染列表...
}

async function showTraceDetail(traceId) {
    const data = await api('GET', `/observability/traces/${traceId}`);
    
    // 构建 span 树
    const spanTree = buildSpanTree(data.spans);
    document.getElementById('trace-detail').innerHTML = `
        <h3>Trace: ${traceId}</h3>
        <p>Session: ${data.session_id || 'N/A'}</p>
        <p>Duration: ${data.duration_ms}ms</p>
        <div class="span-tree">
            ${renderSpanTree(spanTree)}
        </div>
    `;
}

function buildSpanTree(spans) {
    const byId = new Map(spans.map(s => [s.span_id, { ...s, children: [] }]));
    const roots = [];
    for (const span of byId.values()) {
        if (span.parent_id && byId.has(span.parent_id)) {
            byId.get(span.parent_id).children.push(span);
        } else {
            roots.push(span);
        }
    }
    return roots;
}

function renderSpanTree(nodes, depth = 0) {
    return nodes.map(n => `
        <div class="span-node" style="padding-left: ${depth * 20}px">
            <span class="span-name">${n.name}</span>
            <span class="span-duration">${n.duration_ms}ms</span>
            ${n.error ? `<span class="span-error">⚠️ ${n.error}</span>` : ''}
            ${n.children.length > 0 ? renderSpanTree(n.children, depth + 1) : ''}
        </div>
    `).join('');
}
```

### 5.5 Events 标签增强：实时 SSE 推送（可选）

**新增后端 SSE 端点**：
```python
@app.get("/observability/events/stream")
async def events_stream() -> StreamingResponse:
    """SSE 实时推送新事件。"""
    async def generator():
        # 简化为轮询 events.jsonl 尾部
        last_size = 0
        while True:
            events_path = Path(system.observability.data_dir) / "events.jsonl"
            if events_path.exists():
                current_size = events_path.stat().st_size
                if current_size > last_size:
                    with open(events_path, "r") as f:
                        f.seek(last_size)
                        for line in f:
                            line = line.strip()
                            if line:
                                yield f"data: {line}\n\n"
                    last_size = current_size
            await asyncio.sleep(1)
    
    return StreamingResponse(generator(), media_type="text/event-stream")
```

**前端 SSE 接收**：
```javascript
let eventSource = null;

function startEventStream() {
    if (eventSource) return;
    eventSource = new EventSource(`${API_BASE}/observability/events/stream`);
    eventSource.onmessage = (e) => {
        const event = JSON.parse(e.data);
        prependEventToTable(event);
    };
}

function stopEventStream() {
    if (eventSource) {
        eventSource.close();
        eventSource = null;
    }
}
```

---

## 六、实施清单

### 6.1 后端修改

| 文件 | 修改内容 | 优先级 |
|------|---------|--------|
| `learning_agent/agent/observability.py` | 重构 `_active_span_stack` → `_span_stacks: dict[str, list]` | P0 |
| `learning_agent/agent/observability.py` | 新增 `_session_trace_map` | P0 |
| `learning_agent/agent/observability.py` | 改造 `start_trace/end_trace/start_span/end_span/current_span` | P0 |
| `learning_agent/agent/observability.py` | 新增 `get_trace_by_session/get_spans_by_session/clear_session_traces` | P1 |
| `learning_agent/agent/observability.py` | 新增 per-session 指标记录 | P2 |
| `learning_agent/agent/agent_loop.py` | 所有 span 创建点增加 try/finally 保护 | P0 |
| `learning_agent/web_server.py` | 新增 `GET /observability/runtimes` | P1 |
| `learning_agent/web_server.py` | 可选：新增 `GET /observability/events/stream` | P3 |

### 6.2 前端修改

| 文件 | 修改内容 | 优先级 |
|------|---------|--------|
| `web/observability.html` | 新增 "Runtimes" 标签页 | P1 |
| `web/observability.html` | Traces 标签增加 Session 过滤器和 Span 树展示 | P2 |
| `web/static/observability.js` | 新增 `loadRuntimes/resetRuntime/startRuntimeRefresh` | P1 |
| `web/static/observability.js` | 新增 `buildSpanTree/renderSpanTree/showTraceDetail` | P2 |
| `web/static/observability.js` | 可选：Events 标签 SSE 实时推送 | P3 |
| `web/static/style.css` | 新增 runtime 卡片、span 树、badge 样式 | P2 |

---

## 七、关键决策记录

1. **为什么用 `dict[str, list[TraceSpan]]` 而不是 `dict[str, Trace]` 来隔离？**
   - `Trace` 对象本身已经按 `trace_id` 隔离存储在 `_traces` 中
   - 问题是 span 的**栈结构**（`_active_span_stack`）是全局的
   - 所以只需隔离栈，无需隔离 Trace 对象本身

2. **为什么保留 `_active_trace` 而不是完全按 session 路由？**
   - 向后兼容：现有代码可能直接调用 `obs.start_span()` 而不传 session_id
   - `_active_trace` 作为兜底，保证无 session 上下文时的行为
   - 长期来看，所有调用都应该传入 session_id

3. **为什么 `start_trace()` 时要结束该 session 的旧 trace？**
   - 防止一个 session 同时存在多个活跃 trace（这在并发 run 时可能发生）
   - 如果 Session A 的 run1 还没结束，run2 又启动了，应该复用或替换 trace
   - 当前策略：替换旧 trace（结束并持久化），开始新 trace
   - 替代方案：复用现有 trace（不创建新 trace），但可能导致 span 树过于复杂

4. **为什么 Events SSE 推送是可选的？**
   - 主要价值是"实时感"，对于调试有帮助但不是核心需求
   - 轮询 events.jsonl 的方式在文件锁和高并发下可能有性能问题
   - 更优雅的方式是通过 EventBus 直接推送，但需要 WebSocket 支持

5. ** ObservabilityCollector 是否也要做成 per-session 实例？**
   - 不需要。观测数据天然需要聚合（全局 metrics、事件总线）
   - 隔离应该在 Collector 内部通过字典实现，而不是创建多个 Collector 实例
   - 这与 pi-mono 的 `AgentSession` 持有独立 `Agent` 的模式不同——观测是横切关注点

---

## 八、与方案 A 的协同关系

| 方案 A 组件 | 观测增强的协同点 |
|------------|----------------|
| `AgentLoop._session_runtimes` | `GET /observability/runtimes` 直接暴露 |
| `AgentLoopSession.lock` | 观测面板显示 lock 状态，帮助诊断卡住的问题 |
| `AgentLoopSession._chat_only_mode` | 观测面板高亮显示，帮助发现意外降级 |
| `AgentLoopSession._failure_tracker` | 观测面板显示 banned tools，帮助诊断工具问题 |
| `AgentLoopSession` 的独立 trace | `ObservabilityCollector._span_stacks` 保证并发安全 |

**三个文档的依赖关系**：

```
文档1（方案A）
    ├── 定义 AgentLoopSession 类
    ├── 定义状态隔离机制
    └── 定义 run_turn() 中的 span 生命周期
        │
        ▼
文档3（观测增强）
    ├── 改造 ObservabilityCollector 支持并发 trace
    ├── 新增 per-session 观测 API
    └── 保证 span 生命周期安全
        │
        ▼
文档2（Web 适配）
    ├── 新增 /observability/runtimes 端点
    ├── 前端展示运行时状态
    └── 生命周期管理（删除时清理）
```

---

## 附录：问题 12 修复验证

**修复前的问题复现**：
```python
# 模拟并发场景
async def session_a():
    trace_a = obs.start_trace(session_id="sess-a")
    span_a1 = obs.start_span("agent.loop")
    await asyncio.sleep(0.1)  # 模拟执行中
    # 此时 session_b 插入！
    span_a2 = obs.current_span()  # 可能返回 span_b1（错误！）
    obs.end_span(span_a1)  # span_a1 可能不在栈中（泄漏！）
    obs.end_trace()

async def session_b():
    await asyncio.sleep(0.05)
    trace_b = obs.start_trace(session_id="sess-b")
    span_b1 = obs.start_span("agent.loop")
    # start_trace 调用了 _active_span_stack.clear()
    # 这清除了 session_a 的 span_a1！
```

**修复后的正确行为**：
```python
# 改造后
async def session_a():
    trace_a = obs.start_trace(session_id="sess-a")
    # _span_stacks[trace_a.trace_id] = []
    # _session_trace_map["sess-a"] = trace_a.trace_id
    span_a1 = obs.start_span("agent.loop")
    # _span_stacks[trace_a.trace_id] = [span_a1]
    await asyncio.sleep(0.1)
    span_a2 = obs.current_span()  # 从 _session_trace_map["sess-a"] 找到 trace_a
                                  # 返回 _span_stacks[trace_a.trace_id][-1] = span_a1 ✓
    obs.end_span(span_a1)  # 在 _span_stacks[trace_a.trace_id] 中找到 span_a1 ✓
    obs.end_trace()  # 清理 _span_stacks[trace_a.trace_id] ✓

async def session_b():
    await asyncio.sleep(0.05)
    trace_b = obs.start_trace(session_id="sess-b")
    # _span_stacks[trace_b.trace_id] = []
    # _session_trace_map["sess-b"] = trace_b.trace_id
    # 【注意】没有清除任何其他栈 ✓
    span_b1 = obs.start_span("agent.loop")
    # _span_stacks[trace_b.trace_id] = [span_b1]
    # 与 session_a 的栈完全隔离 ✓
```
