# 实施报告：观测页面增强方案

> 依据设计文档：`docs/design/design-observability-enhancement.md`
> 实施日期：2026-05-12

---

## 功能概述

本次实施解决了以下三个核心问题：

1. **Span 泄漏（问题 12）**：将全局单例的 `_active_span_stack` 重构为按 `trace_id` 隔离的 `_span_stacks: dict[str, list[TraceSpan]]`，彻底消除多 Session 并发时 span 栈互相破坏的问题。
2. **全局 trace 栈错乱**：引入 `_session_trace_map` 建立 session → trace 路由，确保每个 Session 的 span 操作都路由到正确的 trace 上下文。
3. **运行时状态可观测**：增强后端 API 和前端面板，支持实时查看 Session 运行时状态、按 Session 过滤 Trace、Span 树可视化、SSE 实时事件推送。

---

## 修改文件清单

| 文件 | 改动说明 |
|------|---------|
| `learning_agent/agent/observability.py` | **核心重构**：<br>1. `_active_span_stack` → `_span_stacks: dict[str, list[TraceSpan]]`<br>2. 新增 `_session_trace_map: dict[str, str]`<br>3. `start_trace/end_trace/start_span/end_span/current_span` 全部支持按 trace_id/session 隔离<br>4. 新增 `get_trace_by_session/get_spans_by_session/clear_session_traces`<br>5. 新增 `record_runtime_created/record_runtime_cleared`<br>6. `_update_metrics_from_event` 中新增 `agent.state` per-session Gauge |
| `learning_agent/agent/agent_loop.py` | **Span 生命周期安全**：<br>1. `AgentLoopSession.__init__` 新增 `_current_trace_id`<br>2. `run_turn` 中所有 span 操作显式传入 `trace_id=self._current_trace_id`<br>3. `ctx_span` 增加 `try/finally` 保护<br>4. `_execute_tool_calls` 中每个 tool call 用 `try/finally` 包裹，确保 `tool_span` 在任何异常路径下都被关闭<br>5. `_run_alignment_turn/_finalize_with_llm` 中的 span 操作传入 trace_id<br>6. `_set_state` 中 `current_span()` 传入 trace_id<br>7. `clear_session_runtime` 调用 `obs.clear_session_traces` 和 `record_runtime_cleared`<br>8. `_get_or_create_runtime` 调用 `record_runtime_created` |
| `learning_agent/web_server.py` | **API 增强**：<br>1. `GET /observability/runtimes` 增强：返回 `lock_acquired`, `trace` (含 span_count, active_spans, duration_ms), `total_session_count`<br>2. 新增 `GET /observability/events/stream` SSE 端点（轮询 events.jsonl 尾部推送）<br>3. 新增 `asyncio` 导入 |
| `web/observability.html` | **前端结构**：<br>1. Traces Panel 新增 Session filter (`<select id="trace-session-filter">`)<br>2. Runtimes Panel 新增 Summary Cards (Active Runtimes / Total Sessions) |
| `web/static/observability.js` | **前端逻辑**：<br>1. `loadRuntimes` 增强：渲染表格（含 Lock、Active Spans、Banned Tools、Reset 按钮），动态填充 summary cards<br>2. 新增 `resetRuntime` 函数<br>3. 新增 `startRuntimeRefresh/stopRuntimeRefresh`（5 秒轮询）<br>4. `switchTab` 中控制 Runtimes 自动刷新和 Events SSE 开关<br>5. `loadTraces` 增强：支持按 Session 过滤，动态填充 filter options<br>6. 新增 `buildSpanTree/renderSpanTree`：Trace 详情展示层级化 Span 树（保留 duration 瀑布条）<br>7. 新增 `startEventStream/stopEventStream/prependEventToTable`：Events 标签 SSE 实时推送<br>8. `showToast` 增强：支持 `type='error'` 样式 |

---

## 关键决策

1. **为何在 `AgentLoopSession` 中显式保存 `_current_trace_id`？**
   - 设计文档保留 `_active_trace` 作为兜底，但并发场景下 `_active_trace` 会被其他 Session 覆盖。
   - 最安全的做法是在 `run_turn` 开始时保存 `trace_id`，后续所有 span 操作显式传入。
   - 这比 `contextvars` 方案更简单、更明确，且与现有代码风格一致。

2. **`start_trace` 时为何结束该 session 的旧 trace？**
   - 防止一个 session 同时存在多个活跃 trace。
   - 如果 Session A 的 run1 还没结束，run2 又启动了，旧 trace 会被结束并持久化，然后开始新 trace。
   - 这与设计文档的决策 3 一致。

3. **`_execute_tool_calls` 的 `try/finally` 包裹范围？**
   - 把整个 for 循环体（从 `tool_call_id` 到 `AFTER_TOOL_RESULT` hook）包裹在 `try` 中，`finally` 里统一 `end_span(tool_span)`。
   - 这样无论 banned、validation failed、ask、abort、execution error 还是 hook 异常，tool_span 都会被正确关闭。

4. **SSE 实现方式？**
   - 采用轮询 `events.jsonl` 文件尾部的方式，而非 WebSocket/EventBus 直接推送。
   - 这是 P3 可选功能，实现简单，对现有架构侵入最小。
   - 已知限制：高并发下文件锁可能有性能问题（设计文档已说明）。

---

## 验证状态

- **Python 语法检查**：`py_compile` 通过 ✅
- **JavaScript 语法检查**：`node --check` 通过 ✅
- **测试套件**：`pytest tests/` 36/36 通过 ✅
- **隔离性验证**：手动测试多 session trace/span 隔离逻辑通过 ✅

### 已知限制

1. SSE 轮询 `events.jsonl` 在高并发下可能有文件锁竞争（P3 功能，可接受）。
2. `_active_trace` 仍作为兜底保留，长期所有调用应显式传入 `trace_id`。
3. `start_trace` 结束旧 trace 时，旧 trace 的 span 栈如果还有未关闭的 span，会被静默丢弃（`self._span_stacks.pop(old_trace_id, None)`）。这在正常流程中不应发生，但如果前一个 run 异常退出导致 span 泄漏，这是合理的清理行为。
