# 重试、兜底与工具输入校验——技术实现文档

> 版本：v1.0（终版）
> 日期：2026-05-11
> 用途：指导代码实现，不修改本文档中的架构决策

---

## 一、概述

### 1.1 目标

在现有 `learning-agent` 项目中引入分层韧性机制：
- **Provider 层**：网络级重试、多源 Fallback、熔断器
- **Agent Loop 层**：Turn 级重试、上下文压缩、纯对话降级
- **Tool 执行层**：输入校验（Pydantic/jsonschema）、error result 回流、滑动窗口禁用

### 1.2 设计原则

1. **不侵入业务**：通过包装器/前置步骤注入，原有逻辑保持独立运行
2. **工业对齐**：校验与回流机制参照 Claude Code `toolExecution.ts` 的生产实践
3. **流式透明**：前端可观测事件，但流式输出不中断
4. **Fail-Closed**：工具参数校验失败即拦截，不进入执行

---

## 二、数据模型变更

### 2.1 ToolDefinition（models.py）

```python
class ToolDefinition(BaseModel):
    id: str
    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)  # JSON Schema (OpenAI format)
    input_model: Optional[type[BaseModel]] = None              # 【新增】显式 Pydantic 模型
    handler: Optional[Callable[..., Awaitable[Any]]] = Field(default=None, exclude=True)
```

**约束**：
- `input_model` 为可选。若提供，校验器优先使用；若为 `None`，fallback 到 `jsonschema`
- 内置工具（`code_tools.py`）必须逐步迁移至 `input_model`
- 扩展工具保持现有 `parameters` 注册方式即可，无需修改

### 2.2 ToolCall（models.py）——无变更

继续使用现有 `ToolCall` 模型，校验失败时复用 `error` 字段：

```python
class ToolCall(BaseModel):
    tool_id: str
    call_id: Optional[str] = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Optional[Any] = None
    error: Optional[str] = None        # 校验失败时写入 "validation: ..."
    duration_ms: Optional[int] = None
```

**注意**：`error` 字段此前仅在 `execute()` 异常时设置。本方案扩展其语义，校验失败时也写入，前缀为 `validation:`。

### 2.3 SessionEntry（models.py）——无结构变更

`tool_results` 列表保持现有结构：

```python
{
    "tool_id": str,
    "tool_call_id": str,
    "result": str,
    "is_error": bool,
}
```

**约定**：错误类型不通过新增字段区分，而是通过 `result` 内容前缀区分：
- `[Tool Input Validation Failed]` —— 结构校验失败
- `[Blocked]` —— `tool_guard` 权限拒绝（abort）
- `[Approval Required]` —— `tool_guard` 权限询问（ask）
- `[Tool Unavailable]` —— 滑动窗口禁用
- `[Error]` —— 执行期异常

### 2.4 新增配置模型（config.py 或独立模块）

```python
class ResilienceConfig(BaseModel):
    # Provider 层
    provider_fallback_chain: list[str] = Field(default_factory=list)
    provider_retry_max_attempts: int = 3
    provider_retry_backoff_base: float = 2.0
    provider_retry_max_delay: float = 30.0
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_window_seconds: int = 60
    circuit_breaker_recovery_timeout: int = 30

    # Agent Loop 层
    turn_retry_max_attempts: int = 2
    auto_compress_on_context_overflow: bool = True
    react_turns_before_chat_fallback: int = 2

    # Tool 层
    tool_validation_enabled: bool = True
    tool_max_validation_history_groups: int = 3   # 同一工具 error 保留组数
    tool_failure_window_turns: int = 5            # 滑动窗口 turn 数
    tool_failure_threshold: int = 3               # 窗口内失败次数阈值
    tool_default_timeout: int = 60
```

---

## 三、组件设计

### 3.1 ToolInputValidator（新增）

**职责**：在工具执行前完成结构合规校验，不进入权限检查和实际执行。

**位置**：`learning_agent/core/tool_validator.py`（建议新建）

**接口**：

```python
class ValidationErrorDetail(BaseModel):
    param: str
    issue: str          # 如 "required field missing", "expected integer, got string"

class ToolInputValidator:
    def __init__(self, tool_registry: ToolRegistry):
        self._registry = tool_registry
        self._jsonschema_validators: dict[str, Any] = {}   # 懒加载缓存

    async def validate(self, tool_call: ToolCall) -> Optional[list[ValidationErrorDetail]]:
        """
        校验工具调用参数。
        返回 None 表示通过；返回列表表示失败详情。
        """
        ...

    def _get_jsonschema_validator(self, tool_id: str, schema: dict) -> Any:
        """获取或编译缓存的 jsonschema 校验器。"""
        ...
```

**校验顺序**：
1. 工具名存在性：查 `ToolRegistry`，不存在直接返回错误
2. JSON 可解析性：`tool_call.arguments` 必须是 `dict`（流式拼接后已由 AgentLoop 保证）
3. Pydantic 强校验：若 `ToolDefinition.input_model` 存在，用 `model_validate()`
4. JSON Schema 校验：fallback 到 `jsonschema.validate()`

**错误信息构造**（返回给 AgentLoop）：

```
[Tool Input Validation Failed]
Tool: {tool_id}
Call ID: {call_id}

Errors:
- Parameter '{param}': {issue}
- Parameter '{param}': {issue}

Hint: Please fix the parameters and retry.
```

### 3.2 ToolFailureTracker（新增）

**职责**：维护每个工具的滑动窗口失败计数，判断是否触发临时禁用。

**位置**：`learning_agent/core/tool_failure_tracker.py`（建议新建）或内嵌于 `SessionManager`

**接口**：

```python
class ToolFailureTracker:
    def __init__(self, window_turns: int = 5, threshold: int = 3):
        self._window = window_turns
        self._threshold = threshold
        self._counts: dict[str, list[int]] = {}   # tool_id -> [turn_count, ...]

    def record_failure(self, tool_id: str, turn_count: int) -> None:
        """记录一次工具失败（校验失败、权限拒绝、执行异常均计入）。"""
        ...

    def record_success(self, tool_id: str) -> None:
        """成功执行后清零该工具计数。"""
        ...

    def is_banned(self, tool_id: str, current_turn: int) -> bool:
        """判断工具是否在当前滑动窗口内被临时禁用。"""
        ...

    def get_ban_message(self, tool_id: str) -> str:
        """返回临时禁用的标准错误文本。"""
        ...
```

**行为**：
- `record_success` 时清空该工具的全部历史计数（彻底重置）
- `is_banned` 只统计 `current_turn - turn_count <= self._window` 的记录
- 计数包含所有失败类型（validation、abort、ask、execution error），因为无论哪种失败都代表工具未成功交付价值

### 3.3 ResilientProvider（新增）

**职责**：包装 `BaseProvider`，注入应用级重试、Fallback、熔断。

**位置**：`learning_agent/provider/resilient_provider.py`（建议新建）

**接口**：

```python
class ResilientProvider(BaseProvider):
    def __init__(
        self,
        primary: BaseProvider,
        fallback_chain: list[BaseProvider],
        config: ResilienceConfig,
    ):
        ...

    async def stream_chat(self, params: ChatParams) -> AsyncIterable[ChatChunk]:
        ...

    async def chat(self, params: ChatParams) -> ChatChunk:
        ...
```

**内部状态**：
- `_circuit_breakers: dict[str, CircuitBreaker]` —— 按 provider 标识维护熔断器
- `_attempt_counts: dict[str, int]` —— 当前 provider 的连续失败计数

**重试策略**：
- 仅对 `RetryableError` 子类触发指数退避
- `AuthError` / `InvalidRequestError` 直接抛出不重试
- 每次重试前检查目标 provider 的熔断器状态，若 `OPEN` 则直接切换 Fallback

### 3.4 AgentLoop 修改点

**文件**：`learning_agent/agent/agent_loop.py`

#### 修改点 A：初始化注入

`AgentLoop.__init__` 新增参数：

```python
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
    resilience_config: Optional[ResilienceConfig] = None,   # 【新增】
):
```

内部初始化：
```python
self._validator = ToolInputValidator(tool_registry)
self._failure_tracker = ToolFailureTracker(
    window_turns=config.tool_failure_window_turns,
    threshold=config.tool_failure_threshold,
)
self._resilience_config = resilience_config or ResilienceConfig()
```

#### 修改点 B：_execute_tool_calls 流水线重构

当前 `_execute_tool_calls()` 的每个 tool call 处理逻辑重构为以下严格顺序：

```
1.  emit TOOL_EXECUTION_START
2.  【新增】ToolFailureTracker.is_banned()? 
      → Yes: 直接返回 (tc, ban_message, True)，跳至步骤 8
3.  【新增】ToolInputValidator.validate(tc)
      → Fail: 记录 failure_tracker.record_failure(tc.tool_id, turn_count)
              构造 validation error message
              返回 (tc, error_message, True)，跳至步骤 8
4.  HookSystem.execute(ON_TOOL_CALL, tc, ...)
      → ask: 返回 (tc, ask_message, True)，跳至步骤 8
      → abort: 记录 failure_tracker.record_failure(tc.tool_id, turn_count)
               返回 (tc, abort_reason, True)，跳至步骤 8
5.  ToolRegistry.execute(tc)
      → Exception: 记录 failure_tracker.record_failure(tc.tool_id, turn_count)
                   返回 (tc, exception_message, True)，跳至步骤 8
      → Success: 记录 failure_tracker.record_success(tc.tool_id)
                  返回 (tc, result, False)
6.  HookSystem.execute(AFTER_TOOL_RESULT, tc, ...)
7.  emit TOOL_EXECUTION_END
8.  将结果（成功或失败）保存到 SessionEntry
9.  保存状态快照
```

#### 修改点 C：Turn 级重试

在 ReACT 内层循环中，`provider.stream_chat()` 失败时的处理：

```python
for attempt in range(self._resilience_config.turn_retry_max_attempts + 1):
    try:
        async for chunk in self.provider.stream_chat(params):
            ...
        break
    except ContextLengthError:
        self._compress_context(session)
        # 回滚本 turn 的 assistant message 草稿，重试
    except RetryableError as e:
        if attempt < max_attempts:
            await asyncio.sleep(backoff_delay)
            continue
        yield ChatChunk(content=f"\n[Error] LLM stream failed: {e}\n")
        error_message = str(e)
        break
    except (AuthError, InvalidRequestError):
        yield ChatChunk(content=f"\n[Error] {e}\n")
        break
```

**边界**：`ValidationError` / `ToolBannedError` 不会到达这里（它们在 Tool 执行层已被消费）。

#### 修改点 D：上下文构建时的历史压缩

`_build_context_for_turn()` 在组装 `ChatMessage` 列表时：

```python
# 新增：压缩同一工具的冗余 validation error 历史
history = self._compress_tool_error_history(history)
```

压缩规则（伪代码）：
```python
def _compress_tool_error_history(self, entries: list[SessionEntry]) -> list[SessionEntry]:
    """
    对连续同一工具的 validation error 进行压缩，最多保留最近 N 组。
    一组 = assistant entry(含 tool_calls) + 紧随其后的 tool error entry。
    """
    # 从旧到新扫描，统计每工具的 trial 组数
    # 超出 max_validation_history_groups 的早期组，标记为跳过
    # 成功的 tool result、权限 ask/abort、系统消息永不压缩
```

### 3.5 Provider 层初始化修改

**文件**：`learning_agent/main.py` 或 Provider 组装处

```python
# 伪代码
primary = OpenAIProvider(primary_config)
fallbacks = [OpenAIProvider(cfg) for cfg in fallback_configs]
provider = ResilientProvider(
    primary=primary,
    fallback_chain=fallbacks,
    config=resilience_config,
)
```

---

## 四、调用流程时序图

### 4.1 正常工具调用

```
User Input
    │
    ▼
AgentLoop.run()
    │
    ├──► Provider.stream_chat() ──► LLM
    │         │
    │         └──► AssistantMessage (含 tool_calls) 保存到历史
    │
    ▼
_execute_tool_calls()
    │
    ├──► ToolFailureTracker.is_banned()? ──► No
    │
    ├──► ToolInputValidator.validate() ──► Pass
    │
    ├──► HookSystem.execute(ON_TOOL_CALL) ──► Allow
    │
    ├──► ToolRegistry.execute() ──► Success
    │
    ├──► HookSystem.execute(AFTER_TOOL_RESULT)
    │
    └──► ToolResult 保存到历史
    │
    ▼
下一轮 LLM 调用（携带 tool_result）
```

### 4.2 校验失败路径

```
_execute_tool_calls()
    │
    ├──► ToolFailureTracker.is_banned()? ──► No
    │
    ├──► ToolInputValidator.validate() ──► Fail
    │         │
    │         └──► ValidationErrorDetail[]
    │
    ├──► ToolFailureTracker.record_failure(tool_id, turn_count)
    │
    ├──► EventBus.publish("agent.toolValidationFailed", {...})
    │
    └──► 构造 error ChatMessage(role=TOOL, is_error=True)
              content="[Tool Input Validation Failed]..."
          保存到历史，跳过 Hook 和 execute
    │
    ▼
下一轮 LLM 调用（携带 error tool_result）
```

### 4.3 工具被临时禁用路径

```
_execute_tool_calls()
    │
    ├──► ToolFailureTracker.is_banned()? ──► Yes
    │
    ├──► EventBus.publish("agent.toolBanned", {...})
    │
    └──► 构造 error ChatMessage(role=TOOL, is_error=True)
              content="[Tool Unavailable]..."
          保存到历史，跳过校验、Hook、execute
    │
    ▼
下一轮 LLM 调用（携带 banned tool_result）
```

### 4.4 Provider 熔断 + Fallback 路径

```
AgentLoop.run()
    │
    ├──► ResilientProvider.stream_chat()
    │         │
    │         ├──► CircuitBreaker.check()? ──► OPEN
    │         │
    │         ├──► 切换 Fallback Provider
    │         │
    │         ├──► FallbackProvider.stream_chat() ──► LLM
    │         │
    │         └──► EventBus.publish("resilience.providerFallback", {...})
    │
    └──► 正常继续
```

---

## 五、错误分类体系

所有错误必须归类为以下类型之一，作为各层决策依据：

```python
class ResilienceError(Exception):
    """韧性层错误基类。"""
    pass

class RetryableError(ResilienceError):
    """可重试：网络抖动、限流、超时、服务端不可用。"""
    pass

class ContextLengthError(RetryableError):
    """上下文过长：可重试 + 需触发上下文压缩。"""
    pass

class ValidationError(ResilienceError):
    """参数校验失败：不重试，回流给 LLM 自纠正。"""
    pass

class ToolBannedError(ResilienceError):
    """工具被临时禁用：不重试，回流给 LLM 决策。"""
    pass

class AuthError(ResilienceError):
    """认证/授权错误：不可重试。"""
    pass

class InvalidRequestError(ResilienceError):
    """请求格式错误：不可重试。"""
    pass

class ServiceUnavailable(ResilienceError):
    """服务端不可用：可重试 + 可触发熔断。"""
    pass
```

**决策矩阵**：

| 错误类型 | Provider 重试 | Turn 重试 | 熔断器 | 行为 |
|----------|--------------|-----------|--------|------|
| `RetryableError` | ✅ 指数退避 | ✅ | 计数+1 | 重试至上限后抛错 |
| `ContextLengthError` | ❌ | ✅ | 不计 | 压缩上下文后重试 Turn |
| `ValidationError` | ❌ | ❌ | 不计 | Error result 回流 |
| `ToolBannedError` | ❌ | ❌ | 不计 | Error result 回流 |
| `AuthError` | ❌ | ❌ | 直接 OPEN | 立即抛错 |
| `InvalidRequestError` | ❌ | ❌ | 不计 | 立即抛错 |
| `ServiceUnavailable` | ✅ | ✅ | 计数+1 | 重试 + 熔断 |

---

## 六、EventBus 事件规范

### 6.1 事件列表

| 事件类型 | 发射时机 | Payload |
|----------|---------|---------|
| `agent.toolValidationFailed` | ToolInputValidator 校验失败时 | `{"tool_id": str, "call_id": str, "errors": list[dict], "turn": int}` |
| `agent.toolBanned` | ToolFailureTracker 判定工具被禁用时 | `{"tool_id": str, "call_id": str, "turn": int, "failures_in_window": int}` |
| `agent.toolPermissionDenied` | tool_guard Hook 返回 abort 时 | `{"tool_id": str, "call_id": str, "reason": str}` |
| `agent.toolPermissionAsk` | tool_guard Hook 返回 ask 时 | `{"tool_id": str, "call_id": str, "message": str}` |
| `resilience.providerRetry` | Provider 应用级重试触发时 | `{"attempt": int, "delay": float, "provider": str, "error": str}` |
| `resilience.providerFallback` | 切换到 Fallback Provider 时 | `{"from": str, "to": str, "reason": str}` |
| `resilience.circuitBreakerStateChange` | 熔断器状态变更时 | `{"provider": str, "old_state": str, "new_state": str}` |
| `agent.turnRetry` | Agent Loop Turn 级重试触发时 | `{"attempt": int, "reason": str}` |
| `agent.contextCompressed` | 上下文压缩触发时 | `{"original_messages": int, "remaining_messages": int, "reason": str}` |

### 6.2 前端视觉区分映射

前端根据事件类型渲染不同样式的工具卡片：

| 场景 | 事件 | 建议视觉 |
|------|------|---------|
| 校验失败 | `agent.toolValidationFailed` | 🟡 黄色警告卡片，标题"参数错误" |
| 权限拒绝 | `agent.toolPermissionDenied` | 🔴 红色阻断卡片，标题"已阻止" |
| 权限询问 | `agent.toolPermissionAsk` | 🟠 橙色确认卡片，标题"等待确认" |
| 工具禁用 | `agent.toolBanned` | ⚫ 灰色禁用卡片，标题"工具不可用" |
| 执行异常 | `agent.toolExecutionEnd` (is_error=True) | 🔴 红色错误卡片，标题"执行失败" |
| 执行成功 | `agent.toolExecutionEnd` (is_error=False) | 🟢 绿色成功卡片 |

---

## 七、边界条件与异常处理

### 7.1 工具未找到

**场景**：LLM 生成了不存在的 `tool_id`。

**处理**：
1. `ToolInputValidator.validate()` 在步骤 1（存在性检查）失败
2. 返回错误：`"No such tool available: {tool_id}"`
3. 记录 `failure_tracker.record_failure(tool_id, turn_count)`
4. 构造 `ValidationError` 格式的 error result 回流

### 7.2 JSON Schema 过于复杂

**场景**：扩展工具的 `parameters` 包含 `anyOf`, `$ref`, `allOf` 等高级 JSON Schema 特性。

**处理**：
- `jsonschema` 库原生支持 JSON Schema Draft 7/2019-09/2020-12
- 若遇到不支持的构造，校验器捕获异常，降级为宽松校验（仅检查 `type: object` + 必填字段存在性）
- 记录 warning log，不阻塞执行

### 7.3 Pydantic 模型与 JSON Schema 不一致

**场景**：某工具同时提供了 `input_model` 和 `parameters`，但两者不完全等价。

**处理**：
- 以 `input_model` 为准（优先使用）
- 在 `ToolRegistry.register()` 时可选增加一致性检查（开发期断言，生产期忽略）

### 7.4 流式中断

**场景**：用户在前端发送新消息，打断正在进行的流式响应。

**处理**：
- 现有 `AbortController` / `abortController.signal` 机制继续生效
- 若中断发生在工具执行阶段，已完成的 tool results 保留，未执行的丢弃
- `ToolFailureTracker` 不记录因中断而未执行的工具

### 7.5 Session 恢复

**场景**：从磁盘/数据库加载历史 Session，继续对话。

**处理**：
- `ToolFailureTracker` 的内存计数**不持久化**，session 恢复后重新从零累积
- 这是可接受的，因为滑动窗口是短期防御机制，跨 session 记忆无必要
- 若未来需要跨 session 持久化，可在 `SessionEntry.metadata` 中存储 `tool_failure_counts` 快照

### 7.6 并发工具调用

**场景**：LLM 在一次响应中并行调用多个工具（OpenAI 支持并行 function calling）。

**处理**：
- `ToolInputValidator` 对每个 `tool_call` 独立校验，互不影响
- `ToolFailureTracker` 对每个 `tool_id` 独立计数
- 任一工具校验失败，不影响其他合法工具的正常执行

### 7.7 降级到纯对话的触发条件

**场景**：模型因某种原因持续无法正确使用工具。

**处理**：
- 当连续 `react_turns_before_chat_fallback`（默认 2）个 turn 都因 tool 相关错误（validation/abort/execution）而未能产出有效 tool result 时
- AgentLoop 清空 `params.tools`，以纯对话模式继续当前 session
- 不清除历史，只是不再向模型暴露工具 Schema
- 可通过配置 `resilience_config.react_turns_before_chat_fallback = 0` 关闭此降级

---

## 八、依赖变更

### 8.1 新增依赖

```
jsonschema>=4.0       # 扩展工具的 JSON Schema 校验 fallback
```

### 8.2 现有依赖复用

- `pydantic>=2.0`：已存在，用于 `ToolDefinition.input_model` 校验
- 现有 `openai` SDK：`max_retries` 继续负责 HTTP 层瞬态错误重试

---

## 九、实施检查清单

### Phase 1：Tool 校验框架（P0）

- [ ] 新建 `learning_agent/core/tool_validator.py`，实现 `ToolInputValidator`
- [ ] 新建 `learning_agent/core/tool_failure_tracker.py`，实现 `ToolFailureTracker`
- [ ] `models.py`：`ToolDefinition` 新增 `input_model` 字段
- [ ] `agent_loop.py`：`_execute_tool_calls()` 重构为 8 步流水线
- [ ] `agent_loop.py`：注入 `_validator` 和 `_failure_tracker`
- [ ] `agent_loop.py`：`_build_context_for_turn()` 增加历史压缩逻辑
- [ ] `agent_loop.py`：新增 Turn 级重试逻辑（`_run_react_turn_with_retry`）
- [ ] `config.py`：新增 `ResilienceConfig` 模型
- [ ] `requirements.txt`：新增 `jsonschema`

### Phase 2：内置工具迁移（P0）

- [ ] `code_tools.py`：`read_file`, `write_file`, `edit_file`, `bash` 定义 Pydantic Input Model
- [ ] 注册时传入 `input_model` 参数
- [ ] 验证 `ToolInputValidator` 对内置工具走 Pydantic 路径

### Phase 3：Provider 韧性（P1）

- [ ] 新建 `learning_agent/provider/resilient_provider.py`
- [ ] 实现 `CircuitBreaker` 状态机
- [ ] 实现指数退避重试逻辑
- [ ] 组装主备 Provider 链
- [ ] `main.py`：初始化时包装 `ResilientProvider`

### Phase 4：观测与前端（P1-P2）

- [ ] `agent_loop.py`：在各决策点发射 EventBus 事件
- [ ] `web/observability.html`：订阅新增事件，渲染差异化工具卡片
- [ ] 补充 span tag：`retry.count`, `fallback.target`, `circuit_breaker.state`

### Phase 5：测试与调优（P2-P3）

- [ ] 单元测试：`ToolInputValidator` 各种通过/失败场景
- [ ] 单元测试：`ToolFailureTracker` 滑动窗口边界
- [ ] 集成测试：完整 ReACT 循环中校验失败 → 回流 → 自纠正
- [ ] 压力测试：熔断器在高频失败下的表现

---

## 十、已确认决策汇总（讨论归档）

| # | 决策项 | 结论 |
|---|--------|------|
| 1 | 校验层位置 | `AgentLoop._execute_tool_calls()` 内，Hook 之前 |
| 2 | 校验 vs 权限顺序 | 先结构校验，后 `tool_guard` 权限 Hook |
| 3 | Pydantic 实现 | 双轨制：内置工具显式 `BaseModel`，扩展工具 fallback 到 `jsonschema` |
| 4 | Error Result 格式 | 内容前缀区分，不附带完整参数定义 |
| 5 | 历史污染 | 保留错误历史；`_build_context_for_turn` 压缩同一工具至最近 3 组 |
| 6 | 错误类型标记 | 不改 `SessionEntry` 模型，用内容前缀 + EventBus 事件做区分 |
| 7 | 连续失败兜底 | Sliding window（5 turn / 3 次）→ 工具临时禁用，LLM 自主决策 |
| 8 | 禁用后工具列表 | 保留在 `ChatParams.tools` 中，不主动移除 |
| 9 | 恢复机制 | 参照 Claude Code：成功执行后清零 / session 结束重置，无手动解封 |
| 10 | 前端视觉 | 特殊视觉区分（黄/红/橙/灰/绿） |
| 11 | Turn 级重试边界 | 仅针对 LLM 调用层失败，不覆盖 Tool 层失败 |
| 12 | 组件边界 | 独立 `ToolInputValidator` + `ToolFailureTracker`，AgentLoop 注入使用 |
| 13 | jsonschema 依赖 | 接受引入 |
| 14 | 降级到纯对话 | 连续 2 turn 工具失败后，清空 tools 进入纯对话模式 |

---

*本文档由架构讨论生成，代码实现阶段如有细节调整，需同步更新本文档。*
