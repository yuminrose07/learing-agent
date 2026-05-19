# 修复：滑动窗口上下文截断导致 `tool_call_id is not found`

## 功能概述

本次修复解决了多轮对话中因 `_build_context_for_turn` 使用硬编码滑动窗口 `history[-20:]` 截断历史消息，导致 **assistant `tool_calls` 与对应 `tool` 消息被拆散**，从而触发 OpenAI/Moonshot API 报错 `Invalid request: tool_call_id is not found` 的问题。

修复策略：
- **删除核心层所有硬编码的滑动窗口截断逻辑**（`history[-20:]`、`history[-10:]`、`_compress_context` 粗暴移除一半消息）
- **将上下文压缩职责移交扩展层**，通过已有的 `BEFORE_CONTEXT_BUILD` Hook 点暴露标准接口
- **核心层默认行为改为透传完整历史**，确保 OpenAI API 协议完整性（assistant `tool_calls` ↔ tool `tool_call_id` 一一对应）

---

## 问题定义

### 现象
用户在第 5 轮对话（从 session 数据看是第 5 次用户输入后）触发 LLM stream 时，API 返回：

```
Invalid request: tool_call_id  is not found
```

### 根因分析

`AgentLoop._build_context_for_turn()` 在构建上下文时使用：

```python
for entry in history[-20:]:
```

当会话历史增长超过 20 条时，`history[-20:]` 从列表中间截断，可能出现以下情况：

```
历史消息（23 条）:
  [0] system
  [1] user
  [2] assistant (tool_calls: bash:0, bash:1)   ← 被截断丢弃
  [3] tool (tool_call_id: bash:0)              ← 保留，变成孤儿
  [4] tool (tool_call_id: bash:1)              ← 保留，变成孤儿
  ...
```

请求发送到 API 后，`role="tool"` 的消息携带 `tool_call_id="bash:0"`，但请求中已不存在 `role="assistant"` 消息包含 `id="bash:0"` 的 `tool_calls`。API 校验失败，返回 400 错误。

### 为什么前三轮正常

| 轮次 | 历史消息数 | `history[-20:]` 是否截断 | 结果 |
|------|-----------|------------------------|------|
| 1    | 3         | 否                     | ✅   |
| 2    | 6         | 否                     | ✅   |
| 3    | 11        | 否                     | ✅   |
| 4    | 17        | 否                     | ✅   |
| 5    | 23        | **是**                 | ❌   |

---

## 修复策略

### 1. 删除硬编码截断

- `_build_context_for_turn`：`history[-20:]` → `history`（完整历史）
- `_build_alignment_messages`：`history[-10:]` → `history`（完整历史）

### 2. 删除粗暴压缩方法 `_compress_context`

`_compress_context` 的实现是"移除最早的一半非系统消息"，同样会破坏 assistant-tool 对的完整性。本次一并删除。

`ContextLengthError` 的处理改为直接返回错误提示，不再尝试内置压缩。

### 3. 暴露 `BEFORE_CONTEXT_BUILD` Hook 接口

`HookPoint.BEFORE_CONTEXT_BUILD` 枚举值已存在于 `models.py`，但代码中从未触发。本次在 `_build_context_for_turn` 中激活该 Hook：

```python
hook_result = await self.hooks.execute(
    HookPoint.BEFORE_CONTEXT_BUILD,
    history,                                      # data: list[SessionEntry]
    {"session": session, "memory": self.memory, "events": self.events},
)
if hook_result.modified and isinstance(hook_result.data, list):
    history = hook_result.data
```

**Hook 契约：**
| 项目 | 说明 |
|------|------|
| Hook 点 | `BEFORE_CONTEXT_BUILD` (`agent.beforeContextBuild`) |
| 输入 `data` | `list[SessionEntry]` —— 经过 `_compress_tool_error_history` 处理后的历史 |
| 输入 `context` | `{"session": LearningSession, "memory": MemoryManager, "events": EventBus}` |
| 输出期望 | `HookResult(modified=True, data=list[SessionEntry])` |
| 职责边界 | 扩展层可对 `history` 进行压缩、摘要、重排等变换；**必须保证 assistant-tool 对的完整性** |

### 4. 保留 `_compress_tool_error_history`

`_compress_tool_error_history` 仅压缩特定格式的 validation error / banned 消息（成对过滤 assistant + tool），属于业务级容错，与通用上下文压缩职责不同，予以保留。

---

## 修改文件清单

| 文件 | 改动说明 |
|------|---------|
| `learning_agent/agent/agent_loop.py` | ① `_build_context_for_turn`: 删除 `history[-20:]`，新增 `BEFORE_CONTEXT_BUILD` Hook 触发；② 删除 `_compress_context` 方法；③ `_stream_chat_with_retry`: 删除 `ContextLengthError` 时的 `_compress_context` 调用；④ `_build_alignment_messages`: 删除 `history[-10:]`；⑤ 新增 `HookPoint` import |

---

## 关键决策

### 决策 1：为什么不做内置压缩，而是完全移除？

上下文压缩是一个策略性极强的领域：
- 不同场景需要不同策略（Token 预算截断、RAG 摘要、分层记忆、FIFO 等）
- 任何内置的"一刀切"策略（如 `[-20:]` 或"移除一半"）都会在某些场景下破坏协议或丢失关键信息
- 根据 AGENTS.md 分层自治原则，策略性决策应下放扩展层，核心层只保证最小必要逻辑（协议完整性）

### 决策 2：为什么保留 `_compress_tool_error_history`？

`_compress_tool_error_history` 不是通用压缩，而是**业务级容错**：当同一工具连续输入校验失败时，防止冗余 error 历史淹没上下文。它成对过滤（assistant + tool），不会破坏协议完整性。因此保留在核心层作为兜底。

### 决策 3：`ContextLengthError` 时为什么直接报错？

删除 `_compress_context` 后，核心层不再具备自动压缩能力。如果扩展层没有注册 `BEFORE_CONTEXT_BUILD` Hook 来实现压缩，`ContextLengthError` 将直接暴露给用户。这是**显式失败优于静默破坏**的设计选择：
- 用户知道需要接入上下文压缩扩展
- 而不是让核心层用一个粗暴策略 silently corrupt 对话历史

---

## 验证状态

- [x] 全部 21 个现有单元测试通过
- [x] 代码静态检查无 import 错误
- [x] SessionEntry 协议完整性（assistant `tool_calls.id` ↔ tool `tool_call_id`）在核心层得到保障

### 已知限制 / 待办

- [ ] 需要扩展层实现具体的上下文压缩策略（如 TokenBudgetCompressor、SummarizationCompressor）并注册到 `BEFORE_CONTEXT_BUILD` Hook
- [ ] 建议未来在扩展层实现压缩策略时，增加对 assistant-tool 对完整性的校验断言
- [ ] `auto_compress_on_context_overflow` 配置项仍保留在 `ResilienceConfig` 中，但当前核心层不再消费；建议在扩展层实现压缩后，由扩展层读取该配置或废弃

---

## 附录：如何接入上下文压缩扩展

开发者可通过以下方式接入自定义压缩策略：

```python
from learning_agent.models import HookPoint, HookResult
from learning_agent.core.hook_system import HookSystem

async def my_context_compressor(history, context):
    # history: list[SessionEntry]
    # 实现压缩逻辑，确保 assistant-tool 对完整性
    compressed = ...
    return HookResult(modified=True, data=compressed)

hook_system.register(
    HookPoint.BEFORE_CONTEXT_BUILD,
    my_context_compressor,
    priority=10,
    extension_id="my-extension",
)
```
