# API 兼容性与 Tool Results 完整性修复 —— 实施总结

> 对应技术文档：`docs/design/api-compatibility-tool-results-fix.md` (v3.0)
> 实施日期：2026-05-12

---

## 功能概述

本次实施修复了以下问题：

1. **`reasoning_content` 缺失**：上下文构建时已兜底填充空字符串，Provider 序列化保留字段。
2. **`tool_call_id` 不匹配**：解析 tool calls 时兜底生成稳定 `call_id`，保存 assistant entry 时回写 buffer。
3. **孤儿 tool calls（流中断）**：新增流中断兜底流程——补偿 synthetic tool results 后直接调用收尾 LLM（纯对话模式），不进入 ReACT 重试循环。
4. **Tool results 一致性检测**：新增 `BEFORE_TOOL_RESULTS_PERSIST` Hook，由 `core-tool-results-validator` 扩展负责检测、排序、修正。

---

## 修改文件清单

| 文件 | 改动说明 |
|------|---------|
| `learning_agent/models/models.py` | 1. `HookPoint` 枚举新增 `BEFORE_TOOL_RESULTS_PERSIST`<br>2. 新增 `ToolResultsBatch` 数据类 |
| `learning_agent/models/__init__.py` | 导出 `ToolResultsBatch` |
| `learning_agent/agent/agent_loop.py` | 1. **修改点 A**：保存 assistant entry 时回写 `call_id` 到 `tool_call_buffers`<br>2. **修改点 B**：ReACT 循环内流中断兜底逻辑（删除旧 `break`，替换为 `_ensure_tool_results_for_orphans` + `_finalize_with_llm` + `break`）<br>3. **修改点 C**：`run()` 外层 `except` 中增加补偿孤儿 tool + 尝试收尾 LLM + 错误通知<br>4. **修改点 D**：`_execute_tool_calls` 返回后触发 `BEFORE_TOOL_RESULTS_PERSIST` Hook<br>5. **修改点 E1/E2**：`_build_context_for_turn` 中 `reasoning_content` 兜底和 `tool_call_id` 读取（已有代码，无需改动）<br>6. 新增 `_ensure_tool_results_for_orphans()` 方法<br>7. 新增 `_finalize_with_llm()` 方法<br>8. `_stream_chat_with_retry`：最终 `RetryableError` 和未知异常改为 `raise`（使调用方可进入兜底流程） |
| `learning_agent/extensions/built_in.py` | 1. 新增 `_create_tool_results_validator_extension()` 工厂函数<br>2. 在 `create_builtin_extensions()` 中注册新扩展 |
| `learning_agent/provider/openai_provider.py` | `_convert_messages()` 已保留 `reasoning_content` 字段（`is not None` 判断），无需额外修改 |

---

## 关键决策

### 1. `_stream_chat_with_retry` 的最终异常处理

文档未明确列出此修改，但为使流中断兜底逻辑（修改点 B）能够工作，必须让 `_stream_chat_with_retry` 在最终失败时传播异常而非静默 yield 错误 chunk。

- `RetryableError` 重试耗尽后改为 `raise`
- `Exception`（未知异常）改为 `raise`
- `AuthError`、`InvalidRequestError`、`ContextLengthError` 保持现有行为（不归入兜底）

### 2. Hook Context 中传入 `events`

文档要求扩展通过 `ExtensionContext.publish_event()` 发射 `agent.toolResultsValidated` 事件，但 Hook handler 无法直接访问 `ExtensionContext`。因此在 AgentLoop 调用 Hook 时将 `self.events` 传入 context，handler 通过 `context.get("events")` 发射事件。

### 3. `_finalize_with_llm` 的异常处理策略

`_finalize_with_llm` 是 `AsyncIterable`，内部不捕获 Provider 异常，由调用方（修改点 B）的 `try/except` 捕获。捕获后：
- yield 最终错误 chunk `"[Error] Unable to continue. Please try again later."`
- 设置 `finalized = False`
- 设置 `AgentState.ERROR`
- 发射 `agent.streamInterrupted` 事件（`finalized=False`）

### 4. 外层异常中 `tool_call_buffers` 的 locals 检查

`tool_call_buffers` 定义在 ReACT 循环内部，外层 `except` 可能在其定义前捕获异常。添加 `"tool_call_buffers" in locals()` 检查以避免 `NameError`。

---

## 验证状态

| 检查项 | 状态 |
|--------|------|
| 语法检查 (`python3 -m py_compile`) | ✅ 通过 |
| `from learning_agent.agent.agent_loop import AgentLoop` | ✅ 通过 |
| `from learning_agent.extensions.built_in import create_builtin_extensions` | ✅ 通过，`core-tool-results-validator` 已注册 |
| `HookPoint.BEFORE_TOOL_RESULTS_PERSIST` | ✅ 存在 |
| `ToolResultsBatch` 导入 | ✅ 通过 |

### 待办（需运行时验证）

- [ ] 场景 A：模拟 LLM 流中断，确认直接调用收尾 LLM，无重试循环
- [ ] 场景 B：模拟收尾 LLM 也失败，确认用户收到友好错误通知
- [ ] 场景 C：模拟外层异常，确认收尾 LLM 和错误通知同时生效
- [ ] ID 一致性：确认 assistant entry 的 `call_id` 与 tool result entry 的 `tool_call_id` 严格一致
- [ ] `core-tool-results-validator` 在数量/ID 不匹配时的修正行为

---

*本文档由代码实施阶段生成，用于后续快速查阅。*
