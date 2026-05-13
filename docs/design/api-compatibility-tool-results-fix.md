# API 兼容性与 Tool Results 完整性修复 —— 技术实现文档

> 版本：v3.0
> 日期：2026-05-11
> 依赖文档：`docs/design/resilience-and-validation-design.md`、`docs/design/tool-execution-reliability-fix.md`
> 依赖原则：`AGENTS.md` 分层自治与自愈原则

---

## 一、概述

### 1.1 背景

在联调 OpenAI / Moonshot API 时发现两个导致 `400 Invalid request` 的兼容性问题：

1. **`reasoning_content` 缺失**：当模型开启 thinking 模式时，包含 `tool_calls` 的 assistant message 必须显式携带 `reasoning_content` 字段（即使是空字符串）。
2. **`tool_call_id` 不匹配**：流式传输中 API 可能在早期 chunk 不发送 `tool_call.id`，导致 assistant message 的 `call_id` 与后续 tool result message 的 `tool_call_id` 不一致。

此外，Agent Loop 在异常退出路径中（如 LLM 流中断）可能产生**孤儿 tool calls**（assistant message 包含 tool_calls，但无对应 tool result），违反 OpenAI API 协议。

### 1.2 设计目标

| 问题 | 修复策略 | 职责归属 |
|------|---------|---------|
| `reasoning_content` 缺失 | 上下文构建时兜底填充空字符串，Provider 序列化时保留字段 | AgentLoop + Provider |
| `tool_call_id` 不匹配 | 解析 tool calls 时统一兜底生成稳定 `call_id`，保存 assistant entry 时回写 buffer | AgentLoop |
| 孤儿 tool calls（流中断）| 补偿 synthetic tool results → **直接调用收尾 LLM**（纯对话模式），不进入重试循环 | AgentLoop（核心层内部解决）|
| Tool results 一致性检测 | 新增 `BEFORE_TOOL_RESULTS_PERSIST` Hook，由扩展负责检测、排序、修正 | 扩展（`core-tool-results-validator`）|

### 1.3 流中断兜底流程

遵循 `AGENTS.md` 的**自愈与持续运行原则**。流中断属于**通信层不可恢复故障**，Agent 的职责是**优雅收尾**，而非重试恢复：

```
LLM 流中断（异常抛出）
    │
    ├──► 1. 补偿孤儿 tool calls（synthetic results）
    │         └──► 写入 session，保证历史记录协议自洽
    │
    ├──► 2. 直接调用收尾 LLM（纯对话模式，tools=None）
    │         └──► 构建上下文（包含 synthetic results）
    │         └──► 追加 system message 告知 LLM 当前状态
    │         └──► yield 流式输出
    │         └──► 保存 assistant message 到 session
    │
    └──► 3. 所有路径记录日志 + 发射观测事件
            └──► agent.streamInterrupted
```

**关键原则**：流中断后不进入 ReACT 重试循环。理由：
- synthetic result 已明确告知 LLM"工具因流中断未执行"
- LLM 基于该上下文可直接给出最佳回答（或建议用户重试）
- 重试循环不会改善结果，反而增加 token 成本和用户等待时间

---

## 二、数据模型变更

### 2.1 无结构变更的字段使用

- `ChatMessage.reasoning_content`（`models.py` 中已存在）：上下文构建时兜底为空字符串
- `SessionEntry.metadata["synthetic"]`：标记 synthetic tool result
- `SessionEntry.metadata["reason"]`：标记 synthetic 原因

### 2.2 新增 HookPoint

```python
class HookPoint(str, Enum):
    # ... 现有枚举值 ...
    BEFORE_TOOL_RESULTS_PERSIST = "agent.beforeToolResultsPersist"
```

**触发时机**：`_execute_tool_calls()` 返回后、保存 tool results 到 session 之前。

**入参契约**：

```python
class ToolResultsBatch:
    """BEFORE_TOOL_RESULTS_PERSIST Hook 的入参数据结构。"""
    tool_calls: list[ToolCall]           # 声明的工具调用列表（按索引有序）
    results: list[tuple[ToolCall, Any, bool]]  # 执行结果
```

**返回值**：`HookResult`
- `modified=True` + `data=ToolResultsBatch`：用扩展修正后的结果替换原始结果
- `modified=False`：结果保持不变
- `abort=True`：仅用于用户权限类场景，本 Hook 不预期触发 abort

---

## 三、组件设计

### 3.1 core-tool-results-validator 扩展（新增）

**职责**：在 tool results 持久化前做批量检测与修正，为后续并行执行工具做准备。

**位置**：`learning_agent/extensions/built_in.py` 中新增工厂函数。

**注册方式**：在 `create_builtin_extensions()` 中返回。

**Hook Handler 逻辑**：

```python
async def _hook_validate_tool_results(batch: ToolResultsBatch, context: dict) -> HookResult:
    """
    检测并修正 tool results 的一致性。
    
    检测项：
    1. 数量匹配：len(results) == len(tool_calls)
    2. ID 存在性：每个 result 对应的 tool_call_id 非空
    3. ID 一致性：每个 result 的 tool_call_id 都能在 tool_calls 中找到对应
    4. 顺序对齐（可选）：结果顺序与 tool_calls 声明顺序一致
    
    修正行为（按分层自治原则，自动修正不 abort）：
    - 缺失 result：为缺失的 tool call 生成 synthetic error result
    - 多余 result：按 tool_calls 长度截断
    - ID 缺失/不匹配：按索引强制对齐（result[i].tool_call_id = tool_calls[i].call_id）
    - 顺序错乱：按 tool_calls 的 call_id 重新排序 results
    """
```

**告警方式**：
- `logger.warning` 记录不一致事件
- 通过 `ExtensionContext.publish_event()` 发射 `agent.toolResultsValidated` 事件

### 3.2 AgentLoop._ensure_tool_results_for_orphans（新增私有方法）

**职责**：为未执行的 tool calls 生成 synthetic tool results，保证历史记录协议自洽。

**位置**：`learning_agent/agent/agent_loop.py`

**接口**：

```python
async def _ensure_tool_results_for_orphans(
    self,
    session: LearningSession,
    tool_call_buffers: dict[int, dict[str, Any]],
    reason: str,
) -> None:
    """
    为孤儿 tool calls 生成 synthetic tool results 并写入 session。
    
    Args:
        session: 当前会话
        tool_call_buffers: 流式累积的 tool call buffer（已回写 call_id）
        reason: 失败原因标识
    """
```

**Synthetic Result 文案**：

| reason | 文案 |
|--------|------|
| `llm_stream_interrupted` | `"[Tool Execution Interrupted] The tool call to '{tool_name}' was not executed because the language model stream was interrupted. No changes were made. Please retry this tool call or provide the answer directly."` |
| `agent_loop_error` | `"[Tool Execution Skipped] The tool call to '{tool_name}' could not be completed due to an unexpected agent error. Please retry or answer directly."` |

**Metadata 标记**：
```python
{
    "tool_id": tool_name,
    "tool_call_id": call_id,
    "is_error": True,
    "synthetic": True,
    "reason": reason,
}
```

### 3.3 AgentLoop._finalize_with_llm（新增私有方法）

**职责**：流中断后的**标准收尾路径**。调用一次纯对话模式的 LLM 给出收尾回答。

> 注意：不同于旧版 v2.0，本方法不再只是"恢复耗尽后"的兜底，而是**流中断后的唯一标准路径**。

**接口**：

```python
async def _finalize_with_llm(
    self,
    session: LearningSession,
    user_input: str,
    error_reason: str,
    parent_span: Optional[Any],
) -> AsyncIterable[ChatChunk]:
    """
    流中断后的收尾 LLM 调用。
    
    行为：
    1. 构建上下文（包含 synthetic results）
    2. 追加 system message 告知 LLM 当前状态
    3. 以纯对话模式（tools=None）调用 LLM
    4. yield 流式输出
    5. 保存 assistant message 到 session
    
    Returns:
        AsyncIterable[ChatChunk]：流式输出
    """
```

**System Message 模板**：
```
Note: The previous tool execution was interrupted due to: {error_reason}. 
Please provide the best possible response based on the information already available. 
If you cannot answer fully, explain the limitation clearly to the user.
```

---

## 四、AgentLoop 修改点详细说明

### 4.1 修改点 A：保存 assistant entry 时回写 call_id

位置：`agent_loop.py`，保存 assistant entry 的代码块。

```python
for idx in sorted(tool_call_buffers.keys()):
    tc_buf = tool_call_buffers[idx]
    fn = tc_buf.get("function", {})
    
    call_id = tc_buf.get("id") or f"call-{uuid.uuid4().hex[:8]}"
    tc_buf["id"] = call_id  # ← 回写，确保后续 synthetic result 使用相同 id
    
    assistant_entry.tool_calls.append(
        ToolCall(
            tool_id=fn.get("name", "unknown"),
            call_id=call_id,
            arguments=args,
        )
    )
```

### 4.2 修改点 B：ReACT 循环内的流中断兜底逻辑（核心）

位置：`agent_loop.py` 的 `run()` 方法，ReACT `while` 循环内部。

```python
while has_tool_calls and turn_count < self.max_react_turns:
    turn_count += 1
    has_tool_calls = False
    # ... 正常流程 ...
    
    llm_error_occurred = False
    try:
        async for chunk in self._stream_chat_with_retry(session, params):
            # ... 累积 tool_call_buffers ...
            yield chunk
    except Exception as e:
        logger.exception(f"[AgentLoop] LLM stream error: {e}")
        yield ChatChunk(content="\n[System] LLM response stream interrupted. Initiating recovery...\n")
        error_message = str(e)
        llm_error_occurred = True
    finally:
        if self.obs:
            self.obs.end_span(llm_span)
    
    # 【核心改动】流中断兜底：无重试循环，直接收尾
    if llm_error_occurred:
        # 1. 补偿孤儿 tool calls
        if tool_call_buffers:
            await self._ensure_tool_results_for_orphans(
                session, tool_call_buffers, reason="llm_stream_interrupted"
            )
        
        # 2. 直接调用收尾 LLM（无重试循环）
        logger.warning(f"[AgentLoop] LLM stream interrupted, finalizing with chat-only mode")
        yield ChatChunk(
            content="\n[Notice] Tool execution was interrupted. "
                    "Generating a response based on available information...\n"
        )
        
        # 保存当前 turn 状态
        await self._emit_agent_event(AgentEventType.TURN_END, {...})
        await self._save_state_snapshot(session, turn_count, [], error_message)
        
        # 调用收尾 LLM
        async for chunk in self._finalize_with_llm(session, user_input, error_message, root_span):
            yield chunk
        
        # 观测事件
        await self.events.publish(Event(
            type="agent.streamInterrupted",
            payload={"reason": error_message, "finalized": True},
            source="agent_loop",
            session_id=session.id,
        ))
        break  # ← 结束 ReACT 循环
    
    # ... 保存 assistant entry ...
    # ... 执行 tool calls ...
    # ... 保存 tool results ...
```

**关键变化（对比 v2.0）**：
- 删除了 `recovery_attempts` 计数器和恢复循环
- 删除了 `has_tool_calls = True` 的恢复分支
- 流中断后直接走 `_finalize_with_llm()`，然后 `break`

### 4.3 修改点 C：run() 外层异常处理

位置：`agent_loop.py` 的 `run()` 方法，最外层 `try-except`。

```python
try:
    # ... ReACT 循环 ...
except Exception as e:
    # 1. 补偿孤儿 tool calls（保证历史完整）
    if tool_call_buffers:
        await self._ensure_tool_results_for_orphans(
            session, tool_call_buffers, reason="agent_loop_error"
        )
    
    # 2. 通知用户
    yield ChatChunk(
        content="\n[Notice] An unexpected error occurred. "
                "Attempting to generate a final response...\n"
    )
    
    # 3. 尝试收尾 LLM
    try:
        final_messages = await self._build_context_for_turn(session, user_input)
        final_messages.append(ChatMessage(
            role=MessageRole.SYSTEM,
            content=f"Note: An unexpected error occurred. "
                    f"Please provide the best response possible based on available context.",
        ))
        final_params = ChatParams(
            model=self.provider.default_model or "gpt-4o",
            messages=final_messages,
            tools=None,
        )
        async for chunk in self.provider.stream_chat(final_params):
            yield chunk
    except Exception as final_e:
        yield ChatChunk(content="\n[Error] Unable to continue. Please try again later.\n")
    
    # 4. 错误状态 + 观测
    self._set_state(AgentState.ERROR, session.id)
    logger.exception(f"[AgentLoop] Unhandled error: {e}")
    
    await self.events.publish(Event(
        type="agent.unhandledError",
        payload={"error": str(e), "session_id": session.id},
        source="agent_loop",
        session_id=session.id,
    ))
    
    if self.obs:
        self.obs.end_span(root_span)
        self.obs.end_trace()
```

### 4.4 修改点 D：_execute_tool_calls 返回后触发 Hook

位置：`agent_loop.py`，tool results 保存前。

```python
if tool_call_buffers:
    has_tool_calls = True
    tool_calls = self._parse_tool_calls(tool_call_buffers)
    tool_results = await self._execute_tool_calls(session, tool_calls, turn_count, root_span)
    
    # 触发 BEFORE_TOOL_RESULTS_PERSIST Hook
    batch = ToolResultsBatch(tool_calls=tool_calls, results=tool_results)
    hook_result = await self.hooks.execute(
        HookPoint.BEFORE_TOOL_RESULTS_PERSIST,
        batch,
        {"session": session, "memory": self.memory},
    )
    if hook_result.modified and isinstance(hook_result.data, ToolResultsBatch):
        tool_results = hook_result.data.results
    
    # 保存 tool results...
```

### 4.5 修改点 E：_build_context_for_turn 修复

**E1：reasoning_content 兜底**

```python
elif entry.tool_calls:
    messages.append(ChatMessage(
        role=entry.role,
        content=entry.content,
        tool_calls=[...],
        reasoning_content=entry.metadata.get("reasoning_content") or "",
    ))
```

**E2：tool_call_id 一致性**

```python
if entry.role == MessageRole.TOOL:
    tool_call_id = entry.metadata.get("tool_call_id", "")
    messages.append(ChatMessage(
        role=MessageRole.TOOL,
        content=entry.content,
        tool_call_id=tool_call_id,
    ))
```

---

## 五、调用流程时序图

### 5.1 正常路径

```
User Input
    │
    ▼
AgentLoop.run() — ReACT Turn 1
    │
    ├──► Provider.stream_chat() ──► LLM
    │         │
    │         └──► AssistantMessage（含 tool_calls）保存到历史
    │                    └──► call_id 回写到 tool_call_buffers
    │
    ▼
_parse_tool_calls() → call_id 兜底生成
    │
    ▼
_execute_tool_calls() → 8 步流水线
    │
    ▼
Hook BEFORE_TOOL_RESULTS_PERSIST
    │
    ▼
保存 Tool Results → has_tool_calls = True
    │
    ▼
ReACT Turn 2（下一轮 LLM 携带 tool_result）
```

### 5.2 流中断 + 直接收尾路径（方案 C 主路径）

```
ReACT Turn N
    │
    ├──► Provider.stream_chat()
    │         │
    │         └──► X 流中断（异常抛出）
    │
    ▼
except Exception:
    llm_error_occurred = True
    yield "[System] LLM response stream interrupted..."
    │
    ▼
【兜底逻辑】
    │
    ├──► _ensure_tool_results_for_orphans()
    │         └──► 写入 synthetic tool results 到 session
    │
    ├──► yield "[Notice] Tool execution was interrupted..."
    │
    ├──► _finalize_with_llm()
    │         ├──► 构建上下文 + system message
    │         ├──► 纯对话模式调用 LLM
    │         └──► yield 收尾回答
    │
    └──► break
    │
    ▼
AGENT_END
```

### 5.3 流中断 + 收尾 LLM 也失败路径（兜底路径）

```
ReACT Turn N
    │
    ├──► Provider.stream_chat() ──► X 流中断
    │
    ▼
【兜底逻辑】
    │
    ├──► _ensure_tool_results_for_orphans()
    │
    ├──► _finalize_with_llm()
    │         ├──► 构建上下文 + system message
    │         ├──► 纯对话模式调用 LLM ──► X 也失败
    │         └──► yield "[Error] Unable to continue..."
    │
    └──► break
    │
    ▼
AGENT_END（用户至少收到了中断通知）
```

### 5.4 外层异常路径

```
AgentLoop.run()
    │
    ├──► try: ReACT 循环 ...
    │
    └──► except Exception:
             │
             ├──► _ensure_tool_results_for_orphans()
             ├──► yield "[Notice] Unexpected error..."
             ├──► 尝试收尾 LLM
             ├──► _set_state(ERROR)
             └──► 发射 agent.unhandledError 事件
```

---

## 六、边界条件与异常处理

### 6.1 无限循环防护

**机制**：
- `max_react_turns`：硬上限，任何情况下 turn 数不超过此值
- 流中断后直接 `break` 结束 ReACT 循环，不再进入下一轮

**极端场景**：
- 假设 `max_react_turns = 10`
- Turn 5 流中断 → 补偿孤儿 tool → `_finalize_with_llm` → `break`
- 总 turn 数 = 5，不会超过上限

### 6.2 tool_call_buffers 为空

**场景**：LLM 流中断发生在输出 tool_calls 之前（普通对话中断）。

**处理**：`_ensure_tool_results_for_orphans()` 检查 `if not tool_call_buffers: return`，不产生 synthetic result。`_finalize_with_llm()` 仍会调用，让 LLM 基于已有上下文收尾回答。

### 6.3 收尾 LLM 也失败

**场景**：流中断后，`_finalize_with_llm()` 的 Provider 调用也抛出异常。

**处理**：
- yield 最终的错误 chunk：`"[Error] Unable to continue. Please try again later."`
- 记录 `logger.exception`
- 发射 `agent.streamInterrupted` 事件，`finalized=False`
- 设置 `AgentState.ERROR`
- 用户至少收到了之前的 `[System]` 中断通知和 `[Notice]` 说明，不会面对空白

### 6.4 Hook 未注册或返回异常数据

**场景**：没有扩展注册 `BEFORE_TOOL_RESULTS_PERSIST`，或 Hook 返回了非 `ToolResultsBatch` 类型。

**处理**：
- 未注册：`HookSystem.execute()` 返回 `modified=False`，使用原始结果
- 类型不匹配：AgentLoop 做 `isinstance` 校验，不匹配则忽略并记录 warning
- 核心流程不受影响

### 6.5 Session 恢复后的孤儿检测

**场景**：从磁盘加载历史 Session，其中包含之前产生的 synthetic tool results。

**处理**：`_build_context_for_turn()` 正常读取，和普通 tool results 一样处理。`synthetic: True` 不影响上下文构建，仅用于可观测性。

---

## 七、EventBus 事件规范（新增/扩展）

| 事件类型 | 发射时机 | Payload |
|----------|---------|---------|
| `agent.streamInterrupted` | LLM 流中断后调用收尾 LLM 时 | `{"reason": str, "finalized": bool}` |
| `agent.toolResultsValidated` | `BEFORE_TOOL_RESULTS_PERSIST` Hook 修正后 | `{"tool_count": int, "issues": list[str], "corrected": bool}` |
| `agent.orphanToolCallsCompensated` | `_ensure_tool_results_for_orphans()` 执行后 | `{"count": int, "reason": str, "call_ids": list[str]}` |
| `agent.unhandledError` | `run()` 外层 `except` 捕获到异常时 | `{"error": str, "session_id": str}` |

> 注：v3.0 删除 v2.0 中的 `agent.recoveryAttempt` 和 `agent.recoveryExhausted` 事件。

---

## 八、实施检查清单

### Phase 1：数据模型

- [ ] `models.py`：`HookPoint` 枚举新增 `BEFORE_TOOL_RESULTS_PERSIST`
- [ ] `models.py`：新增 `ToolResultsBatch` 数据类

### Phase 2：AgentLoop 核心修改

- [ ] `agent_loop.py`：保存 assistant entry 时回写 `call_id` 到 `tool_call_buffers`
- [ ] `agent_loop.py`：新增 `_ensure_tool_results_for_orphans()` 方法
- [ ] `agent_loop.py`：新增 `_finalize_with_llm()` 方法
- [ ] `agent_loop.py`：ReACT 循环内 `llm_error_occurred` 处理改为**直接收尾**（删除 recovery 循环）
- [ ] `agent_loop.py`：`run()` 外层 `except` 中增加收尾 LLM 尝试
- [ ] `agent_loop.py`：`_build_context_for_turn()` 中 `reasoning_content` 兜底为空字符串
- [ ] `agent_loop.py`：`_build_context_for_turn()` 中 tool message 读取 `tool_call_id`
- [ ] `agent_loop.py`：`_parse_tool_calls()` 保留 `call_id` 兜底生成逻辑

### Phase 3：Hook 与扩展

- [ ] `built_in.py`：新增 `create_tool_results_validator_extension()` 工厂函数
- [ ] `built_in.py`：在 `create_builtin_extensions()` 中注册新扩展
- [ ] `agent_loop.py`：在 tool results 保存前触发 `BEFORE_TOOL_RESULTS_PERSIST` Hook

### Phase 4：Provider 修复

- [ ] `openai_provider.py`：`_convert_messages()` 中保留 `reasoning_content` 字段

### Phase 5：文档

- [ ] `AGENTS.md`：全局设计原则（长期/稳定/自主运行）
- [ ] `docs/design/api-compatibility-tool-results-fix.md`：本文档 v3.0
- [ ] `docs/design/tool-execution-reliability-fix.md`：工具执行可靠性修复文档

### Phase 6：验证

- [ ] 语法检查：`python3 -m py_compile` 通过
- [ ] 导入验证：`from learning_agent.agent.agent_loop import AgentLoop` 成功
- [ ] 场景 A：模拟 LLM 流中断，确认直接调用收尾 LLM，无重试循环
- [ ] 场景 B：模拟收尾 LLM 也失败，确认用户收到友好错误通知
- [ ] 场景 C：模拟外层异常，确认收尾 LLM 和错误通知同时生效
- [ ] ID 一致性：确认 assistant entry 的 `call_id` 与 tool result entry 的 `tool_call_id` 严格一致

---

## 九、已确认决策汇总

| # | 决策项 | 结论 |
|---|--------|------|
| 1 | `reasoning_content` 缺失修复 | 上下文构建时兜底空字符串，Provider 序列化保留字段 |
| 2 | `tool_call_id` 不匹配修复 | 解析时兜底生成 UUID，保存 assistant entry 时回写 buffer |
| 3 | 孤儿 tool calls 修复 | **流中断后直接收尾 LLM**，不进入重试循环 |
| 4 | 流中断后行为 | 补偿 synthetic results → `_finalize_with_llm()`（纯对话模式）→ `break` |
| 5 | 无 recovery_attempts | 流中断属于通信层不可恢复故障，重试不会改善结果 |
| 6 | 收尾 LLM 失败 | yield 最终错误 chunk，记录日志，发射 `agent.streamInterrupted`（finalized=False） |
| 7 | Tool results 一致性检测 | 新增 `BEFORE_TOOL_RESULTS_PERSIST` Hook，由 `core-tool-results-validator` 扩展实现 |
| 8 | 扩展自动修正边界 | 检测到 ID 缺失/不匹配时按索引强制对齐；缺失 result 时补 synthetic error result |
| 9 | 不随意 abort | Hook 检测到问题只修正+告警，不 abort 核心流程 |
| 10 | 用户通知 | 流中断时 yield `[System]`；兜底时 yield `[Notice]`；外层异常时 yield `[Notice]` |
| 11 | 外层异常兜底 | 即使 `run()` 外层 `except`，也补偿孤儿 + 尝试收尾 LLM + 告知用户 |
| 12 | Hook 返回值校验 | AgentLoop 核心对 Hook 返回的 data 做 `isinstance` 校验，类型不匹配则忽略 |
| 13 | 工具执行失败处理 | 由独立文档 `tool-execution-reliability-fix.md` 覆盖（重试、ban 策略、降级恢复） |

---

*本文档由架构讨论生成，代码实现阶段如有细节调整，需同步更新本文档。*
