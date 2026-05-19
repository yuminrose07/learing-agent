# 技术设计文档：Hook 重构与最小运行时切点收敛

> 目标：在不破坏现有 Agent 主链可运行性的前提下，将 Hook 系统收敛为 5 个稳定切点，并为后续代码实现提供可直接落地的技术方案。
> 设计原则：Core 持有主流程裁决权，Hook 提供结构化前后置策略切点，Extension 仅实现可插拔能力。
> 文档日期：2026-05-13

---

## 一、背景与目标

### 1.1 当前问题

当前 Hook 机制存在以下结构性问题：

- Hook 点定义过多，实际进入运行时主链的点位较少，存在明显“预留定义多、真实接线少”的情况。
- Hook 输入输出契约过于泛化，当前主要通过 `data: Any` 和 `context: dict[str, Any]` 传递，调用方和实现方的边界不清晰。
- `HookResult` 统一承载 `modified / abort / ask / data` 等多种语义，导致不同阶段的 Hook 都在复用同一套结果结构，难以建立严格契约。
- Hook 与 Event、Extension 的耦合关系过宽，部分扩展依赖旧 Hook 命名或旧事件语义，容易出现“已注册但未真正接通”的问题。
- `AgentLoop` 当前直接调用多个旧 Hook 点，历史语义包括上下文压缩、流式 chunk 处理、工具调用前后处理、tool results persist 前修正，缺少以“最小稳定运行时”为导向的统一设计。

### 1.2 本次重构目标

本次 Hook 重构只服务当前阶段的 3 个核心目标：

1. 支持稳定的流式传输，Web 端可以持续接收流式内容。
2. 确保 `agent-loop` 长期稳定运行，工具调用、权限检验、安全策略可稳定执行。
3. 保证 Agent 主链具备完整可观测性，关键流程、异常、重试、审计都能记录。

### 1.3 本次重构非目标

以下能力不属于本轮 Hook 重构必须完成的目标：

- 记忆管理扩展接入
- 上下文压缩策略扩展
- 复习调度、知识提取等上层能力
- Session fork/end 等未来型 Hook
- 对 Event 系统做全面重命名或统一大重构

这些能力后续可基于本轮稳定的 Hook 契约增量接回。

### 1.4 核心设计判断

本轮方案采用以下判断：

- `AgentLoop` 仍然是唯一主裁决者。
- Hook 只提供标准切点，不直接驱动状态机。
- Extension 仍然保留，但只负责实现 Hook handler 或 Event consumer。
- 只有少数前后置切点保留为 Hook；主流程编排、失败计数、重试、降级、持久化仍由 Core 控制。

---

## 二、总体设计

### 2.1 目标架构

重构后的职责分层如下：

```text
Core Runtime
  - AgentLoop
  - Tool execution pipeline
  - Permission / validation final decision
  - Retry / fallback / downgrade
  - Session persistence
  - Core observability

Hook System
  - 提供 5 个稳定切点
  - 定义 typed input / typed result
  - 负责多 handler 顺序执行与结果合并
  - 不负责状态机裁决

Extensions
  - 实现 hook handlers
  - 实现 event consumers
  - 实现审计、观测增强、策略增强
  - 不承载核心正确性
```

### 2.2 最终保留的 5 个 Hook

本轮收敛后，运行时只保留以下 5 个 Hook：

1. `before_agent_run`
2. `before_tool_execute`
3. `after_tool_execute`
4. `on_stream_chunk`
5. `after_response`

### 2.3 五个 Hook 的角色

| Hook | 角色 | 典型用途 | 是否允许阻断 |
| --- | --- | --- | --- |
| `before_agent_run` | Gate + Mutate | 启动前检查、环境验证、配置补丁、启动审计 | 允许 |
| `before_tool_execute` | Gate + Mutate | 权限检查、安全检查、策略级参数修正、ask/deny | 允许 |
| `after_tool_execute` | PostProcess + Observe | 结果审计、结果脱敏、元数据补充、日志 | 不允许 |
| `on_stream_chunk` | Observe + Small Mutate | 流式 chunk 观测、轻量内容修饰、trace 打点 | 不允许 |
| `after_response` | PostProcess | 响应收尾、附加 metadata、后续信号生成 | 不允许 |

### 2.4 关键原则

- 所有 Hook 必须通过 typed input / typed result 与 `AgentLoop` 通信。
- Hook 不能直接改写 `AgentLoop` 私有状态。
- Hook 不能直接写会话历史、不能直接发起 retry、不能直接切换 `AgentState`。
- 只有 `before_agent_run` 和 `before_tool_execute` 允许返回阻断类决策。
- `after_tool_execute`、`on_stream_chunk`、`after_response` 只能补充、记录、轻量修饰，不能改变核心成功/失败语义。

---

## 三、现状到目标的映射

### 3.1 当前旧 Hook 点

当前系统定义的旧 Hook 点包括：

- `agent.beforeIntentParse`
- `agent.afterIntentParse`
- `agent.beforeContextBuild`
- `agent.beforeLLMCall`
- `agent.onStreamChunk`
- `agent.afterResponse`
- `agent.onToolCall`
- `agent.afterToolResult`
- `agent.beforeToolResultsPersist`
- `memory.beforeStore`
- `memory.afterRecall`
- `session.onFork`
- `session.onEnd`

### 3.2 迁移映射

本次重构的映射关系如下：

| 旧 Hook | 新去向 |
| --- | --- |
| `agent.beforeIntentParse` | 删除，不保留 |
| `agent.afterIntentParse` | 删除，不保留 |
| `agent.beforeContextBuild` | 暂不保留；上下文压缩延后，以后如需恢复，优先挂到 `before_agent_run` 或新扩展接口，不进入本轮最小集合 |
| `agent.beforeLLMCall` | 合并进 `before_agent_run` |
| `agent.onStreamChunk` | 迁移为 `on_stream_chunk` |
| `agent.afterResponse` | 迁移为 `after_response` |
| `agent.onToolCall` | 迁移为 `before_tool_execute` |
| `agent.afterToolResult` | 迁移为 `after_tool_execute` |
| `agent.beforeToolResultsPersist` | 并入 `after_tool_execute` 后的 Core 后处理阶段；如仍需结果二次修正，由 `after_tool_execute` 返回展示层 patch 和 metadata，不再保留单独 Hook |
| `memory.beforeStore` | 删除，不保留 |
| `memory.afterRecall` | 删除，不保留 |
| `session.onFork` | 删除，不保留 |
| `session.onEnd` | 删除，不保留 |

### 3.3 对现有扩展的影响

| 扩展 | 影响 |
| --- | --- |
| `core-tool-guard` | 迁移到 `before_tool_execute` |
| `core-security-audit` | 继续以 Event consumer 为主，也可以补充接入 `after_tool_execute` |
| `core-fulltrace` | 迁移到 `before_agent_run`、`on_stream_chunk`、`after_tool_execute`、`after_response` |
| `core-tool-results-validator` | 迁移为 Core 内部结果校验逻辑，或改造为 `after_tool_execute` 的补充 handler；不再依赖单独 `beforeToolResultsPersist` |
| `core-observability` | 以 Event consumer 为主，必要时在 `before_agent_run` / `after_response` 做轻量补充 |
| `builtin-context-compressor` | 暂时冻结，不在本轮最小运行时内默认接入 |
| `core-knowledge-extraction` | 暂时冻结，不在本轮默认接入 |
| `core-review` | 暂不依赖新 Hook，继续走独立事件链或冻结 |

---

## 四、Hook 契约模型设计

### 4.1 设计要求

新 Hook 契约模型必须满足：

- 输入和输出结构明确，便于测试和静态分析。
- 可支持多 handler 链式执行和合并。
- 允许记录 warning、audit record、annotations。
- 阻断类决策只在指定 Hook 中可用。
- 结果模型不能反向承载过多业务控制语义。

### 4.2 公共枚举与公共模型

建议在 `learning_agent/ai/models.py` 中新增或集中定义以下模型。

```python
class HookDecision(str, Enum):
    CONTINUE = "continue"
    ASK = "ask"
    DENY = "deny"


class HookWarning(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class HookAuditRecord(BaseModel):
    category: str
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)


class HookContext(BaseModel):
    session_id: str
    trace_id: Optional[str] = None
    turn_id: Optional[int] = None
    agent_state: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
```

### 4.3 `before_agent_run` 输入输出

```python
class BeforeAgentRunInput(BaseModel):
    user_input: str
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    provider_summary: dict[str, Any] = Field(default_factory=dict)
    tools_summary: list[dict[str, Any]] = Field(default_factory=list)
    context: HookContext


class BeforeAgentRunResult(BaseModel):
    decision: HookDecision = HookDecision.CONTINUE
    ask_message: Optional[str] = None
    deny_reason: Optional[str] = None
    runtime_patch: dict[str, Any] = Field(default_factory=dict)
    warnings: list[HookWarning] = Field(default_factory=list)
    audit_records: list[HookAuditRecord] = Field(default_factory=list)
```

#### 允许行为

- 检查 provider/tool/config 是否满足本轮运行条件
- 注入轻量运行时 metadata
- 返回 ask/deny 决策
- 生成启动审计记录

#### 禁止行为

- 长耗时 I/O
- 修改 session history
- 修改 failure tracker
- 修改 `AgentState`
- 初始化重型资源

### 4.4 `before_tool_execute` 输入输出

```python
class BeforeToolExecuteInput(BaseModel):
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    tool_schema: dict[str, Any] = Field(default_factory=dict)
    context: HookContext


class BeforeToolExecuteResult(BaseModel):
    decision: HookDecision = HookDecision.CONTINUE
    ask_message: Optional[str] = None
    deny_reason: Optional[str] = None
    patched_arguments: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)
    warnings: list[HookWarning] = Field(default_factory=list)
    audit_records: list[HookAuditRecord] = Field(default_factory=list)
```

#### 允许行为

- 权限判断
- 安全检查
- 策略级参数补丁
- 风险标签注入
- 要求用户确认

#### 禁止行为

- 执行工具
- 执行协议级 schema 校验
- 修改 retry / ban 逻辑
- 直接写会话工具结果

### 4.5 `after_tool_execute` 输入输出

```python
class AfterToolExecuteInput(BaseModel):
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    success: bool
    result: Any = None
    error: Optional[str] = None
    duration_ms: int = 0
    retry_count: int = 0
    annotations: dict[str, Any] = Field(default_factory=dict)
    context: HookContext


class AfterToolExecuteResult(BaseModel):
    display_result_override: Optional[str] = None
    extra_metadata: dict[str, Any] = Field(default_factory=dict)
    warnings: list[HookWarning] = Field(default_factory=list)
    audit_records: list[HookAuditRecord] = Field(default_factory=list)
```

#### 允许行为

- 生成审计记录
- 生成 UI 友好的结果文本
- 结果脱敏
- 注入 trace tags / metadata

#### 禁止行为

- 修改 `success`
- 修改核心错误分类
- 决定是否重试
- 决定是否 ban

### 4.6 `on_stream_chunk` 输入输出

```python
class OnStreamChunkInput(BaseModel):
    chunk_index: int
    content: str = ""
    reasoning_content: str = ""
    finish_reason: Optional[str] = None
    context: HookContext


class OnStreamChunkResult(BaseModel):
    content_override: Optional[str] = None
    reasoning_content_override: Optional[str] = None
    stream_tags: dict[str, Any] = Field(default_factory=dict)
    warnings: list[HookWarning] = Field(default_factory=list)
```

#### 允许行为

- 轻量文本修饰
- 脱敏
- 首包、尾包标记
- stream metrics 标签注入

#### 禁止行为

- ask/deny
- 触发工具执行逻辑
- 写会话历史
- 做重 I/O

### 4.7 `after_response` 输入输出

```python
class AfterResponseInput(BaseModel):
    response_text: str
    tool_calls_present: bool = False
    response_metadata: dict[str, Any] = Field(default_factory=dict)
    context: HookContext


class AfterResponseResult(BaseModel):
    response_override: Optional[str] = None
    extra_metadata: dict[str, Any] = Field(default_factory=dict)
    followup_signals: list[str] = Field(default_factory=list)
    warnings: list[HookWarning] = Field(default_factory=list)
    audit_records: list[HookAuditRecord] = Field(default_factory=list)
```

#### 允许行为

- 收尾日志
- 最终响应轻量修饰
- 生成后续信号
- 注入消息 metadata

#### 禁止行为

- 改动工具执行结果
- 改动当前轮状态机
- 直接落记忆写入主逻辑

---

## 五、HookSystem 重构设计

### 5.1 重构目标

`learning_agent/agent/hook_system.py` 需要从“泛型 Hook 执行器”重构为“typed middleware dispatcher”。

当前问题：

- 统一 `HookResult` 难以表达不同 Hook 的合法结果范围。
- `HookAbortError` + `abort` + `ask` 共存，阶段职责不清晰。
- 多 handler 的合并规则不显式。

### 5.2 重构后接口

建议新的 `HookSystem` 保留以下核心接口：

```python
class HookName(str, Enum):
    BEFORE_AGENT_RUN = "before_agent_run"
    BEFORE_TOOL_EXECUTE = "before_tool_execute"
    AFTER_TOOL_EXECUTE = "after_tool_execute"
    ON_STREAM_CHUNK = "on_stream_chunk"
    AFTER_RESPONSE = "after_response"


class HookSystem:
    def register(
        self,
        hook_name: HookName | str,
        handler: HookHandler,
        priority: int = 0,
        extension_id: Optional[str] = None,
    ) -> None:
        ...

    def unregister(
        self,
        hook_name: HookName | str,
        handler: HookHandler,
    ) -> None:
        ...

    async def run_before_agent_run(
        self,
        hook_input: BeforeAgentRunInput,
        trace_span: Optional[TraceSpan] = None,
    ) -> BeforeAgentRunResult:
        ...

    async def run_before_tool_execute(
        self,
        hook_input: BeforeToolExecuteInput,
        trace_span: Optional[TraceSpan] = None,
    ) -> BeforeToolExecuteResult:
        ...

    async def run_after_tool_execute(
        self,
        hook_input: AfterToolExecuteInput,
        trace_span: Optional[TraceSpan] = None,
    ) -> AfterToolExecuteResult:
        ...

    async def run_on_stream_chunk(
        self,
        hook_input: OnStreamChunkInput,
        trace_span: Optional[TraceSpan] = None,
    ) -> OnStreamChunkResult:
        ...

    async def run_after_response(
        self,
        hook_input: AfterResponseInput,
        trace_span: Optional[TraceSpan] = None,
    ) -> AfterResponseResult:
        ...
```

### 5.3 不再保留的机制

以下旧机制建议移除：

- 旧 `HookPoint` 枚举
- 旧 `HookResult`
- `HookAbortError`
- 统一的 `execute(point, data, context)` 接口

### 5.4 多 handler 合并规则

不同 Hook 的合并规则必须固定。

#### `before_agent_run` / `before_tool_execute`

- `decision`：取最严格优先级
  - `DENY > ASK > CONTINUE`
- `ask_message`：
  - 若最终决策为 `ASK`，取第一个返回 ask 的 handler message
- `deny_reason`：
  - 若最终决策为 `DENY`，取第一个返回 deny 的 handler reason
- `runtime_patch` / `patched_arguments`：
  - 按 handler 执行顺序逐步 merge，后写覆盖前写
- `warnings`：
  - 全量 append
- `audit_records`：
  - 全量 append
- `annotations`：
  - 后写覆盖前写

#### `after_tool_execute`

- `display_result_override`：
  - 取最后一个非空值
- `extra_metadata`：
  - merge，后写覆盖前写
- `warnings`：
  - 全量 append
- `audit_records`：
  - 全量 append

#### `on_stream_chunk`

- `content_override`：
  - 逐 handler 应用，后一个在前一个 override 基础上继续处理
- `reasoning_content_override`：
  - 同上
- `stream_tags`：
  - merge，后写覆盖前写
- `warnings`：
  - 全量 append

#### `after_response`

- `response_override`：
  - 取最后一个非空值
- `extra_metadata`：
  - merge，后写覆盖前写
- `followup_signals`：
  - 追加后去重
- `warnings`：
  - 全量 append
- `audit_records`：
  - 全量 append

### 5.5 Hook 异常策略

默认策略：

- Hook handler 抛异常时，不中断 Core 主流程。
- `HookSystem` 记录日志和 trace tag。
- 仅对当前 handler 视为失败，继续执行后续 handler。

例外：

- 本轮不再支持通过抛异常来表达 deny/ask。
- deny/ask 必须通过标准结果模型返回。

---

## 六、AgentLoop 集成设计

### 6.1 集成原则

- `AgentLoop` 是主裁决者。
- Hook 结果只作为输入给 `AgentLoop` 做后续判断。
- 所有状态变更、重试、降级、session append、event publish 仍由 `AgentLoop` 控制。

### 6.2 `before_agent_run` 放置点

触发时机：

- 在 `run_turn()` 开始、但尚未进入正式 ReACT 循环前。
- 在 `self.sessions.append_message(session.id, MessageRole.USER, user_input)` 之前触发。

建议顺序：

1. 生成 `HookContext`
2. 构造 `BeforeAgentRunInput`
3. 执行 `hooks.run_before_agent_run()`
4. 根据结果处理：
   - `DENY`：输出拒绝消息，记录 event，结束本轮
   - `ASK`：进入 ask 流程或直接返回用户确认提示
   - `CONTINUE`：应用 `runtime_patch` 后继续

### 6.3 `on_stream_chunk` 放置点

触发时机：

- 在 provider 流式返回每个 `ChatChunk` 时。
- 在 chunk 累积进 `full_content` 之前。

建议顺序：

1. 构造 `OnStreamChunkInput`
2. 执行 `hooks.run_on_stream_chunk()`
3. 如果存在 `content_override` / `reasoning_content_override`，更新 chunk 副本
4. 用更新后的 chunk：
   - 累积 `full_content`
   - 发布 `agent.responseChunk`
   - `yield` 给 Web

### 6.4 `after_response` 放置点

触发时机：

- assistant 文本输出完成、tool calls 已解析、assistant entry 已 append 后
- 在处理工具调用之前执行

建议顺序：

1. 构造 `AfterResponseInput`
2. 执行 `hooks.run_after_response()`
3. 如有 `response_override`，只影响消息展示文本和后续写入 metadata，不回写原始 LLM chunk 历史
4. 将 `extra_metadata` 合并进 assistant entry metadata
5. 将 `followup_signals` 作为后续 event 或内部 signal 使用

### 6.5 `before_tool_execute` 放置点

触发时机：

- 在工具存在性检查、协议级参数校验之后
- 在真正调用 `ToolRegistry.execute()` 之前

建议顺序：

1. Core 先执行工具存在性检查
2. Core 先执行 input_model / schema 校验
3. 通过后构造 `BeforeToolExecuteInput`
4. 执行 `hooks.run_before_tool_execute()`
5. 根据结果处理：
   - `DENY`：记录 deny event，生成 blocked result，不执行工具
   - `ASK`：记录 ask event，生成 approval result，不执行工具
   - `CONTINUE`：应用参数 patch 后执行工具

### 6.6 `after_tool_execute` 放置点

触发时机：

- 工具执行结束后
- 在 Core 已得到最终 `success / error / retry_count / duration_ms` 之后
- 在写 session tool result 之前

建议顺序：

1. Core 先完成执行、重试、成功失败判断
2. 构造 `AfterToolExecuteInput`
3. 执行 `hooks.run_after_tool_execute()`
4. 得到 `display_result_override`、`extra_metadata`
5. 再由 Core 将结果写入 `SessionEntry.tool_results`

### 6.7 对工具执行流水线的影响

现有流水线：

1. emit tool_execution_start
2. is_banned
3. input validation
4. on_tool_call
5. ToolRegistry.execute
6. after_tool_result
7. emit tool_execution_end
8. persist result

重构后流水线：

1. emit tool_execution_start
2. is_banned
3. Core input validation
4. `before_tool_execute`
5. ToolRegistry.execute with retry
6. Core success/failure classification
7. `after_tool_execute`
8. emit tool_execution_end
9. persist result

---

## 七、Core 与 Hook 的边界

### 7.1 必须留在 Core 的能力

- 工具存在性检查
- `input_model` / schema 校验
- retry 判定
- failure tracker 计数
- banned 判定
- chat-only recovery
- session append / persistence
- event publish
- state transition

### 7.2 允许由 Hook 承担的能力

- 权限策略判断
- 安全检查
- 结果审计
- 日志增强
- 轻量 metadata 注入
- 文案轻量修饰
- trace tag 补充

### 7.3 明确禁止 Hook 做的事

- 直接修改 `_failure_tracker`
- 直接修改 `_chat_only_mode`
- 直接修改 `_session_runtimes`
- 直接写入 session messages
- 直接决定 retry
- 直接发起工具执行
- 直接变更 `AgentState`

---

## 八、Event 配合策略

### 8.1 本轮 Event 原则

本轮不做 Event 全量重构，但需要保证 Hook 重构不会破坏关键事件链。

保留并继续使用的关键事件：

- `agent.llmCalled`
- `agent.responseChunk`
- `agent.streamInterrupted`
- `agent.toolCalled`
- `agent.toolResult`
- `agent.toolRetry`
- `agent.toolValidationFailed`
- `agent.toolPermissionAsk`
- `agent.toolPermissionDenied`
- `agent.toolBanned`
- `agent.unhandledError`
- `agent.traceCompleted`
- `agent.stateSnapshot`

### 8.2 Hook 与 Event 的协作方式

- Hook 不直接发布业务事件。
- Hook 只返回 `audit_records`、`warnings`、`followup_signals`。
- 是否将这些内容转成 Event，由 `AgentLoop` 或专门的 observability adapter 决定。

### 8.3 建议新增的内部转换点

如需增强可观测，可在 Core 内增加以下内部处理：

- 将 `HookWarning` 统一映射为 trace tag 或 warn log
- 将 `HookAuditRecord` 统一推送给审计 sink
- 将 `followup_signals` 统一转为内部 event 或后台任务调度信号

---

## 九、Extension 迁移策略

### 9.1 第一批必须迁移的扩展

#### `core-tool-guard`

- 旧接线：`agent.onToolCall`
- 新接线：`before_tool_execute`
- 输出：
  - `decision=CONTINUE / ASK / DENY`
  - `annotations={"risk_level": "...", "policy_rule_id": "..."}`

#### `core-fulltrace`

- 新接线：
  - `before_agent_run`
  - `on_stream_chunk`
  - `after_tool_execute`
  - `after_response`

#### `core-security-audit`

- 继续保留 Event consumer
- 可选在 `after_tool_execute` 增加 hook handler，用于生成 `HookAuditRecord`

### 9.2 第二批延后迁移的扩展

- `builtin-context-compressor`
- `core-knowledge-extraction`
- `core-review`
- `core-material-text`

默认策略：

- 先从默认关键链路中弱化这些扩展
- 等 Core Hook 稳定后再重新设计接入位置

---

## 十、文件级改造清单

### 10.1 `learning_agent/ai/models.py`

新增：

- `HookName`
- `HookDecision`
- `HookWarning`
- `HookAuditRecord`
- `HookContext`
- 五组 Hook input/result 模型

删除或废弃：

- `HookPoint`
- `HookResult`

### 10.2 `learning_agent/agent/hook_system.py`

重构内容：

- 新增基于 `HookName` 的注册表
- 删除旧 `execute(point, data, context)` 接口
- 新增五个 typed run 方法
- 删除 `HookAbortError`
- 明确多 handler merge 逻辑

### 10.3 `learning_agent/learning_agent/extension_manager.py`

调整内容：

- `ExtensionContext.register_hook()` 改为接收 `HookName`
- 扩展注册时不再传旧 `HookPoint`
- 如有必要，可为兼容期提供短期 mapping，但不建议长期保留

### 10.4 `learning_agent/agent/agent_loop.py`

改造内容：

- 在 `run_turn()` 入口新增 `before_agent_run`
- 用 `on_stream_chunk` 替换旧 `ON_STREAM_CHUNK`
- 用 `after_response` 替换旧 `AFTER_RESPONSE`
- 在 `_execute_tool_calls()` 中：
  - 删除旧 `ON_TOOL_CALL`
  - 删除旧 `AFTER_TOOL_RESULT`
  - 删除旧 `BEFORE_TOOL_RESULTS_PERSIST`
  - 接入 `before_tool_execute`
  - 接入 `after_tool_execute`

### 10.5 `learning_agent/extensions/tool_guard.py`

改造内容：

- 迁移到 `before_tool_execute`
- 统一返回 `BeforeToolExecuteResult`

### 10.6 `learning_agent/learning_agent/extensions/built_in.py`

改造内容：

- `core-fulltrace` 改挂新 Hook
- `core-observability` 如保留 hook handler，只挂允许的轻量切点
- `core-tool-results-validator` 从旧 Hook 中迁出，必要时转为 Core 或 `after_tool_execute` handler

### 10.7 `learning_agent/extensions/security_audit.py`

改造内容：

- 默认保留事件消费者实现
- 可选增加 `after_tool_execute` handler

---

## 十一、分阶段实施步骤

### 阶段 1：建模与基础设施

1. 在 `models.py` 新增新 Hook 契约模型
2. 在 `hook_system.py` 实现新的 typed dispatcher
3. 保留旧实现分支不删，先完成新系统单测

### 阶段 2：接入工具链路

1. 在 `_execute_tool_calls()` 中接入 `before_tool_execute`
2. 接入 `after_tool_execute`
3. 迁移 `tool_guard`
4. 迁移相关测试

### 阶段 3：接入流式链路

1. 在 `run_turn()` 中接入 `on_stream_chunk`
2. 在 `run_turn()` 开始处接入 `before_agent_run`
3. 在 assistant 响应完成后接入 `after_response`

### 阶段 4：删除旧 Hook 系统

1. 删除旧 `HookPoint`
2. 删除旧 `HookResult`
3. 删除旧 handler 注册代码
4. 删除旧兼容桥接

### 阶段 5：清理扩展与文档

1. 更新默认激活扩展清单
2. 补充接线审计文档
3. 更新相关设计文档和测试结论

---

## 十二、测试设计

### 12.1 `hook_system.py` 单元测试

必须覆盖：

- 多 handler 按 priority 顺序执行
- `before_tool_execute` 的 `DENY > ASK > CONTINUE` 合并规则
- `patched_arguments` merge 规则
- `warnings` / `audit_records` 聚合
- handler 抛异常时不中断后续 handler
- `on_stream_chunk` content override 链式覆盖

### 12.2 `AgentLoop` 集成测试

必须覆盖：

- `before_agent_run` 返回 `DENY`
- `before_agent_run` 返回 `ASK`
- `before_tool_execute` 返回 `DENY`
- `before_tool_execute` 返回 `ASK`
- `before_tool_execute` patch arguments 后工具执行成功
- `after_tool_execute` 修改展示文本
- `on_stream_chunk` 覆盖 chunk 内容并正常流式输出
- `after_response` 写入 extra metadata

### 12.3 回归测试

必须保证以下现有能力不回退：

- 工具 retry
- tool banned
- validation failed
- hook deny 不计入错误分类混乱
- chat-only recovery
- Web 流式输出
- snapshot persistence

---

## 十三、验收标准

本轮 Hook 重构完成后，应满足以下验收标准：

1. 运行时只保留 5 个 Hook。
2. `AgentLoop` 不再依赖旧 `HookPoint` 和旧 `HookResult`。
3. 工具执行前后的权限、安全、审计都可通过新 Hook 正常接入。
4. Web 流式输出链路不依赖 Event 驱动主渲染，`on_stream_chunk` 不影响流式稳定性。
5. Hook handler 报错不会中断 Core 主流程。
6. 关键路径具备对应测试。
7. 上层扩展能力未接回时，核心运行链仍可独立稳定运行。

---

## 十四、最终结论

本方案的本质不是“把 Hook 做得更灵活”，而是：

- 把 Hook 收敛成少数稳定切点
- 把 Core 与 Hook 的边界重新画清楚
- 让后续代码实现以 typed 契约为基础，而不是继续依赖泛型 `dict`

按本方案实施后：

- `AgentLoop` 仍然掌握核心执行权
- Hook 系统具备清晰、可控、可测的前后置能力
- Extension 可以继续存在，但不再承载核心正确性
- 后续再接记忆、压缩、知识提取时，也有稳定的最小运行时底座可依赖
