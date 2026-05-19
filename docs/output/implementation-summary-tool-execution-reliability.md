# 实施总结：工具执行可靠性修复

> 依据技术文档：`docs/design/design-tool-execution-reliability-fix.md`
> 日期：2026-05-12

---

## 一、功能概述

本次修复针对 Agent Loop 工具执行层在长期运行场景下的 4 个可靠性缺陷，实现了以下能力：

1. **工具执行自动重试**：对 transient 异常（网络超时、连接错误、HTTP 503/502/504/429 等）自动重试，减少 LLM 决策压力。
2. **Ban 策略区分失败类型**：只有 `execution_error` 类型的服务级故障才计入 ban 计数，参数校验失败和策略拒绝不再惩罚工具。
3. **Chat-Only 降级自动恢复**：临时故障导致的 chat-only 降级支持自动恢复，避免永久性能力损伤。
4. **工具执行超时保护**：`ToolRegistry.execute()` 实现 `asyncio.wait_for` 超时，同步 handler 通过 `asyncio.to_thread` 包装，防止单个工具挂死阻塞整个 Agent Loop。

---

## 二、修改文件清单

| # | 文件 | 改动说明 |
|---|------|---------|
| 1 | `learning_agent/ai/models.py` | `ResilienceConfig` 新增 4 个字段：`max_tool_retries`、`tool_retry_base_delay`、`tool_retry_max_delay`、`chat_only_recovery_turns` |
| 2 | `learning_agent/agent/tool_failure_tracker.py` | `record_failure` 增加 `reason` 参数；`is_banned` 只统计 `execution_error`；新增 `get_all_failures_in_window`；`get_failures_in_window` 只统计 `execution_error` |
| 3 | `learning_agent/learning_agent/tool_registry.py` | `execute()` 增加 `timeout` 参数；使用 `asyncio.wait_for` 实现超时；同步 handler 通过 `asyncio.to_thread` 包装；超时后抛出 `TimeoutError`；替换 `asyncio.iscoroutinefunction` 为 `inspect.iscoroutinefunction`（Python 3.14 兼容） |
| 4 | `learning_agent/agent/agent_loop.py` | 新增 `_is_retryable_tool_error()` 方法；`_execute_tool_calls` Step 5 增加重试循环（指数退避）；各失败分支传入正确的 `reason`；`run()` 增加 Chat-Only 自动恢复逻辑；`__init__` 初始化 `_chat_only_success_turns = 0`；`agent.toolBanned` 事件扩展 `failure_types` |
| 5 | `tests/test_tool_execution_reliability.py` | 新增 21 个测试用例，覆盖全部 6 个验证场景 |
| 6 | `docs/output/implementation-summary-tool-execution-reliability.md` | 本文档 |

---

## 三、关键决策

| 决策项 | 结论 | 理由 |
|--------|------|------|
| 重试次数默认值 | `max_tool_retries = 1` | 对 transient 异常最多重试 1 次，平衡自愈能力与延迟 |
| 退避策略 | 指数退避：`base_delay * 2^(attempt-1)`，上限 `max_delay = 5s` | 快速恢复小抖动，避免长时间等待 |
| Ban 计数范围 | **只统计 `execution_error`** | 参数错误和策略拒绝是调用方/策略层问题，不应惩罚工具本身 |
| Chat-Only 恢复阈值 | `chat_only_recovery_turns = 3` | 3 个无工具调用的成功 turn 后恢复，兼顾稳定性与响应速度 |
| 超时后行为 | 抛出 `TimeoutError` → 被 `_is_retryable_tool_error` 识别为可重试 | 超时属于 transient 故障，应纳入重试链路 |
| 恢复逻辑位置 | 放在 `if tool_call_buffers:` 块**之外** | 恢复发生在**没有** tool calls 的 turn，必须在块外才能执行 |
| 同步 handler 包装 | `asyncio.to_thread` | 避免同步 handler 阻塞事件循环 |

---

## 四、验证状态

全部 21 个测试通过：

```
tests/test_tool_execution_reliability.py::TestToolFailureTracker::test_record_failure_with_reason PASSED
tests/test_tool_execution_reliability.py::TestToolFailureTracker::test_is_banned_only_counts_execution_error PASSED
tests/test_tool_execution_reliability.py::TestToolFailureTracker::test_is_banned_respects_window PASSED
tests/test_tool_execution_reliability.py::TestToolFailureTracker::test_record_success_clears_all PASSED
tests/test_tool_execution_reliability.py::TestToolFailureTracker::test_get_all_failures_in_window PASSED
tests/test_tool_execution_reliability.py::TestToolFailureTracker::test_get_failures_in_window_only_execution_error PASSED
tests/test_tool_execution_reliability.py::TestToolRegistry::test_execute_async_handler_success PASSED
tests/test_tool_execution_reliability.py::TestToolRegistry::test_execute_sync_handler_wrapped PASSED
tests/test_tool_execution_reliability.py::TestToolRegistry::test_execute_timeout_raises_timeout_error PASSED
tests/test_tool_execution_reliability.py::TestToolRegistry::test_execute_sync_handler_timeout PASSED
tests/test_tool_execution_reliability.py::TestAgentLoopRetryableError::test_retryable_exceptions PASSED
tests/test_tool_execution_reliability.py::TestAgentLoopRetryableError::test_retryable_keywords PASSED
tests/test_tool_execution_reliability.py::TestAgentLoopRetryableError::test_non_retryable_errors PASSED
tests/test_tool_execution_reliability.py::TestAgentLoopExecuteToolCalls::test_retry_success_on_transient_error PASSED
tests/test_tool_execution_reliability.py::TestAgentLoopExecuteToolCalls::test_retry_exhausted_records_failure PASSED
tests/test_tool_execution_reliability.py::TestAgentLoopExecuteToolCalls::test_no_retry_for_non_retryable_error PASSED
tests/test_tool_execution_reliability.py::TestAgentLoopExecuteToolCalls::test_validation_failed_no_ban PASSED
tests/test_tool_execution_reliability.py::TestAgentLoopExecuteToolCalls::test_hook_abort_no_ban PASSED
tests/test_tool_execution_reliability.py::TestAgentLoopExecuteToolCalls::test_execution_error_triggers_ban PASSED
tests/test_tool_execution_reliability.py::TestAgentLoopChatOnlyRecovery::test_chat_only_auto_recovery PASSED
tests/test_tool_execution_reliability.py::TestAgentLoopChatOnlyRecovery::test_chat_only_no_recovery_when_tool_calls_present PASSED
```

### 场景覆盖

| 场景 | 描述 | 状态 |
|------|------|------|
| A | 模拟工具 handler 抛出 `ConnectionError`，确认重试 1 次后成功 | ✅ 通过 |
| B | 模拟工具连续 validation 失败，确认不触发 ban | ✅ 通过 |
| C | 模拟工具连续 execution_error，确认触发 ban | ✅ 通过 |
| D | 降级到 chat-only 后，连续 3 个无工具 turn，确认自动恢复 | ✅ 通过 |
| E | 模拟工具 handler 死锁，确认超时后抛出 `TimeoutError` | ✅ 通过 |
| F | 模拟重试耗尽后 execution_error，确认错误回流给 LLM | ✅ 通过 |

---

## 五、已知限制与待办

1. **Chat-Only 恢复计数跨 Session**：`_chat_only_success_turns` 是 AgentLoop 实例级别的，同一 AgentLoop 跨 Session 运行会累计计数。当前设计符合预期（AgentLoop 实例通常绑定一个长期会话），如需 Session 级隔离可后续优化。
2. **同步 handler 超时精度**：`asyncio.to_thread` + `asyncio.wait_for` 的超时精度取决于线程调度，极端场景下可能有少量额外延迟。
3. **Python 3.14 兼容性**：已将 `asyncio.iscoroutinefunction` 替换为 `inspect.iscoroutinefunction`，消除 DeprecationWarning。
