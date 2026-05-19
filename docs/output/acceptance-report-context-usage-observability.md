# 上下文 Usage 能力最小验收记录

## 目标

确认上下文 usage 能力已经满足以下对外交付要求：

- 可生成结构化 `TurnUsage`
- 可通过聊天 SSE 实时暴露 `usage`
- 可在 assistant message metadata 中持久化
- 可在前端展示“估算/已对账”
- usage 缺失时可优雅降级
- 观测指标不再把 chunk 数误当 token 数

## 验收范围

### 1. Runtime 与 Provider

- Runtime 在上下文构建后生成 `TurnUsage`
- Provider 若返回 `provider_usage`，Runtime 会回填 `actual_*` 字段并将 `is_estimated` 切为 `false`

### 2. Product / Interface

- `stream_session_chat()` 会对外补齐 `metadata.usage`
- `/sessions/{session_id}/chat` 的 SSE payload 暴露 `usage`
- `GET /sessions/{session_id}` 可返回 assistant message 中持久化的 `metadata.turn_usage`

### 3. Frontend

- 流式对话过程中，assistant 气泡可显示 usage 区块
- 页面刷新后，历史消息可从 `metadata.turn_usage` 回放同一份 usage
- usage 缺失时，仅隐藏 usage 区块，不影响正文

### 4. Observability

- 记录 `agent.turnUsage` 事件
- 记录 `llm.usage.*` 指标
- `llm.chunk.received` 仅表示 chunk 数，不表示 token 数

## 最小验证结果

### 自动化验证

已存在并通过的针对性验证：

- `tests/test_turn_usage.py::test_single_pass_turn_persists_turn_usage_in_assistant_metadata`
  - 验证 assistant metadata 中会持久化 `turn_usage`
  - 验证真实 usage 会覆盖 `actual_*` 字段并标记为非估算
- `tests/test_turn_usage.py::test_stream_session_chat_merges_public_usage_metadata`
  - 验证 Product 层会将 `turn_usage` 暴露为公共 `usage`
- `tests/test_turn_usage.py::test_observability_distinguishes_chunk_and_usage_metrics`
  - 验证 chunk 指标与 usage 指标语义分离
- `tests/test_turn_usage.py::test_observability_skips_missing_actual_usage_metrics`
  - 验证无真实 usage 时不会伪造 `actual_*` 指标
- `tests/test_web_static_app.js`
  - 验证 `upsertAssistantUsage` 可正确区分“估算 / 已对账”DOM 展示
  - 验证 usage 缺失时会移除 usage 区块且不破坏正文容器
  - 验证历史消息可从 `metadata.turn_usage` 回放一致的 usage DOM
- `tests/test_web_adaptation.py`
  - 验证聊天 SSE 可直接透出 `metadata.usage`
  - 验证缺少公共 `usage` 时可回退到 `metadata.turn_usage`
  - 验证相关 Web 适配回归用例恢复通过，未因 usage 能力引入兼容性回退

### 本轮回归结果

本次围绕“上下文 usage 对外暴露与前端展示”任务重新执行了直接相关的最小回归：

- `python3 -m pytest tests/test_turn_usage.py tests/test_web_adaptation.py`
  - 结果：`24 passed`
- `node --test tests/test_web_static_app.js`
  - 结果：`3 passed`

结论：Runtime usage 收口、SSE 暴露、assistant metadata 持久化、前端 DOM 渲染与历史回放链路均通过当前最小回归验证。

### 手工/使用说明

#### 实时对话观察点

1. 启动 Web 服务。
2. 打开聊天页并发送一条消息。
3. 观察 assistant 气泡底部 usage 区块。
4. 若模型返回真实 usage，应显示“已对账”。
5. 若模型未返回真实 usage，应显示“估算”。

#### 历史回放观察点

1. 刷新页面或重新进入该 session。
2. 观察历史 assistant 消息。
3. 若该消息 metadata 中存在 `turn_usage`，应再次显示相同 usage。

#### 降级观察点

1. 使用不返回 usage 的 Provider，或构造缺少 usage 的历史消息。
2. 重新发送消息或刷新会话。
3. 页面应正常展示正文。
4. usage 区块可缺失，但不得导致报错、空白消息或历史加载失败。

## 前端展示口径

- “上下文”主值表示当前轮输入上下文占用，优先取真实 prompt tokens，否则取估算 prompt tokens
- “上限”表示模型 context limit
- “占比”表示 `estimated_prompt_tokens / context_limit`
- “输出”表示真实 completion tokens，仅在可得时展示
- “总计”表示真实 total tokens，仅在可得时展示
- “估算”表示仍未拿到真实 usage
- “已对账”表示至少已有一项真实 usage 已回填

## 已知边界

- 当前自动化验证已覆盖 Runtime、SSE 适配、前端 DOM 展示与历史回放渲染，但仍属于针对本能力的最小回归，不等同于更大范围的全量端到端覆盖
- 历史持久化的权威字段仍是 `metadata.turn_usage`；`metadata.usage` 主要用于对外流式兼容与前端读取兼容
- `utilization_ratio` 反映的是输入上下文占比，不等同于整轮账单占比

## 结论

本次能力已满足正式文档对齐、外部字段说明、最小使用说明、前端 DOM 自动化验证和降级行为说明要求，可作为当前实现基线。当前任务涉及的定向回归已通过，后续若继续推进，可优先补充更大范围的端到端联调覆盖。
