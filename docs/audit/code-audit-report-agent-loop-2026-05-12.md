# Learning Agent 核心代码错误分析报告

> **审计范围**：`learning_agent/agent/agent_loop.py`、`learning_agent/core/`、`learning_agent/provider/`、`learning_agent/memory/`、`learning_agent/models/`
>
> **审计日期**：2026-05-12
>
> **审计方法**：静态代码审查，逐行阅读核心流程（ReACT Loop、Hook 系统、Provider 包装器、EventBus、Observability）

---

## 执行摘要

本次审计共发现 **13 个问题**，全部确认存在。其中：

| 级别 | 数量 | 说明 |
|------|------|------|
| 🔴 严重 | 3 | 可能导致功能异常、数据错乱、跨 Session 污染 |
| 🟡 中等 | 6 | 可靠性盲区，极端场景下会触发降级或卡死 |
| 🟢 轻微 | 4 | 技术债或性能优化点，建议排期修复 |

**最优先修复项**：问题 1（实例级状态污染）、问题 2（上下文压缩逻辑无效）、问题 3（无并发控制）。

---

## 🔴 严重问题

### 1. 实例级状态污染：多 Session 互相影响

| 属性 | 详情 |
|------|------|
| **文件** | `learning_agent/agent/agent_loop.py` |
| **行号** | 111–116 |
| **状态** | ✅ 确认存在 |

#### 问题描述

`AgentLoop` 通常作为全局单例存在，但以下关键状态均为**实例级变量**，而非按 `session_id` 隔离：

```python
# AgentLoop.__init__
self._failure_tracker = ToolFailureTracker(
    window_turns=self._resilience_config.tool_failure_window_turns,
    threshold=self._resilience_config.tool_failure_threshold,
)
self._chat_only_mode = False          # ← 实例级
self._chat_only_success_turns = 0     # ← 实例级
```

#### 影响分析

1. **Chat-Only 模式跨 Session 传染**：Session A 的工具连续失败 → `_chat_only_mode = True` → Session B 调用 `run()` 时 `tools_for_llm = None`，B 也无法使用工具。
2. **工具 Ban 跨 Session 传染**：Session A 中 `tool_x` 连续 `execution_error` 达到阈值 → `_failure_tracker` 对该工具全局 `is_banned()` → Session B 调用同一工具直接被拒绝。
3. **恢复计数错乱**：`_chat_only_success_turns` 被所有 Session 共享，导致自动恢复逻辑提前或延后触发。

#### 修复建议

将状态迁移到 `LearningSession` 模型中，或在 `AgentLoop` 内维护按 `session_id` 隔离的字典：

```python
self._session_states: dict[str, SessionRuntimeState] = {}
```

其中 `SessionRuntimeState` 包含 `chat_only_mode`、`chat_only_success_turns`、`failure_tracker` 等字段。

---

### 2. 上下文压缩不生效：ContextLengthError 重试无效

| 属性 | 详情 |
|------|------|
| **文件** | `learning_agent/agent/agent_loop.py` |
| **行号** | 861–876 |
| **状态** | ✅ 确认存在 |

#### 问题描述

```python
# _stream_chat_with_retry()
except ContextLengthError as e:
    if self._resilience_config.auto_compress_on_context_overflow:
        await self._compress_context(session)   # ← 修改了 session.entries
        await self.events.publish(...)
        continue   # ← 用同一个 params 重试！但 params.messages 还是旧的
```

#### 影响分析

`_compress_context()` 修改的是 `session.entries`（底层持久化列表），但 `params.messages` 是在之前 `_build_context_for_turn()` 中构建的**全新独立列表**（`ChatMessage` 对象的副本）。`continue` 后直接用旧 `params` 重调 Provider，上下文长度完全没有变短，重试必然再次失败，直到耗尽 `max_attempts` 后进入外层流中断兜底。

这导致 `auto_compress_on_context_overflow` 配置项**形同虚设**。

#### 修复建议

在 `continue` 前重新构建 `params.messages`：

```python
except ContextLengthError as e:
    if self._resilience_config.auto_compress_on_context_overflow:
        await self._compress_context(session)
        # 重新构建上下文，否则 params.messages 仍是压缩前的旧数据
        params.messages = await self._build_context_for_turn(session, user_input)
        continue
```

---

### 3. 无并发控制：Race Condition

| 属性 | 详情 |
|------|------|
| **文件** | `learning_agent/agent/agent_loop.py` |
| **行号** | 218–676 (`run()` 整体) |
| **状态** | ✅ 确认存在 |

#### 问题描述

`run()` 方法没有 session 级别的锁或重入保护。`AgentLoop` 的实例级状态（`_chat_only_mode`、`_failure_tracker`、`_chat_only_success_turns`）在并发下无锁访问。

#### 影响分析

如果同一个 `session.id` 被并发调用（例如 Web 端快速双击、SSE 重连导致重复请求）：

1. `tool_call_buffers` 字典会被多个协程同时写入，导致 tool call ID 错乱、参数拼接错误。
2. `_failure_tracker.record_failure()` / `record_success()` 在并发下计数错乱（`ToolFailureTracker` 内部无锁）。
3. `session.entries` 的 `append` 操作可能产生交错的消息历史（虽然 `SessionManager.append_message` 可能有自己的锁，但 AgentLoop 层面没有保证 turn 的原子性）。
4. `_chat_only_mode` 的读写完全无锁，多个协程同时进入降级/恢复逻辑会导致状态翻转错乱。

#### 修复建议

在 `AgentLoop` 中增加按 `session_id` 隔离的锁字典，并在 `run()` 入口获取：

```python
def __init__(...):
    self._session_locks: dict[str, asyncio.Lock] = {}

async def run(self, session, user_input, ask_mode=False):
    lock = self._session_locks.setdefault(session.id, asyncio.Lock())
    async with lock:
        ...  # 原有逻辑
```

---

## 🟡 中等问题

### 4. 最终兜底链路不够彻底

| 属性 | 详情 |
|------|------|
| **文件** | `learning_agent/agent/agent_loop.py` |
| **行号** | 645–658（外层 except）、1328–1359（`_finalize_with_llm`） |
| **状态** | ✅ 确认存在 |

#### 问题描述

全局兜底和流中断兜底都直接调用 `self.provider.stream_chat()`，**没有走 `_stream_chat_with_retry()`**。

```python
# run() 外层 except Exception:
final_params = ChatParams(...)
async for chunk in self.provider.stream_chat(final_params):   # ← 直接调用，无重试
    yield chunk
```

以及 `_finalize_with_llm()` 中：

```python
async for chunk in self.provider.stream_chat(final_params):   # ← 同样直接调用
    yield chunk
```

#### 影响分析

如果此时 Provider 仍然不可用（网络未恢复、Rate Limit 仍在、连接超时），会直接抛 `final_e`，只能输出静态错误文本 `[Error] Unable to continue...`。兜底链路的"兜底"能力不够彻底。

#### 修复建议

兜底链路也应调用 `_stream_chat_with_retry()`，或确保 `self.provider` 本身就是 `ResilientProvider`（注入时做类型校验或包装）。

---

### 5. Hook 系统无超时保护

| 属性 | 详情 |
|------|------|
| **文件** | `learning_agent/core/hook_system.py` |
| **行号** | 105–109 |
| **状态** | ✅ 确认存在 |

#### 问题描述

```python
for reg in registrations:
    start_ts = __import__("time").time()
    try:
        result = await reg.handler(current_data, context)   # ← 直接 await，无超时
```

#### 影响分析

工具执行有 `timeout`（默认 60s），但 Hook 执行没有超时。如果某个 Extension 的 Hook 死锁或阻塞（比如等待用户输入、网络 IO、访问数据库卡住），整个 Agent Turn 会永远卡住。

#### 修复建议

为 Hook 调用增加 `asyncio.wait_for` 超时：

```python
hook_timeout = context.get("hook_timeout", 30.0)
try:
    result = await asyncio.wait_for(reg.handler(current_data, context), timeout=hook_timeout)
except asyncio.TimeoutError:
    logger.warning(f"[HookSystem] Hook '{point.value}' timed out for {ext_id}")
    if trace_span:
        trace_span.tags[f"hook.{point.value}.{ext_id}.timed_out"] = True
    continue
```

---

### 6. 工具结果无大小限制/截断

| 属性 | 详情 |
|------|------|
| **文件** | `learning_agent/agent/agent_loop.py` |
| **行号** | 532–550 |
| **状态** | ✅ 确认存在 |

#### 问题描述

```python
result_content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
entry = self.sessions.append_message(
    session.id,
    MessageRole.TOOL,
    result_content,   # ← 完整写入，无大小限制
    metadata={...},
)
```

#### 影响分析

工具返回的大文本（如 `read_file` 读取 1MB 日志、`bash` 输出大量内容）会被完整追加到 `session.entries`。下一次 `_build_context_for_turn()` 时，这些大文本会挤占 Token 预算，甚至直接触发 `ContextLengthError`。

#### 修复建议

在追加前对结果进行截断。例如：

```python
MAX_TOOL_RESULT_CHARS = 16000  # 约 4K tokens
if len(result_content) > MAX_TOOL_RESULT_CHARS:
    truncated = result_content[:MAX_TOOL_RESULT_CHARS]
    truncated += f"\n... [truncated, original length: {len(result_content)} chars]"
    result_content = truncated
```

---

### 7. EventBus 无背压控制

| 属性 | 详情 |
|------|------|
| **文件** | `learning_agent/agent/agent_loop.py`、`learning_agent/core/event_bus.py` |
| **行号** | 131–144 (`_set_state` 中的 `create_task`) |
| **状态** | ✅ 确认存在，严重程度有限 |

#### 问题描述

```python
# _set_state() 中
asyncio.get_event_loop().create_task(
    self.events.publish(...)
)
```

`EventBus.publish()` 内部使用 `await asyncio.gather(...)`，但 `_set_state` 每次状态转换都用 `create_task` 发起一个**无界**的 background task。

#### 影响分析

如果状态转换极频繁（如高频异常抖动），未完成的 task 会持续堆积。虽然 `EventBus._history` 有 `history_limit=1000` 限制，但**订阅者 handler 的并发执行**没有队列上限。如果某个 handler 执行缓慢，并发 gather 会堆积。

由于 `_set_state` 的调用频率不高（每轮一次），实际影响有限，但在极端场景下仍存在内存泄漏风险。

#### 修复建议

1. 对 `_set_state` 中的事件发布做限速/批量合并。
2. 在 `EventBus` 中引入有界队列，或使用 `asyncio.Semaphore` 限制并发 handler 数量。

---

### 8. 确认判断过于粗糙

| 属性 | 详情 |
|------|------|
| **文件** | `learning_agent/agent/agent_loop.py` |
| **行号** | 1230–1253 |
| **状态** | ✅ 确认存在 |

#### 问题描述

```python
negation_patterns = {"不对", "不好", "不行", "不要", ... "no", "not", "don't"...}
for neg in negation_patterns:
    if neg in text:   # ← 子串匹配
        return False
```

#### 影响分析

- 用户说 `"no problem"` → 包含 `"no"` → **判为否定**（实际应为肯定）
- 用户说 `"don't worry, yes"` → 包含 `"don't"` → **判为否定**
- 用户说 `"not sure"` → **判为否定**（实际应继续询问，不应直接结束对齐）

#### 修复建议

按整词/词边界匹配（正则 `\bno\b`），或调整匹配策略避免子串误伤。例如：

```python
# 使用词边界或精确匹配
negation_whole_words = {"no", "not", "nope", "wrong", "incorrect", "不对", ...}
words = set(text.lower().split())
if words & negation_whole_words:
    return False
```

对于 `"not sure"` 等模糊表达，应返回 `None`（继续询问）而非 `False`（直接否定）。

---

### 9. 收尾 System Message 位置不当

| 属性 | 详情 |
|------|------|
| **文件** | `learning_agent/agent/agent_loop.py` |
| **行号** | 647–651（外层兜底）、1328–1335（`_finalize_with_llm`） |
| **状态** | ✅ 确认存在 |

#### 问题描述

```python
final_messages = await self._build_context_for_turn(session, user_input)
final_messages.append(ChatMessage(
    role=MessageRole.SYSTEM,
    content=...,
))   # ← 追加到末尾
```

#### 影响分析

OpenAI 等模型通常期望 `system` 消息在消息列表开头（或至少不在末尾被当作普通 user/assistant 消息处理）。虽然现代模型有容错，但 system 消息放在末尾可能导致角色遵循性下降。

#### 修复建议

```python
final_messages.insert(0, ChatMessage(role=MessageRole.SYSTEM, content=...))
```

---

## 🟢 轻微问题 / 技术债

### 10. 错误分类基于字符串匹配，易误伤

| 属性 | 详情 |
|------|------|
| **文件** | `learning_agent/provider/resilient_provider.py` |
| **行号** | 135–149 |
| **状态** | ✅ 确认存在 |

#### 问题描述

```python
def _classify_exception(self, exc: Exception) -> Exception:
    msg = str(exc).lower()
    if "rate limit" in msg or "timeout" in msg or "connection" in msg or "service unavailable" in msg:
        return ServiceUnavailable(str(exc))
```

#### 影响分析

错误信息 `"Invalid request: connection string format error"` 会因包含 `"connection"` 被误分类为 `ServiceUnavailable`（可重试），而不是 `InvalidRequestError`（不应重试）。这会导致不必要的重试循环，浪费 Token 和时间。

#### 修复建议

使用更精确的匹配模式（如要求 `"connection"` 与 `"refused/reset/error"` 组合），或优先基于异常类型（`isinstance`）分类，字符串匹配仅作为兜底：

```python
if isinstance(exc, (ConnectionError, TimeoutError, ConnectionRefusedError)):
    return ServiceUnavailable(str(exc))
# 字符串匹配仅对未知异常类型使用
msg = str(exc).lower()
if "rate limit" in msg or "too many requests" in msg:
    return ServiceUnavailable(str(exc))
```

---

### 11. 工具调用串行执行

| 属性 | 详情 |
|------|------|
| **文件** | `learning_agent/agent/agent_loop.py` |
| **行号** | 940 |
| **状态** | ✅ 确认存在 |

#### 问题描述

```python
results: list[tuple[ToolCall, Any, bool]] = []
for tc in tool_calls:
    ...
    # Step 1~8 全部串行
```

#### 影响分析

如果一次 LLM 响应中请求了多个无依赖的工具（如同时查天气和查股价），串行执行浪费了时间。现代 LLM 的 function calling 设计天然支持并行 tool calls。

#### 修复建议

无依赖的工具应使用 `asyncio.gather` 并发执行。需注意并发下 `session.entries` 的写入顺序——建议先收集所有结果，再按原 `tool_calls` 顺序写入：

```python
async def _execute_single_tool(...) -> tuple[ToolCall, Any, bool]:
    ...

tool_results = await asyncio.gather(*[_execute_single_tool(...) for tc in tool_calls])
# 然后按原顺序写入 session.entries
```

---

### 12. Span 可能泄漏

| 属性 | 详情 |
|------|------|
| **文件** | `learning_agent/agent/agent_loop.py`、`learning_agent/core/observability.py` |
| **状态** | ✅ 确认存在（特定路径 + 多 Session 并发时加剧） |

#### 问题描述

**路径一：HookAbortError 导致 `tool_span` 泄漏**

在 `_execute_tool_calls()` 中，`tool_span` 创建后，如果 `self.hooks.execute(HookPoint.ON_TOOL_CALL, ...)` 抛出 `HookAbortError`（在 `HookSystem.execute()` 第 126–131 行被捕获并重新抛出），AgentLoop 层面没有 catch 这个异常。异常直接向上传播到 `run()` 的外层 `except Exception`，在那里只关闭了 `root_span`，**`tool_span` 从未被关闭**。

**路径二：`ObservabilityCollector` 的 `_active_span_stack` 全局共享**

`ObservabilityCollector.start_trace()` 会 `self._active_span_stack.clear()`。如果多 Session 并发调用 `run()`，A Session 的 `start_trace` 会清空 B Session 正在使用的 span 栈，导致 span 树彻底错乱。

#### 修复建议

1. 在 `_execute_tool_calls` 的 Hook 调用外层加 `try...finally` 保护 `tool_span`。
2. 将 `ObservabilityCollector` 的 span 栈按 `trace_id` 隔离，或确保每个 `AgentLoop.run()` 使用独立的 trace 上下文。

---

### 13. 知识图谱/间隔重复模块未集成

| 属性 | 详情 |
|------|------|
| **文件** | `learning_agent/memory/knowledge_graph.py`、`learning_agent/memory/spaced_repetition.py`、`learning_agent/agent/agent_loop.py` |
| **状态** | ✅ 确认存在 |

#### 问题描述

- `knowledge_graph.py` 实现了 `KnowledgeGraph` 类（节点 CRUD、关联查询、文本相似度）。
- `spaced_repetition.py` 实现了 `SpacedRepetitionEngine`（简化版 SM-2 算法）。
- `AgentLoop.__init__` 虽然接收了 `memory_manager: MemoryManager`，但**仅在 3 处 Hook 上下文传递时作为参数透传**（`BEFORE_LLM_CALL`、`AFTER_RESPONSE`、`BEFORE_TOOL_RESULTS_PERSIST`）。

**核心循环中没有任何主动调用：**

- 没有调用 `memory_manager.promote_to_l2()`
- 没有调用 `memory_manager.get_due_reviews()`
- 没有调用 `memory_manager.relevant_recall()`
- 没有调用 `spaced_repetition_engine.schedule_first_review()` 或 `process_review()`

#### 影响分析

这意味着项目的记忆系统实际上**只有短期消息历史**（`session.entries`），没有真正的长期知识管理和复习调度。`KnowledgeGraph` 和 `SpacedRepetitionEngine` 虽然代码存在，但核心循环不感知它们，属于"写了但没接线"的僵尸模块。

#### 修复建议

在 `AgentLoop` 的关键节点（如 turn 结束、session 关闭时）主动调用 `MemoryManager` 的方法，或编写专门的 Extension Hook 来桥接核心循环与记忆系统。如果当前设计意图是让扩展层通过 Hook 完成集成，应在文档中明确说明，并提供默认的集成 Extension。

---

## 修改文件清单

| 文件 | 问题编号 | 改动说明 |
|------|----------|----------|
| `learning_agent/agent/agent_loop.py` | 1, 2, 3, 4, 6, 8, 9, 11, 12 | 状态隔离、上下文压缩修复、并发锁、兜底链路增强、工具结果截断、确认判断优化、system message 位置、并发执行、span 泄漏修复 |
| `learning_agent/core/hook_system.py` | 5 | 增加 Hook 执行超时保护 |
| `learning_agent/core/observability.py` | 12 | span 栈按 trace 隔离 |
| `learning_agent/provider/resilient_provider.py` | 10 | 异常分类改为 `isinstance` 优先 |
| `learning_agent/memory/*.py` | 13 | 需补充与 AgentLoop 的集成文档或默认 Extension |

---

## 关键决策与待办

1. **状态隔离方案选择**：是将运行时状态放入 `LearningSession` 模型，还是在 `AgentLoop` 中维护 `dict[str, SessionRuntimeState]`？前者更简洁，但需修改模型定义；后者更轻量，但需手动管理生命周期。
2. **并发锁粒度**：`run()` 整函数加锁最简单，但会阻塞同 Session 的所有并发请求。是否只允许单线程 per session？
3. **Hook 超时默认值**：建议默认 30s，可通过 `ResilienceConfig` 配置。
4. **知识图谱集成**：需确认设计意图——是核心层主动调用，还是完全由扩展层通过 Hook 完成？
