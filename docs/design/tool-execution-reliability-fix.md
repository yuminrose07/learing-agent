# 工具执行可靠性修复 —— Bug 修复技术文档

> 版本：v1.0
> 日期：2026-05-11
> 依赖文档：`AGENTS.md` 自愈与持续运行原则、分层自治原则

---

## 一、概述

### 1.1 背景

Agent Loop 的 `_execute_tool_calls()` 已实现 8 步流水线，具备基本的错误分类、Ban 策略、降级机制。但在长期运行场景下，代码中存在 4 个影响**自主、稳定、长期运行**的缺陷。

### 1.2 设计目标

| 缺陷 | 修复目标 | 对应原则 |
|------|---------|---------|
| 工具执行无重试 | 对 transient 异常自动重试，减少 LLM 决策压力 | 自愈与持续运行 |
| Ban 策略混计所有失败 | 区分失败类型，只对服务级故障 Ban | 分层自治 |
| Chat-Only 单向降级 | 临时故障不造成永久损伤，支持自动恢复 | 自愈与持续运行 |
| 工具超时未实现 | 防止单个工具挂死导致整个 Agent Loop 阻塞 | 长期运行 |

---

## 二、问题清单

### 🔴 问题 1：工具执行层完全没有重试

**代码位置**：`learning_agent/agent/agent_loop.py`，`_execute_tool_calls()` Step 5

**现状**：
```python
try:
    result = await self.tools.execute(tc)
    # ... 成功处理 ...
except Exception as e:
    self._failure_tracker.record_failure(tc.tool_id, turn_count)
    # ... 直接返回错误给 LLM ...
```

**问题**：无论工具是因为网络超时、503、连接重置等**可恢复原因**失败，还是逻辑错误等**不可恢复原因**失败，都是**一次失败后直接抛给 LLM**。这意味着：
- 一个简单的网络抖动需要消耗额外 ReACT turn（LLM 重新决策 → 重新调用工具）
- LLM 可能因为没有重试信息而告诉用户"服务不可用"

**根因**：`ToolRegistry.execute()` 和 `_execute_tool_calls()` 均未实现重试逻辑。

---

### 🟡 问题 2：Ban 策略混计所有失败类型

**代码位置**：`learning_agent/core/tool_failure_tracker.py`

**现状**：
```python
def record_failure(self, tool_id: str, turn_count: int) -> None:
    """记录一次工具失败（校验失败、权限拒绝、执行异常均计入）。"""
```

**问题**：
- LLM 反复传错参数（`validation_failed`）→ 工具被 Ban
- 安全策略拒绝（`hook_abort`）→ 工具被 Ban
- 用户未确认（`hook_ask`）→ 不计数

**后果**：一个工具可能因为 **"LLM 不会用"** 或 **"策略太严"** 而被临时禁用，而不是因为工具本身不稳定。Agent 在长期运行中会**逐渐失去可用工具**。

**根因**：`record_failure` 不区分失败原因，`is_banned` 统计所有类型。

---

### 🟡 问题 3：Chat-Only 降级是单向的，没有恢复

**代码位置**：`learning_agent/agent/agent_loop.py`，`run()`

**现状**：
```python
if consecutive_tool_failure_turns >= fallback_limit:
    self._chat_only_mode = True
# 代码中没有任何地方将 _chat_only_mode 设为 False
```

**问题**：某外部服务临时挂了 30 秒，导致连续 2 个 turn 全部工具失败，Agent **永久进入纯对话模式**。后续即使服务恢复，该 Session 也再也不能调用工具。

**后果**：临时故障造成**永久性能力损伤**，违背"自愈与持续运行"原则。

**根因**：降级触发后缺少恢复机制。

---

### 🟡 问题 4：`tool_default_timeout` 配置存在但未实现

**代码位置**：`learning_agent/core/tool_registry.py`，`execute()`

**现状**：
```python
async def execute(self, tool_call: ToolCall) -> Any:
    result = await handler(**tool_call.arguments)  # ← 无限等待
```

**问题**：`ResilienceConfig` 中定义了 `tool_default_timeout: int = 60`，但 `ToolRegistry.execute()` 中没有任何 `asyncio.wait_for` 或超时逻辑。如果某个工具 handler 死锁、无限等待或调用第三方 API 时阻塞，**整个 Agent Loop 会挂死**。

**后果**：单个工具的故障级联为整个 Session 的瘫痪。

**根因**：配置与实现脱节。

---

## 三、修复方案

### 3.1 修复 1：工具执行层增加有限重试

#### 新增配置

```python
class ResilienceConfig(BaseModel):
    # ... 现有字段 ...
    
    # 【新增】工具执行失败后的重试次数（0 = 不重试）
    max_tool_retries: int = 1
    
    # 【新增】工具重试基础延迟（秒），指数退避
    tool_retry_base_delay: float = 1.0
    
    # 【新增】工具重试最大延迟（秒）
    tool_retry_max_delay: float = 5.0
```

#### 可重试异常判断

```python
def _is_retryable_tool_error(self, error: Exception) -> bool:
    """判断工具错误是否属于 transient 故障，值得重试。"""
    retryable_exceptions = (
        TimeoutError,
        asyncio.TimeoutError,
        ConnectionError,
        ConnectionRefusedError,
        ConnectionResetError,
        BrokenPipeError,
    )
    if isinstance(error, retryable_exceptions):
        return True
    
    # 检查常见 HTTP/服务错误关键词
    error_str = str(error).lower()
    retryable_keywords = [
        "503", "502", "504", "429",
        "timeout", "connection reset", "connection refused",
        "temporarily unavailable", "service unavailable",
        "too many requests", "rate limit",
    ]
    return any(kw in error_str for kw in retryable_keywords)
```

#### `_execute_tool_calls` Step 5 改造

```python
# Step 5: ToolRegistry.execute() 带重试
max_tool_retries = self._resilience_config.max_tool_retries
tool_retry_count = 0
last_error = None

while True:
    try:
        result = await self.tools.execute(tc)
        tc.result = result
        self._failure_tracker.record_success(tc.tool_id)
        # ... 成功处理 ...
        break
    except Exception as e:
        last_error = e
        is_retryable = self._is_retryable_tool_error(e)
        
        if tool_retry_count < max_tool_retries and is_retryable:
            tool_retry_count += 1
            delay = min(
                self._resilience_config.tool_retry_base_delay * (2 ** (tool_retry_count - 1)),
                self._resilience_config.tool_retry_max_delay,
            )
            logger.warning(
                f"[AgentLoop] Tool '{tc.tool_id}' failed transiently "
                f"(attempt {tool_retry_count}/{max_tool_retries + 1}), "
                f"retrying in {delay}s..."
            )
            await asyncio.sleep(delay)
            continue
        else:
            # 重试耗尽或非可重试异常 → 记录失败并返回错误
            self._failure_tracker.record_failure(tc.tool_id, turn_count, reason="execution_error")
            tc.error = str(last_error)
            # ... 错误处理 ...
            break
```

**关键约束**：
- 只对 `execution_error` 类型的 transient 异常重试
- `validation_failed`、`hook_abort`、`banned` 等前置失败不走重试（它们在 Step 5 之前已被拦截）
- 重试成功 → 不记录失败计数（`record_success`）
- 重试耗尽 → 才记录失败计数（`record_failure`）

---

### 3.2 修复 2：Ban 策略区分失败类型

#### `ToolFailureTracker` 改造

```python
class ToolFailureTracker:
    def __init__(self, window_turns: int = 5, threshold: int = 3):
        self._window = window_turns
        self._threshold = threshold
        # tool_id -> [(turn_count, reason), ...]
        self._counts: dict[str, list[tuple[int, str]]] = {}

    def record_failure(self, tool_id: str, turn_count: int, reason: str = "execution_error") -> None:
        """记录一次工具失败，附带失败原因。"""
        if tool_id not in self._counts:
            self._counts[tool_id] = []
        self._counts[tool_id].append((turn_count, reason))
        logger.debug(
            f"[ToolFailureTracker] Recorded failure for '{tool_id}' "
            f"at turn {turn_count} (reason={reason})."
        )

    def is_banned(self, tool_id: str, current_turn: int) -> bool:
        """判断工具是否在当前滑动窗口内被临时禁用。
        
        只对 'execution_error' 类型的失败进行 ban 计数。
        validation_failed、hook_abort 等不计入 ban。
        """
        entries = self._counts.get(tool_id, [])
        if not entries:
            return False
        
        failures_in_window = sum(
            1 for turn, reason in entries
            if current_turn - turn <= self._window and reason == "execution_error"
        )
        return failures_in_window >= self._threshold

    def get_failures_in_window(self, tool_id: str, current_turn: int) -> int:
        """返回工具在当前窗口内的 execution_error 失败次数。"""
        entries = self._counts.get(tool_id, [])
        return sum(
            1 for turn, reason in entries
            if current_turn - turn <= self._window and reason == "execution_error"
        )

    def get_all_failures_in_window(self, tool_id: str, current_turn: int) -> dict[str, int]:
        """返回各类失败在窗口内的分布（用于观测）。"""
        entries = self._counts.get(tool_id, [])
        counts: dict[str, int] = {}
        for turn, reason in entries:
            if current_turn - turn <= self._window:
                counts[reason] = counts.get(reason, 0) + 1
        return counts
```

#### `_execute_tool_calls` 调用方修改

```python
# validation_failed → 不触发 ban
self._failure_tracker.record_failure(tc.tool_id, turn_count, reason="validation_failed")

# hook_abort → 不触发 ban
self._failure_tracker.record_failure(tc.tool_id, turn_count, reason="hook_abort")

# execution_error（Step 5 except 块）→ 触发 ban
self._failure_tracker.record_failure(tc.tool_id, turn_count, reason="execution_error")
```

**关键约束**：
- `validation_failed`：LLM 参数错误，不应惩罚工具
- `hook_abort`：策略拒绝，不应惩罚工具
- `execution_error`：服务级故障，才需要 Ban 保护
- `record_success` 仍清空全部历史（彻底重置）

---

### 3.3 修复 3：Chat-Only 降级支持自动恢复

#### 新增配置

```python
class ResilienceConfig(BaseModel):
    # ... 现有字段 ...
    
    # 【新增】连续多少个无工具调用的成功 turn 后退出 chat-only 模式
    # 0 = 永不自动恢复（保持现有单向行为）
    chat_only_recovery_turns: int = 3
```

#### `AgentLoop` 新增状态变量

```python
def __init__(...):
    # ... 现有初始化 ...
    self._chat_only_success_turns = 0
```

#### `run()` 方法改造（降级恢复逻辑）

在现有降级逻辑之后，增加恢复逻辑：

```python
# 现有降级逻辑（不变）
if turn_has_valid_tool_result:
    consecutive_tool_failure_turns = 0
else:
    consecutive_tool_failure_turns += 1
    fallback_limit = self._resilience_config.react_turns_before_chat_fallback
    if fallback_limit > 0 and consecutive_tool_failure_turns >= fallback_limit:
        logger.warning(
            f"[AgentLoop] Tool failures reached {consecutive_tool_failure_turns} "
            f"consecutive turns. Downgrading to chat-only mode."
        )
        self._chat_only_mode = True
        self._chat_only_success_turns = 0  # 重置恢复计数

# 【新增】Chat-Only 自动恢复逻辑
if self._chat_only_mode:
    recovery_limit = self._resilience_config.chat_only_recovery_turns
    if recovery_limit > 0:
        # 本 turn 没有工具调用需求（LLM 直接回答了），算一个"成功对话 turn"
        if not has_tool_calls:
            self._chat_only_success_turns += 1
            if self._chat_only_success_turns >= recovery_limit:
                logger.info(
                    f"[AgentLoop] Auto-recovered from chat-only mode after "
                    f"{self._chat_only_success_turns} successful turns."
                )
                self._chat_only_mode = False
                self._chat_only_success_turns = 0
                consecutive_tool_failure_turns = 0
                await self.events.publish(
                    Event(
                        type="agent.chatOnlyRecovered",
                        payload={"recovery_turns": self._chat_only_success_turns},
                        source="agent_loop",
                        session_id=session.id,
                    )
                )
        else:
            # 本 turn 有工具调用但 chat-only 模式下被跳过了，不算恢复
            pass
else:
    self._chat_only_success_turns = 0
```

**关键约束**：
- 只有在 **chat-only 模式下且本 turn 没有触发工具调用** 时，才累计恢复计数
- 恢复后 `consecutive_tool_failure_turns` 清零，避免立即再次降级
- `chat_only_recovery_turns = 0` 时保持现有单向行为（兼容旧配置）

---

### 3.4 修复 4：工具执行增加超时保护

#### `ToolRegistry.execute()` 改造

```python
import asyncio

async def execute(self, tool_call: ToolCall, timeout: Optional[float] = None) -> Any:
    """执行工具调用，带超时保护。"""
    handler = self._handlers.get(tool_call.tool_id)
    if not handler:
        raise ValueError(f"Tool '{tool_call.tool_id}' not found")

    import time
    start = time.time()
    
    try:
        if asyncio.iscoroutinefunction(handler):
            coro = handler(**tool_call.arguments)
        else:
            # 同步函数包装为异步
            coro = asyncio.to_thread(handler, **tool_call.arguments)
        
        result = await asyncio.wait_for(coro, timeout=timeout or 60.0)
        
        tool_call.result = result
        tool_call.duration_ms = int((time.time() - start) * 1000)
        return result
        
    except asyncio.TimeoutError:
        tool_call.duration_ms = int((time.time() - start) * 1000)
        raise TimeoutError(
            f"Tool '{tool_call.tool_id}' execution timed out after "
            f"{timeout or 60.0}s. The tool handler did not complete within the allowed time."
        )
    except Exception:
        tool_call.duration_ms = int((time.time() - start) * 1000)
        raise
```

#### `_execute_tool_calls` 调用方修改

```python
# Step 5: 传入超时配置
timeout = self._resilience_config.tool_default_timeout
result = await self.tools.execute(tc, timeout=timeout)
```

**关键约束**：
- 超时后抛出 `TimeoutError`，会被 `_is_retryable_tool_error` 识别为可重试异常
- 同步 handler 通过 `asyncio.to_thread` 包装，避免阻塞事件循环
- 超时配置 `tool_default_timeout` 终于真正生效

---

## 四、EventBus 事件规范（新增/扩展）

| 事件类型 | 发射时机 | Payload |
|----------|---------|---------|
| `agent.toolRetry` | 工具执行 transient 失败后、重试前 | `{"tool_id": str, "attempt": int, "max_attempts": int, "delay": float, "reason": str}` |
| `agent.chatOnlyRecovered` | Chat-Only 模式自动恢复后 | `{"recovery_turns": int, "session_id": str}` |
| `agent.toolBanned` | Payload 扩展 `failure_types` | `{"tool_id": str, "failures_in_window": int, "failure_types": dict[str, int]}` |

---

## 五、实施检查清单

### Phase 1：配置模型

- [ ] `models.py`：`ResilienceConfig` 新增 `max_tool_retries`、`tool_retry_base_delay`、`tool_retry_max_delay`、`chat_only_recovery_turns`

### Phase 2：工具失败追踪器

- [ ] `tool_failure_tracker.py`：`record_failure` 增加 `reason` 参数
- [ ] `tool_failure_tracker.py`：`is_banned` 只统计 `execution_error`
- [ ] `tool_failure_tracker.py`：新增 `get_all_failures_in_window` 方法

### Phase 3：工具注册表超时

- [ ] `tool_registry.py`：`execute()` 增加 `timeout` 参数，使用 `asyncio.wait_for`
- [ ] `tool_registry.py`：同步 handler 通过 `asyncio.to_thread` 包装

### Phase 4：AgentLoop 核心修改

- [ ] `agent_loop.py`：新增 `_is_retryable_tool_error()` 方法
- [ ] `agent_loop.py`：`_execute_tool_calls` Step 5 增加重试循环
- [ ] `agent_loop.py`：`_execute_tool_calls` 各失败分支传入正确的 `reason`
- [ ] `agent_loop.py`：`run()` 增加 Chat-Only 自动恢复逻辑
- [ ] `agent_loop.py`：`__init__` 初始化 `_chat_only_success_turns = 0`

### Phase 5：验证

- [ ] 场景 A：模拟工具 handler 抛出 `ConnectionError`，确认重试 1 次后成功
- [ ] 场景 B：模拟工具连续 validation 失败，确认不触发 ban
- [ ] 场景 C：模拟工具连续 execution_error，确认触发 ban
- [ ] 场景 D：降级到 chat-only 后，连续 3 个无工具 turn，确认自动恢复
- [ ] 场景 E：模拟工具 handler 死锁，确认超时后抛出 `TimeoutError`
- [ ] 场景 F：模拟重试耗尽后 execution_error，确认错误回流给 LLM

---

## 六、决策汇总

| # | 决策项 | 结论 |
|---|--------|------|
| 1 | 工具执行重试次数 | `max_tool_retries = 1`（默认），对 transient 异常最多重试 1 次 |
| 2 | 重试退避策略 | 指数退避：`base_delay * 2^(attempt-1)`，上限 `max_delay = 5s` |
| 3 | 可重试异常范围 | `TimeoutError`、`ConnectionError`、HTTP 503/502/504/429 等 transient 故障 |
| 4 | 不可重试异常 | 逻辑错误、参数错误、权限拒绝等，直接失败 |
| 5 | Ban 计数范围 | **只统计 `execution_error`**，validation / abort / ask 不计入 |
| 6 | Chat-Only 恢复 | 连续 `chat_only_recovery_turns = 3` 个无工具调用的成功 turn 后自动恢复 |
| 7 | 超时实现 | `asyncio.wait_for`，默认 60s，同步 handler 走 `asyncio.to_thread` |
| 8 | 重试成功后 | 调用 `record_success()` 清空失败计数，不记录本次失败 |
| 9 | 重试耗尽后 | 调用 `record_failure(reason="execution_error")`，错误回流给 LLM |

---

*本文档记录 Agent 工具执行层的可靠性缺陷及修复方案，代码实现阶段如有调整需同步更新。*
