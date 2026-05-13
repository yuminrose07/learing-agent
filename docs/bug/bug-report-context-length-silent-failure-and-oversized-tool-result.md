# Bug Report：ContextLengthError 被静默吞掉 & 超大工具结果无保护导致会话持续故障

**报告日期**: 2026-05-12  
**严重级别**: P0 — 导致会话不可恢复、Trace 失真、用户无法获知真实错误  
**影响模块**: `agent_loop.py`, `code_tools.py`, `context_compressor.py`, `observability.py`  
**关联审计**: [code-audit-report-agent-loop-2026-05-12.md](./code-audit-report-agent-loop-2026-05-12.md)（Issue #6、#12 相关）

---

## 一、问题摘要

当 Agent 调用 `read_file` 读取大文件后，工具执行**成功**，但返回的超大内容被直接存入会话历史。在下一轮 ReACT turn 组装 LLM 上下文时，总长度超过模型窗口上限，Provider 抛出 `ContextLengthError`。

**当前代码对该错误的处理是：在 `_stream_chat_with_retry` 内部捕获异常，yield 一段 `[Error] Context length exceeded...` 文本后正常 return。外层调用方感知不到异常，继续走"成功"分支，最终状态变为 `COMPLETED`，且 Trace 中没有任何 error 标记。**

此外，导致上下文超限的超大工具结果**永久留在 `session.entries` 中**，后续用户每次发消息都会重新触发同样的错误，导致会话**持续不可用**。

---

## 二、影响范围

| 维度 | 影响描述 |
|------|---------|
| **用户体验** | 用户收到 "[Error] Context length exceeded..." 文本，但该文本被当成普通 assistant 回复呈现，用户无法区分这是系统错误还是 LLM 的正常输出。 |
| **会话可用性** | 超大 TOOL 消息永久留在 session 历史中，后续每轮对话都会继续爆上下文，**会话无法自愈**。 |
| **可观测性** | Trace/Span 中 `llm.stream` 被正常结束，`root_span` 正常结束，trace 状态为 `COMPLETED`，**完全掩盖了实际故障**。 |
| **降级逻辑** | 现有的 chat-only 降级逻辑基于"连续工具执行失败"，但本场景是"工具执行成功但结果超限"，**现有降级逻辑完全不触发**。 |
| **资源浪费** | 超大文件内容被完整持久化到 session 存储，占用磁盘空间。 |

---

## 三、复现步骤

1. 启动 Agent 会话，确保 `ContextCompressor` 扩展已加载（默认启用）。
2. 用户输入：`请帮我读取 node_modules 下面某个大型 JS 文件（>1MB）的内容`。
3. LLM 输出 `tool_call: read_file`，工具执行成功，返回完整文件内容（数十万字符）。
4. 工具结果作为 `MessageRole.TOOL` 存入 `session.entries`。
5. 进入 ReACT Turn 2，`_build_context_for_turn` 组装上下文时包含该超大 TOOL 消息。
6. `_stream_chat_with_retry` 调用 provider，`stream_chat` 抛出 `ContextLengthError`。
7. **预期行为**：错误被记录到 trace，状态变为 `ERROR` 或触发降级，向用户给出清晰错误说明。  
   **实际行为**：`_stream_chat_with_retry` yield `[Error] Context length exceeded...` 后正常 return，外层无异常，trace 无 error，状态 `COMPLETED`。
8. 用户再次发送任意消息 → 重复步骤 5-7，会话**永久卡死**。

---

## 四、根因分析（按代码层面拆解）

### 4.1 LLM 调用层：`_stream_chat_with_retry` 静默吞掉 `ContextLengthError`

**文件**: `learning_agent/agent/agent_loop.py`  
**行号**: 869-910

```python
async def _stream_chat_with_retry(...):
    for attempt in range(max_attempts + 1):
        try:
            async for chunk in self.provider.stream_chat(params):
                yield chunk
            return
        except ContextLengthError as e:
            # ← 问题在这里
            yield ChatChunk(content=f"\n[Error] Context length exceeded: {e}\n")
            return
```

**问题**：
- `ContextLengthError` 是**不可恢复错误**（上下文已经超限，重试无意义），但这里把它当成"可降级为文本输出"的错误来处理。
- 内部 `yield` 一个 `ChatChunk` 后 `return`，外层 `async for` 不会抛出异常，因此：
  - `run_turn` 中 `llm_error_occurred` 保持 `False`（第 392、449-455 行）
  - 不进入流中断兜底逻辑（第 460-505 行）
  - `llm_span` 在 `finally` 中正常结束，**未设置 `span.error`**（第 457-458 行）
  - 状态最终变为 `COMPLETED`（第 312-314、767 行）

**正确行为**：`ContextLengthError` 应该向上传播，由外层的 `except Exception` 捕获，触发完整的错误处理流程（记录 span error、状态转换、降级/收尾）。

### 4.2 工具层：`read_file` 无大小保护

**文件**: `learning_agent/extensions/code_tools.py`  
**行号**: 105-120

```python
async def _tool_read_file(path: str, offset: int = 1, limit: int = 0, **kwargs: Any) -> dict[str, Any]:
    ...
    with open(target, "r", encoding="utf-8") as f:
        lines = f.readlines()   # ← 一次性读入内存，无大小限制
```

**问题**：
- 无文件大小预检。
- `limit=0`（默认值）时返回全部内容。
- 对二进制/超大文本文件无防御。

### 4.3 会话层：超大工具结果无截断直接持久化

**文件**: `learning_agent/agent/agent_loop.py`  
**行号**: 579-598

```python
for tc, result, is_error in tool_results:
    result_content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    entry = self.sessions.append_message(
        session.id,
        MessageRole.TOOL,
        result_content,        # ← 完整超大内容直接存入
        metadata={...},
    )
```

**问题**：
- 工具结果在存入 session 前**没有大小上限检查**。
- 即使一条 TOOL 消息就超过模型上下文窗口，它仍然被完整保存。
- 该消息永久留在 `session.entries`，后续每次对话都会重新组装进上下文。

### 4.4 上下文层：`ContextCompressor` 无法完全兜底

**文件**: `learning_agent/extensions/context_compressor.py`  
**行号**: 97-178

`TokenBudgetCompressor` 的策略是"从 newest 往 oldest 保留 turn unit"。如果超大 TOOL 消息是最近一条，它会被**优先保留**。只有当该 unit 的 token 估算 + system prompt + user message > `max_context_tokens` 时才会被丢弃。

**问题**：
- `_estimate_tokens` 对中文使用 `len(content) // 3` 启发式估算，可能严重低估实际 token（特别是代码/json 内容）。
- 即使被丢弃，也只是**不发送给 LLM**，但 `session.entries` 中仍然完整保留。
- 如果 system prompt + user message 本身就很大，加上超大 TOOL 消息后，可能刚好卡在预算内但实际发送时仍超限。

### 4.5 可观测性：`llm_span` 错误未被记录

**文件**: `learning_agent/agent/agent_loop.py`  
**行号**: 360-458

```python
llm_span = self.obs.start_span("llm.stream", ...)
try:
    async for chunk in self._stream_chat_with_retry(session, params):
        ...
except Exception as e:
    if llm_span:
        llm_span.error = str(e)   # ← 永远不会走到这里
finally:
    if self.obs:
        self.obs.end_span(llm_span, ...)   # ← 正常结束，无 error
```

**问题**：因为异常在 `_stream_chat_with_retry` 内部被吞掉，外层 `except` 不触发，`llm_span.error` 永远不被设置。Trace 中该 turn 看起来完全正常。

### 4.6 降级逻辑不匹配

**文件**: `learning_agent/agent/agent_loop.py`  
**行号**: 601-643

chat-only 降级逻辑基于 `consecutive_tool_failure_turns`（连续工具**执行失败**的轮数）。本场景中：
- 工具**执行成功**（`is_error=False`）
- 错误发生在**下一轮 LLM 调用时**

因此 `consecutive_tool_failure_turns` 保持为 0，**降级逻辑完全不触发**。

### 4.7 `_finalize_with_llm` 的二次故障风险

**文件**: `learning_agent/agent/agent_loop.py`  
**行号**: 1272-1314

即使修复了 `_stream_chat_with_retry` 让异常传播并触发流中断兜底，`_finalize_with_llm` 也会调用 `_build_context_for_turn` 重新组装上下文。如果导致超限的超大 TOOL 消息仍在 session 中，finalize 自己也会再次抛出 `ContextLengthError`，导致兜底失败。

---

## 五、修复建议

### 5.1 紧急修复（阻断故障）

#### A. 让 `ContextLengthError` 向上传播

修改 `_stream_chat_with_retry`：

```python
except ContextLengthError as e:
    # 不在这里 yield 错误文本，而是向上传播
    # 让外层错误处理流程接管
    raise
```

或者保留 yield 作为用户提示，但**同时 re-raise**：

```python
except ContextLengthError as e:
    yield ChatChunk(content=f"\n[System] Context length exceeded. Initiating recovery...\n")
    raise  # ← 关键：继续向上传播
```

#### B. 在 `_finalize_with_llm` 中防御二次超限

在调用 `_build_context_for_turn` 之前，检测并移除/截断导致超限的超大 TOOL 消息。例如：扫描最近一轮的 TOOL 结果，如果单条超过阈值，将其替换为 `[Truncated] Result too large...`。

### 5.2 工具层加固

#### C. `read_file` 增加内容上限

```python
MAX_READ_FILE_CHARS = 100_000  # 约 25K-50K tokens，留有余量

async def _tool_read_file(...) -> dict[str, Any]:
    ...
    content = "".join(selected)
    if len(content) > MAX_READ_FILE_CHARS:
        content = content[:MAX_READ_FILE_CHARS] + f"\n\n[Truncated] File exceeded {MAX_READ_FILE_CHARS} character limit. Use offset/limit to read specific sections."
    return {"content": content, "offset": start + 1, "limit": len(selected), "total_lines": total_lines}
```

### 5.3 会话层加固

#### D. 工具结果存入 session 前增加截断

在 `_execute_tool_calls` 的 Step 8（第 579-598 行）中，对 `result_content` 进行大小检查：

```python
MAX_TOOL_RESULT_CHARS = 150_000
if len(result_content) > MAX_TOOL_RESULT_CHARS:
    result_content = result_content[:MAX_TOOL_RESULT_CHARS] + "\n\n[Truncated] Tool result exceeded size limit."
```

这作为**最后一道防线**，防止任何工具（不仅是 `read_file`）返回超大内容。

### 5.4 可观测性加固

#### E. 在 `_stream_chat_with_retry` 中记录错误事件

即使选择 yield + return 的方案（不推荐），也应该在 return 前显式发射 error 事件：

```python
await self.events.publish(Event(
    type="agent.contextLengthExceeded",
    payload={"error": str(e)},
    source="agent_loop",
    session_id=session.id,
))
```

但更推荐让异常传播，由外层统一处理。

---

## 六、修复优先级

| 优先级 | 修复项 | 理由 |
|--------|--------|------|
| P0 | A. `ContextLengthError` 向上传播 | 这是最根本的错误处理链路断裂 |
| P0 | B. `_finalize_with_llm` 防御二次超限 | 否则修复 A 后兜底流程会自己炸 |
| P1 | D. 工具结果存入 session 前截断 | 通用防线，防止任何工具产生超大结果 |
| P1 | C. `read_file` 增加上限 | 从源头减少大文件读取问题 |
| P2 | E. 可观测性事件补充 | 便于事后追溯 |

---

## 七、验证检查清单

修复完成后，应验证以下场景：

- [ ] 让 LLM 读取一个超大文件（>500KB），确认 `_stream_chat_with_retry` 抛出异常而非静默返回。
- [ ] 确认 `llm_span.error` 被正确设置，Trace 中可见 error 标记。
- [ ] 确认 Agent 状态最终为 `ERROR` 或触发降级，而非 `COMPLETED`。
- [ ] 确认 `_finalize_with_llm` 能成功完成，向用户返回清晰的错误说明。
- [ ] 确认后续用户发新消息时，会话能正常继续（超大 TOOL 消息已被截断/移除）。
- [ ] 确认 `read_file` 读取大文件时自动截断，提示用户使用 `offset/limit`。
- [ ] 确认普通小文件读取不受影响。

---

## 八、相关代码引用汇总

| 文件 | 行号 | 相关逻辑 |
|------|------|---------|
| `learning_agent/agent/agent_loop.py` | 869-910 | `_stream_chat_with_retry`，`ContextLengthError` 被吞掉 |
| `learning_agent/agent/agent_loop.py` | 392, 449-458 | `llm_error_occurred` 与 `llm_span` 错误标记 |
| `learning_agent/agent/agent_loop.py` | 460-505 | 流中断兜底逻辑（未触发） |
| `learning_agent/agent/agent_loop.py` | 579-598 | 工具结果存入 session（无截断） |
| `learning_agent/agent/agent_loop.py` | 601-643 | chat-only 降级逻辑（不匹配本场景） |
| `learning_agent/agent/agent_loop.py` | 1272-1314 | `_finalize_with_llm`（可能二次超限） |
| `learning_agent/extensions/code_tools.py` | 105-120 | `read_file` 无大小保护 |
| `learning_agent/extensions/context_compressor.py` | 97-178 | `TokenBudgetCompressor` 估算与策略 |
| `learning_agent/core/observability.py` | 198-232 | `end_span` 逻辑 |
