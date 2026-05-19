# 技术文档：观测页面「智能复制错误日志」功能

## 问题定义

观测页面（`observability.html`）已能展示错误、Trace、Events 等可观测数据，但用户在发现错误后，需要手动从 UI 中摘录信息再粘贴给 AI 进行代码诊断。这个过程：
- 容易遗漏关键上下文（如 `details` 中的具体参数）
- 容易混入健康信息（info 级别事件、成功指标），挤占 AI Token 预算
- 手动操作效率低

**目标**：在观测页面提供一键复制功能，输出的内容最大化保留错误信息、自动过滤健康噪音，直接供 AI 读取并定位代码问题。

---

## 职责边界

| 层级 | 职责 | 本功能归属 |
|------|------|-----------|
| 后端 API | 提供原始可观测数据（`/observability/errors`、`/observability/traces`） | **不修改** |
| 前端 UI | 渲染数据、提供交互入口、执行过滤/聚合/格式化 | **本功能全部在前端完成** |
| AI 修复 | 接收复制内容、分析错误、输出修复方案 | **不感知本功能存在** |

**原则**：后端零改动，所有智能处理逻辑集中在 `observability.js` 中。

---

## 修复策略 / 实现方案

### 1. 复制入口

| 入口 | 位置 | 复制范围 |
|------|------|----------|
| 「📋 Copy for AI」按钮 | Errors 面板顶部工具栏 | 当前过滤条件下可见的全部错误 |
| 「📋 Copy Errors」按钮 | 每个 Trace 卡片展开后的详情区 | 该 Trace / Session 关联的所有错误 |

### 2. 健康信息过滤规则

**自动丢弃（不复制）**：
- `level === "info"` 或 `"debug"` 的事件
- Metrics 数据、成功的 tool result

**保留（精简后复制）**：
- `level === "error"` 或 `"warn"`
- 保留字段：`type`、`source`、`category`、`message`、`details`（完整嵌套结构）
- 弱化字段：`trace_id`、`session_id` 仅在样本时间线中出现，正文不重复

### 3. 聚合去重策略

按 `type + source + category + message` 分组。同一分组的错误合并为：
```
[1] agent.toolValidationFailed (×3)
```

- 取第一条的 `details` 作为 representative 展示
- 给出最多 5 条样本的时间 + session + trace，超出标注 `... and N more`
- 单条错误直接写 `occurred: ...`

### 4. 输出格式（扁平结构化文本，AI 优先）

**不是给人看的 Markdown 报告**，而是信息密度高、无装饰的纯文本：

```text
--- Error Context ---
Generated: 2026-05-12T14:32:10.123Z
Total unique error types: 2 (5 occurrences)

[1] agent.toolValidationFailed (×3)
source: agent_loop
category: validation
level: error
message: Tool 'read_file' input validation failed
details:
  tool_id: read_file
  call_id: call-a1b2c3d4
  turn: 5
  errors:
    -
      param: path
      issue: required field missing
samples:
  - 2026-05-12T01:20:05Z, session=sess-abc123, trace=trace-xyz789
  - 2026-05-12T01:21:12Z, session=sess-abc123, trace=trace-xyz789
  - 2026-05-12T01:31:22Z, session=sess-def456, trace=trace-uvw012

[2] agent.stateChanged
source: agent_loop
category: system
level: error
message: Agent entered error state
details:
  old_state: streaming
  new_state: error
occurred: 2026-05-12T01:20:05Z, session=sess-abc123, trace=trace-xyz789
```

### 5. Trace 错误兜底逻辑

如果用户点击 Trace 卡片的「Copy Errors」时，前端缓存 `_cachedErrors` 中没有该 Trace/Session 的错误：
1. 调用 `/observability/traces/${traceId}` 获取 Trace 原始 JSON
2. 遍历 `trace.spans`，提取 `span.error` 非空的条目
3. 组装为临时错误对象，再走 `formatErrorsForAI()` 格式化

---

## 数据模型变更

**无变更**。后端数据模型（`errors.jsonl`、`trace_*.json`）不变。前端仅在内存中对已有数据结构做过滤和重组。

---

## 实施检查清单

- [x] Errors 面板顶部增加「📋 Copy for AI」按钮
- [x] Trace 卡片详情区增加「📋 Copy Errors」按钮
- [x] 实现 `formatErrorsForAI(errors)`：过滤 → 聚合 → 扁平文本输出
- [x] 实现 `copyAllVisibleErrors()`：绑定 Errors 面板按钮
- [x] 实现 `copyTraceErrors(traceId, sessionId)`：绑定 Trace 卡片按钮
- [x] 实现 `_extractErrorsFromTrace(trace)`：Trace spans 兜底提取
- [x] 实现 `copyToClipboard()`：含 `navigator.clipboard` + fallback
- [x] 实现 `showToast()`：复制成功反馈
- [x] 语法检查通过（`node --check`）

---

## 修改文件清单

| 文件 | 改动说明 |
|------|----------|
| `web/observability.html` | Errors 面板顶部增加 Copy for AI 按钮；Trace 卡片详情区增加 Copy Errors 按钮 |
| `web/static/observability.js` | 新增 `formatErrorsForAI`、`copyAllVisibleErrors`、`copyTraceErrors`、`_extractErrorsFromTrace`、`copyToClipboard`、`showToast` 函数；`loadTraces()` 中渲染 Trace 卡片时插入 Copy Errors 按钮 |

---

## 关键决策

1. **纯前端实现**：不新增后端 API，减少部署复杂度。若未来错误量极大（>10K 条），再考虑后端聚合接口。
2. **扁平文本 > Markdown 报告**：用户明确「copy 出来的信息是给 AI 修复代码 bug 的，不是给人看的」，因此放弃标题、加粗、分隔线等装饰性格式，采用 `key: value` 扁平结构。
3. **样本上限 5 条**：防止同类错误爆发时（如循环中重复失败）输出文本过长，挤占 AI Token 预算。
