# Resilience & Validation 实现总结

> 生成日期：2026-05-11
> 基于文档：`docs/design/resilience-and-validation-design.md`

---

## 一、概述

本次实现为 `learning-agent` 项目引入了完整的分层韧性机制，覆盖 Provider 层、Agent Loop 层和 Tool 执行层。所有代码均遵循"不侵入业务"原则，通过包装器/前置步骤注入，原有逻辑保持独立运行。

---

## 二、实现组件

### 1. 数据模型变更 (`learning_agent/models/models.py`)
- **`ToolDefinition`** 新增 `input_model` 字段（`Optional[Type[BaseModel]]`），支持内置工具走 Pydantic 强校验路径
- **新增 `ResilienceConfig`**：统一配置 Provider 重试、熔断、Turn 级重试、上下文压缩、Tool 滑动窗口禁用等全部参数
- **新增错误分类体系**：`ResilienceError` 基类及 7 个子类（`RetryableError`、`ContextLengthError`、`ValidationError`、`ToolBannedError`、`AuthError`、`InvalidRequestError`、`ServiceUnavailable`），作为各层重试/熔断/降级决策依据

### 2. ToolInputValidator (`learning_agent/core/tool_validator.py`)
- 实现双轨校验：Pydantic 模型优先 → JSON Schema fallback → 降级宽松校验
- 懒加载缓存 jsonschema 编译器，避免重复编译
- 提供标准错误文本格式化，直接回流给 LLM 自纠正

### 3. ToolFailureTracker (`learning_agent/core/tool_failure_tracker.py`)
- 滑动窗口失败计数器（默认 5 turn / 3 次阈值）
- 成功执行后清零、窗口过期自动失效
- 支持临时禁用判定和标准禁用消息生成

### 4. AgentLoop 重构 (`learning_agent/agent/agent_loop.py`)
- **初始化注入**：`resilience_config` 参数 + `_validator` + `_failure_tracker`
- **8 步流水线 `_execute_tool_calls`**：
  1. emit TOOL_EXECUTION_START
  2. ToolFailureTracker.is_banned()（临时禁用拦截）
  3. ToolInputValidator.validate()（结构校验拦截）
  4. HookSystem.execute(ON_TOOL_CALL)（权限 ask/abort）
  5. ToolRegistry.execute()（实际执行）
  6. HookSystem.execute(AFTER_TOOL_RESULT)
  7. emit TOOL_EXECUTION_END
  8. 结果保存到 SessionEntry（调用方处理）
- **Turn 级重试 `_stream_chat_with_retry`**：ContextLengthError 触发上下文压缩、RetryableError 指数退避重试、AuthError/InvalidRequestError 直接失败
- **上下文压缩 `_compress_context`**：移除最旧的一半非系统消息
- **历史压缩 `_compress_tool_error_history`**：对连续同一工具的 validation/banned error 压缩，最多保留最近 N 组
- **纯对话降级**：连续 N 个 turn 工具失败后，清空 tools 进入 chat-only 模式
- **事件发射**：在全部决策点发射 EventBus 事件（`agent.toolValidationFailed`、`agent.toolBanned`、`agent.toolPermissionDenied`、`agent.toolPermissionAsk`、`agent.turnRetry`、`agent.contextCompressed` 等）

### 5. 内置工具 Pydantic 迁移 (`learning_agent/extensions/code_tools.py`)
- 为 `read_file`、`write_file`、`edit_file`、`bash` 定义 Pydantic Input Model
- 注册时传入 `input_model`，校验器自动走 Pydantic 强校验路径

### 6. ResilientProvider (`learning_agent/provider/resilient_provider.py`)
- **CircuitBreaker 状态机**：CLOSED → OPEN → HALF_OPEN，基于失败计数和超时恢复
- **应用级重试**：对 RetryableError 触发指数退避，AuthError/InvalidRequestError 直接抛出
- **Fallback 链切换**：主 provider 熔断后自动切换到备用 provider
- **异常自动归类**：将底层 SDK 异常转换为标准韧性错误类型

---

## 三、依赖与导出更新

- `requirements.txt`：新增 `jsonschema>=4.0`
- `learning_agent/models/__init__.py`：导出全部新增模型和错误类型
- `learning_agent/provider/__init__.py`：导出 `ResilientProvider`

---

## 四、联调问题与修复

在联调过程中发现并修复了两个导致 `400 Invalid request` 的 API 兼容性问题：

1. **`reasoning_content` 缺失**：当模型开启 thinking 模式时，API 要求包含 `tool_calls` 的 assistant message 必须显式携带 `reasoning_content` 字段（即使是空字符串）。修复方式为在上下文构建时默认填充空字符串，并在 Provider 序列化时保留该字段。

2. **`tool_call_id` 不匹配**：流式传输中 OpenAI 可能在早期 chunk 不发送 `tool_call.id`，导致 assistant entry 的 `call_id` 与后续 tool result entry 的 `tool_call_id` 不一致。修复方式为在解析 tool calls 和保存 assistant entry 时统一兜底生成稳定的 `call_id`，确保 assistant message 与 tool message 的 ID 严格对应。

---

## 五、验证结果

- 全部模块通过 `python3 -m py_compile` 语法检查
- 核心组件（`ToolInputValidator`、`ToolFailureTracker`、`CircuitBreaker`）通过运行时功能测试
- 端到端导入验证通过

---

*本文档由代码实现过程生成，后续如有架构调整需同步更新。*
