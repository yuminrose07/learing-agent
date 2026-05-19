# 上下文 Usage 观测与展示设计

## 背景

上下文使用量能力用于把单轮对话的上下文占用情况稳定地暴露给产品层、Web SSE、会话历史和观测系统。该能力的目标不是做计费对账，而是回答以下问题：

- 当前轮上下文大约用了多少 tokens
- 这些数字是估算值还是来自 Provider 的真实 usage
- 当前轮是否触发了 micro/full compact
- 页面刷新后是否还能回放同一份 usage
- 当 Provider 或前端拿不到 usage 时，界面如何优雅降级

该能力已在 Runtime、Product/Application、Web Interface 和前端展示链路中落地。

## 分层边界

- `Agent Runtime`
  - 在上下文构建完成后生成 `TurnUsage`
  - 若 Provider 返回真实 usage，则用真实值回填当前轮 `TurnUsage`
  - 在流式 chunk metadata、assistant message metadata 和观测事件中发布 usage
- `Product/Application`
  - 在 `stream_session_chat()` 中合并流式 metadata
  - 对外补齐兼容字段：若已有 `turn_usage` 且缺少 `usage`，则自动镜像一份 `usage`
- `Interface`
  - `POST /sessions/{session_id}/chat` 的 SSE 输出对外暴露 `usage`
  - `GET /sessions/{session_id}` 返回持久化后的 assistant message metadata
- `Frontend`
  - 流式消费 SSE `usage`
  - 历史回放时读取 `metadata.usage` 或 `metadata.turn_usage`
  - usage 缺失时仅隐藏 usage 区块，不影响正文、persona 和消息流

## `TurnUsage` 数据结构

当前共享模型定义在 `learning_agent/ai/models.py` 的 `TurnUsage`。

### 顶层字段

| 字段 | 类型 | 含义 | 何时可空/默认 |
|------|------|------|---------------|
| `estimated_prompt_tokens` | `int` | 当前轮上下文消息估算 token 数 | 默认 `0`，始终存在 |
| `actual_prompt_tokens` | `int \| null` | Provider 返回的真实输入 tokens | Provider 不返回时为空 |
| `actual_completion_tokens` | `int \| null` | Provider 返回的真实输出 tokens | Provider 不返回时为空 |
| `actual_total_tokens` | `int \| null` | Provider 返回的真实总 tokens | Provider 不返回时为空 |
| `context_limit` | `int` | 当前模型可用上下文上限 | 默认 `0`，正常运行时应为模型上限 |
| `utilization_ratio` | `float` | `estimated_prompt_tokens / context_limit` 的四位小数比例 | 默认 `0.0` |
| `is_estimated` | `bool` | 是否仍是估算口径 | 默认 `true`；一旦收到任一真实 usage 字段即变为 `false` |
| `compaction` | `TurnCompactionUsage` | 当前轮压缩相关状态 | 默认空结构 |

### `compaction` 子字段

| 字段 | 类型 | 含义 |
|------|------|------|
| `micro_compact_applied` | `bool` | 当前轮是否应用 micro compact |
| `full_compact_applied` | `bool` | 当前轮是否应用 full compact |
| `summary_block_present` | `bool` | 当前轮上下文中是否存在摘要块 |
| `full_compact_scope` | `str \| null` | full compact 的作用范围 |
| `recent_token_budget` | `int` | 保留 recent messages 的预算 |

## 数据来源与对账规则

### 估算值

- Runtime 在构建好上下文消息后，对每条消息内容执行 token 估算，累加生成 `estimated_prompt_tokens`
- `context_limit` 来自 Provider 的 `get_max_context_length()`
- `utilization_ratio` 使用估算 prompt tokens 计算，而不是使用 completion 或 total tokens
- 因此前端展示的“占比”语义是“当前轮上下文输入占比”，不是整轮账单占比

### 真实 usage

- OpenAI Provider 若在流式或非流式响应中拿到 SDK usage，会先写入 chunk metadata 中的 `provider_usage`
- Runtime 从 `provider_usage` 提取真实值后，使用 `TurnUsage.with_provider_usage()` 合并
- 只要 `prompt_tokens`、`completion_tokens`、`total_tokens` 中任一字段可用，`is_estimated` 就会变为 `false`
- 如果 Provider 完全不返回 usage，则 `actual_*` 字段保持为空，前端继续按“估算”展示

## 对外暴露路径

### 1. 流式 SSE

`POST /sessions/{session_id}/chat` 返回的 SSE `data:` JSON 中，会暴露：

```json
{
  "content": "你好",
  "finish_reason": "stop",
  "mode": "chat",
  "alignment": false,
  "persona_key": "virtuous_consort_pei_ruotang",
  "persona_name": "贤妃·裴若棠",
  "usage": {
    "estimated_prompt_tokens": 320,
    "actual_prompt_tokens": 344,
    "actual_completion_tokens": 16,
    "actual_total_tokens": 360,
    "context_limit": 128000,
    "utilization_ratio": 0.0025,
    "is_estimated": false,
    "compaction": {
      "micro_compact_applied": true,
      "full_compact_applied": false,
      "summary_block_present": false,
      "full_compact_scope": null,
      "recent_token_budget": 16000
    }
  }
}
```

补充说明：

- Interface 层输出的公开字段名是 `usage`
- Product 层在流式输出前会做一次兼容补齐：若 chunk metadata 里只有 `turn_usage`，会自动复制到 `usage`
- 因此前端与其他调用方优先消费 `usage` 即可，不需要依赖 Runtime 内部命名

### 2. assistant message metadata 持久化

assistant 消息写入 session 历史时，会将最终 `TurnUsage` 落到：

```json
{
  "role": "assistant",
  "content": "……",
  "metadata": {
    "turn_usage": {
      "estimated_prompt_tokens": 320,
      "actual_total_tokens": 360,
      "context_limit": 128000,
      "utilization_ratio": 0.0025,
      "is_estimated": false,
      "compaction": {
        "micro_compact_applied": true,
        "full_compact_applied": false,
        "summary_block_present": false,
        "full_compact_scope": null,
        "recent_token_budget": 16000
      }
    }
  }
}
```

补充说明：

- 持久化权威字段是 `metadata.turn_usage`
- 前端历史回放时会优先读取 `metadata.usage`，若不存在则回退到 `metadata.turn_usage`
- 这保证了新旧数据都能展示，同时不要求旧 session 立即迁移

### 3. 观测事件与指标

Runtime 会发布 `agent.turnUsage` 事件，payload 结构如下：

```json
{
  "phase": "response",
  "usage": {
    "estimated_prompt_tokens": 320,
    "actual_prompt_tokens": 344,
    "actual_completion_tokens": 16,
    "actual_total_tokens": 360,
    "context_limit": 128000,
    "utilization_ratio": 0.0025,
    "is_estimated": false
  }
}
```

当前 `phase` 可能值：

- `response`：标准响应轮
- `single_pass`：单次直出轮
- `finalize`：流中断后的兜底完成轮

观测指标语义如下：

- `llm.chunk.received`
  - 仅表示收到的流式 chunk 数，不代表 tokens
- `llm.usage.turns{phase=...,is_estimated=...}`
  - 记录 turn usage 事件次数
- `llm.usage.estimated_prompt_tokens`
  - 记录估算输入 tokens
- `llm.usage.actual_prompt_tokens`
  - 记录真实输入 tokens
- `llm.usage.actual_completion_tokens`
  - 记录真实输出 tokens
- `llm.usage.actual_total_tokens`
  - 记录真实总 tokens
- `llm.usage.context_limit`
  - 记录模型上下文上限
- `llm.usage.context_utilization_ratio`
  - 记录上下文输入占比

约束：

- 禁止再把 `agent.responseChunk` 或 `llm.chunk.received` 解释为 token 数
- 若真实 usage 不可得，只记录 estimated 相关值，不伪造 `actual_*`

## 前端展示约定

聊天页 assistant 气泡上的 usage 展示遵循以下规则：

- 主值优先级
  - 优先显示 `actual_prompt_tokens`
  - 否则显示 `estimated_prompt_tokens`
  - 若两者都没有，则退到 `actual_total_tokens`
- 标签文案
  - 有 prompt 口径时显示“上下文”
  - 仅剩 total 时显示“总量”
- 状态文案
  - `is_estimated=true` 显示“估算”
  - `is_estimated=false` 显示“已对账”
- 额外信息
  - 若存在 `context_limit`，显示“上限”
  - 若存在 `utilization_ratio`，显示“占比”
  - 若存在 `actual_completion_tokens` 和 `actual_total_tokens`，在次级详情行展示“输出”和“总计”

## 降级行为

### Provider 不返回 usage

- SSE 仍会携带基于 Runtime 估算生成的 `usage`
- `actual_*` 字段为空
- 前端显示“估算”，不会报错或阻断消息流

### SSE 某些 chunk 没有 usage

- 前端只在收到 `data.usage` 时更新 usage 区块
- 若正文 chunk 没有 usage，正文仍继续流式追加
- 只要后续任一 chunk 携带 usage，当前 assistant 气泡即可补齐 usage

### 历史消息缺少 usage

- 前端历史渲染会尝试读取 `metadata.usage` 或 `metadata.turn_usage`
- 两者都不存在时，不渲染 usage 区块
- 不影响消息正文、角色头像、persona badge 和历史加载

### usage 值不可展示

- 若传入对象不含任何可展示数值，前端会移除 usage 区块
- 这是展示层降级，不会改写服务端 session 数据

## 对接建议

- 新接入方若消费流式接口，优先读取 SSE `usage`
- 若做历史回放，优先读取 `metadata.usage`，同时兼容 `metadata.turn_usage`
- 若做指标分析，使用 `agent.turnUsage` 和 `llm.usage.*`，不要把 chunk 数当作 token 数
- 若新增 Provider，建议在 SDK 响应末尾补齐 `provider_usage`，以便自动升级为“已对账”
